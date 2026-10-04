#!/usr/bin/env python3
"""Aggregate completed months, wait for February, build the panel, then estimate."""
import subprocess
import sys
import time
import zipfile
from pathlib import Path

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/citibike"
MONTHLY = ROOT / "data/interim/monthly"
AGG = ROOT / "code/aggregate_citibike.py"
BUILD = ROOT / "code/build_panel.py"
EST = ROOT / "code/estimate.R"
LOG = ROOT / "output/pipeline_run.log"

def say(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with LOG.open("a") as handle:
        handle.write(line + "\n")

def zip_ok(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1_000_000:
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except Exception:
        return False

def qa_count() -> int:
    return len(list(MONTHLY.glob("qa_2025*.json")))

def main():
    say("aggregate pass 1")
    rc = subprocess.call([PY, "-u", str(AGG)])
    say(f"aggregate_rc={rc} qa={qa_count()}")
    while qa_count() < 12:
        feb = RAW / "202502-citibike-tripdata.zip"
        say(f"waiting feb size={feb.stat().st_size if feb.exists() else 0} qa={qa_count()}")
        if zip_ok(feb):
            say("february zip complete; aggregating")
            rc = subprocess.call([PY, "-u", str(AGG)])
            say(f"aggregate_rc={rc} qa={qa_count()}")
            if qa_count() < 12:
                time.sleep(20)
        else:
            time.sleep(30)
    say("building panel")
    rc = subprocess.call([PY, "-u", str(BUILD)])
    say(f"build_rc={rc}")
    if rc != 0:
        raise SystemExit(rc)
    summary = (ROOT / "output/panel_summary.txt").read_text()
    say(summary.replace("\n", " | "))
    say("estimating")
    rc = subprocess.call(["Rscript", str(EST)])
    say(f"estimate_rc={rc}")
    raise SystemExit(rc)

if __name__ == "__main__":
    main()
