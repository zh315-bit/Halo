"""Demo feed until M1 exists: HALO_WARMUP_CSV at startup, and a background CSV replay at N× real time.
Every reading goes through core Engine.update(), the same path M1's live feed will use. Files are only
read from data/ (no path escapes)."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import core
from ..config_loader import REPO_ROOT, load_scoring_config
from ..core.csv_source import load_csv

log = logging.getLogger("halo.replay")
DATA_DIR = (REPO_ROOT / "data").resolve()


class ReplayPathError(ValueError):
    pass


def resolve_data_csv(name: str) -> Path:
    """'x.csv' or 'data/x.csv' -> absolute path inside data/. Rejects anything that resolves outside it."""
    raw = Path(name)
    if raw.is_absolute():
        raise ReplayPathError("give a path relative to data/")
    if raw.parts and raw.parts[0] == "data":
        raw = Path(*raw.parts[1:]) if len(raw.parts) > 1 else Path("")
    p = (DATA_DIR / raw).resolve()             # resolves .. and symlinks
    if DATA_DIR not in p.parents:
        raise ReplayPathError("only files inside data/ can be replayed")
    if p.suffix.lower() != ".csv":
        raise ReplayPathError("only .csv files can be replayed")
    if not p.is_file():
        raise FileNotFoundError(name)
    return p


def _engine() -> core.Engine:
    eng = core.get_engine()
    if eng is None:
        eng = core.Engine(load_scoring_config())
        core.set_engine(eng)
    return eng


def warmup_from_env() -> int:
    """HALO_WARMUP_CSV=qr_scan_demo_clean.csv -> feed the whole file into the live Engine at startup."""
    name = os.environ.get("HALO_WARMUP_CSV")
    if not name:
        return 0
    path = resolve_data_csv(name)
    eng = _engine()
    n = 0
    for r in load_csv(path, eng.cfg):
        eng.update(r)
        n += 1
    log.info("warm-up: %d readings from %s", n, path.name)
    return n


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Replayer:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status = self._idle()

    @staticmethod
    def _idle() -> dict:
        return {"state": "idle", "file": None, "speed": None, "index": 0, "total": 0, "last_reading_id": None,
                "last_as_of": None, "last_status": None, "started_at": None, "error": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, name: str, speed: float, reset: bool) -> dict:
        path = resolve_data_csv(name)
        eng = _engine()
        readings = load_csv(path, eng.cfg)             # parse up front: a bad file fails the request, not the thread
        with self._lock:
            if self.running():
                raise RuntimeError("a replay is already running; DELETE it first")
            if reset:
                eng.reset()
            self._stop.clear()
            self._status = {**self._idle(), "state": "running", "file": path.name, "speed": speed,
                            "total": len(readings), "started_at": _iso_now()}
            self._thread = threading.Thread(target=self._run, args=(eng, readings, speed), daemon=True,
                                            name="halo-replay")
            self._thread.start()
        return self.status()

    def _run(self, eng, readings, speed: float) -> None:
        prev = None
        try:
            for r in readings:
                if prev is not None:
                    gap = (r.timestamp - prev).total_seconds() / speed
                    if gap > 0 and self._stop.wait(gap):   # interruptible sleep; True = stop requested
                        break
                if self._stop.is_set():
                    break
                res = eng.update(r)                    # same path as M1's live feed
                prev = r.timestamp
                with self._lock:
                    self._status.update(index=self._status["index"] + 1, last_reading_id=res.reading_id,
                                        last_as_of=res.as_of, last_status=res.status)
            with self._lock:
                self._status["state"] = "stopped" if self._stop.is_set() else "done"
        except Exception as e:                          # e.g. reset=false and the file is older than the engine
            log.exception("replay failed")
            with self._lock:
                self._status.update(state="error", error=str(e))

    def stop(self, timeout: float = 5.0) -> dict:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
        return self.status()


replayer = Replayer()
