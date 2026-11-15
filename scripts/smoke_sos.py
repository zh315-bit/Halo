"""SOS smoke test. Runs in-process by default (no server, no key, nothing sent); `--url` hits a running server.

    uv run python scripts/smoke_sos.py                       # all scenarios, in-process
    uv run python scripts/smoke_sos.py --url http://localhost:8000
    uv run python scripts/smoke_sos.py --only scan

Scenarios
  demo  CREW_01 replayed demo: start chest_pain SOS, cannot_do escalates and the checklist continues, resolve.
  scan  POST tests/fixtures/scan_record_sample.json to /api/v1/scan as LIVE_01 (LIVE_SMOKE with --url) -> SOS
        -> vitals come from camera_scan, SpO2 / BP / temp are `missing` with "Not measured by camera scan", the
        scan_context is labelled "Live camera measurement (experimental)", live_data lists the reading.
        SKIPPED (not failed) while /api/v1/scan does not exist yet.
  sos_event  replay data/SIMULATED_sos_event.csv (acute SpO2 drop, SIMULATED) through /api/v1/admin/replay ->
        the Engine hook raises a nudge for SIM_CREW_SOS (red flag spo2_low, breathing_difficulty first) -> accept it
        -> SOS shows alert / severity_max / red_flag:spo2_low with SpO2 < 90 from SIMULATED data -> walk the whole
        breathing_difficulty checklist to END -> resolve. Nudges are rate-limited (5 min per crew): on a reused
        server, wait 5 minutes or restart it between runs.
Exit code: 0 = all ran scenarios passed, 1 = a check failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "scan_record_sample.json"
LIVE_CREW = "LIVE_01"


class Skip(Exception):
    pass


def client(url: str | None):
    if url:
        try:
            import httpx2 as httpx  # comes with the anthropic SDK (uv sync --extra llm)
        except ImportError:
            import httpx            # dev dependency, always installed with `uv sync`

        class Remote:
            def __init__(self):
                self.c = httpx.Client(base_url=url, timeout=30)

            def __getattr__(self, name):
                return getattr(self.c, name)
        return Remote()
    os.environ.setdefault("ALERT_MODE", "dry_run")
    os.environ.setdefault("HALO_WARMUP_CSV", "qr_scan_demo_clean.csv")
    from fastapi.testclient import TestClient
    from halo_agent.api.app import app
    return TestClient(app)


def check(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        raise AssertionError(msg)


def scan_body(record: dict, crew_id: str) -> dict:
    """POST /api/v1/scan request body (main, AGENTS.md §8.6): {"crew_id", "record"}."""
    return {"crew_id": crew_id, "record": record}


def demo(c):
    s = c.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "chest_pain"}).json()
    check(s["state"] == "GUIDING" and s["step"]["id"] == "c0", "SOS starts guiding at c0")
    check(s["vitals_snapshot"]["reading_id"] is not None, f"vitals from {s['vitals_snapshot']['data_origin']}")
    check(s["escalation"]["state"] in ("simulated", "queued_offline", "sent"), "opening alert went out (dry run)")
    s = c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "cannot_do"}).json()
    check("cannot_do" in s["escalation"]["reasons"] and s["step"]["id"] != "c0", "cannot_do escalates, list continues")
    s = c.post(f"/api/v1/sos/{s['session_id']}/resolve", json={"outcome": "resolved"}).json()
    check(s["state"] == "RESOLVED", "session resolved")


def scan(c, crew_id: str = LIVE_CREW):
    if not FIXTURE.exists():
        raise Skip(f"fixture missing: {FIXTURE}")
    record = json.loads(FIXTURE.read_text(encoding="utf-8"))
    r = c.post("/api/v1/scan", json=scan_body(record, crew_id))
    if r.status_code in (404, 405):
        raise Skip("POST /api/v1/scan not available yet (main)")
    body = r.json() if r.status_code < 300 else {}
    check(r.status_code < 300, f"POST /api/v1/scan as {crew_id} -> {r.status_code} "
                               f"(reading_id={body.get('reading_id')}, duplicate={body.get('duplicate')})")
    s = c.post("/api/v1/sos", json={"crew_id": crew_id, "symptom": "other"}).json()
    v = s["vitals_snapshot"]
    check(v["data_origin"] == "camera_scan", f"vitals data_origin = {v['data_origin']}")
    for m in ("spo2_pct", "systolic_bp_mmhg", "diastolic_bp_mmhg"):
        mv = v["metrics"][m]
        check(mv["status"] == "missing" and mv.get("note") == "Not measured by camera scan",
              f"{m}: missing, 'Not measured by camera scan'")
    sc = s.get("scan_context")
    check(sc is not None and sc["label"] == "Live camera measurement (experimental)", "scan_context labelled live")
    check(sc["verdict"] == record["verdict"] and sc["breathing_brpm"] == record["breathing_brpm"],
          f"scan_context verdict={sc['verdict']} breathing={sc['breathing_brpm']}")
    ld = s.get("live_data") or {}
    check(any(x["reading_id"] == v["reading_id"] for x in ld.get("readings", [])), "live_data lists the live reading")
    check(s.get("other_data") is None, "no replayed/simulated data mixed into this session")
    c.post(f"/api/v1/sos/{s['session_id']}/resolve", json={"outcome": "resolved"})


EVENT_CSV = "SIMULATED_sos_event.csv"
EVENT_CREW = "SIM_CREW_SOS"


def sos_event(c):
    import time
    r = c.post("/api/v1/admin/replay", json={"file": EVENT_CSV, "speed": 1_000_000, "reset": True})
    if r.status_code == 404 and "no such file" in r.text:
        raise Skip(f"data/{EVENT_CSV} missing: run scripts/make_sos_scenario.py")
    check(r.status_code in (200, 202), f"replay {EVENT_CSV} started -> {r.status_code}")
    st = {}
    for _ in range(600):
        st = c.get("/api/v1/admin/replay").json()
        if st.get("state") in ("done", "error", "stopped"):
            break
        time.sleep(0.05)
    check(st.get("state") == "done" and st.get("last_status") == "alert",
          f"replay {st.get('state')}: {st.get('index')}/{st.get('total')} readings, last status {st.get('last_status')}")
    nudges = c.get(f"/api/v1/crew/{EVENT_CREW}/nudges").json()["nudges"]
    check(len(nudges) == 1, f"one active nudge for {EVENT_CREW} (got {len(nudges)}; "
                            "0 on a reused server within 5 min = rate limit)")
    n = nudges[0]
    sugg = [x["symptom"] for x in n["suggestions"]]
    check(n["trigger"]["kind"] == "red_flag" and n["trigger"]["red_flags"] == ["spo2_low"],
          f"nudge: {n['text']}")
    check(sugg[0] == "breathing_difficulty" and sugg[-1] == "other", f"suggestions {sugg}")
    a = c.post(f"/api/v1/nudges/{n['nudge_id']}/accept", json={"symptom": "breathing_difficulty"}).json()
    s = a["session"]
    check(a["nudge"]["state"] == "accepted" and s["protocol_id"] == "breathing_difficulty_v2",
          f"accepted -> SOS {s['session_id']} on {s['protocol_id']}")
    spo2 = s["vitals_snapshot"]["metrics"]["spo2_pct"]["value"]
    check(spo2 is not None and spo2 < 90, f"SpO2 shown {spo2} (< 90)")
    check("simulated" in (s["vitals_snapshot"]["data_origin"] or "").lower() and s["live_data"] is None,
          f"vitals from {s['vitals_snapshot']['data_origin']}, kept apart from live data")
    check((s["score"] or {}).get("status") == "alert" and "red_flag:spo2_low" in s["escalation"]["reasons"]
          and "severity_max" in s["escalation"]["reasons"], f"escalation {s['escalation']['reasons']}")
    walked = [s["step"]["id"]]
    for _ in range(40):
        if s["checklist_complete"]:
            break
        s = c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": "done"}).json()
        walked.append(s["step"]["id"])
    check(s["checklist_complete"], f"breathing_difficulty walked to END: {' > '.join(walked)}")
    s = c.post(f"/api/v1/sos/{s['session_id']}/resolve", json={"outcome": "resolved"}).json()
    check(s["state"] == "RESOLVED", "session resolved")


SCENARIOS = {"demo": demo, "scan": scan, "sos_event": sos_event}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--only", choices=sorted(SCENARIOS))
    ap.add_argument("--crew", help="live crew for the scan scenario (default: LIVE_01 in-process, LIVE_SMOKE with "
                                   "--url, so a real server's LIVE_01 history is not touched: /scan persists)")
    args = ap.parse_args(argv)
    crew = args.crew or ("LIVE_SMOKE" if args.url else LIVE_CREW)
    c = client(args.url)
    failed = 0
    for name, fn in SCENARIOS.items():
        if args.only and name != args.only:
            continue
        print(f"[{name}]")
        try:
            fn(c, crew) if name == "scan" else fn(c)
            print(f"  PASS  {name}")
        except Skip as e:
            print(f"  SKIP  {name}: {e}")
        except AssertionError:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
