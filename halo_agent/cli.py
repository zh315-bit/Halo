"""python -m halo_agent score data/qr_scan_demo_clean.csv --out out/scores.jsonl"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .config_loader import load_scoring_config
from .core.csv_source import load_csv
from .core.engine import Engine
from .core.score import score


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="halo_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score", help="replay a vitals CSV row by row and write one ScoreResult per row")
    s.add_argument("csv")
    s.add_argument("--out", default="out/scores.jsonl")
    s.add_argument("--live", action="store_true", help="use Engine.update instead of batch score()")
    a = p.parse_args(argv)

    cfg = load_scoring_config()
    readings = load_csv(a.csv, cfg)
    if a.live:
        eng = Engine(cfg)
        results = [eng.update(r) for r in readings]
    else:
        results = score(readings, cfg)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for res in results:
            f.write(json.dumps(res.to_dict(), ensure_ascii=False) + "\n")
    c = Counter(r.status for r in results)
    n = len(results)
    print(f"{n} readings -> {out}  " + "  ".join(f"{k}={c[k]} ({c[k] / n:.1%})" for k in ("normal", "caution", "alert")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
