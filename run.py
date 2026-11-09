#!/usr/bin/env python3
"""Run the whole pipeline: collect -> normalize -> metrics -> dashboard (en + tr) -> internal notes.

  python run.py              reuse raw/ if it is already populated, regenerate everything else
  python run.py --refresh    archive the current raw evidence, then collect a fresh sample
  python run.py --days 30    window for a fresh collection (passed to collect.py)

Each stage prints one line with its timing. A stage fails loudly (non-zero exit) if its command
fails or if any of its expected outputs is missing, empty, or was not rewritten by the stage.
Generated outputs are deterministic: the same raw/ always produces byte-identical files.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
META = RAW / "_meta"
PY = sys.executable

# Collected evidence that --refresh archives. Inputs (inventory.tsv, repos.txt) stay in place.
COLLECTED = [RAW / "piaddys", META / "checkpoints", META / "teams", META / "sample_manifest.json",
             META / "teams.json", META / "inventory.json"]


class StageError(SystemExit):
    pass


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def fail(stage: str, msg: str):
    print(f"[{stage:<10}] FAILED: {msg}", flush=True)
    raise StageError(1)


def raw_status() -> str | None:
    """Describe raw/ if it holds a complete sample, else None."""
    manifest = META / "sample_manifest.json"
    if not manifest.exists() or not (META / "teams.json").exists():
        return None
    m = json.loads(manifest.read_text())
    done = [r for r in m["repos"] if (META / "checkpoints" / f"{r}.json").exists()]
    if len(done) != len(m["repos"]):
        return None
    prs = sum(v["pr_count"] for v in m["per_repo"].values())
    return f"{len(done)} repos, {prs} PRs, cutoff {m['cutoff_utc']}"


def run_stage(name: str, cmds: list[list[str]], outputs: list[Path]):
    t0 = time.monotonic()
    started = time.time()
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            sys.stdout.write(r.stdout)
            sys.stderr.write(r.stderr)
            fail(name, f"`{' '.join(Path(c).name if i == 0 else c for i, c in enumerate(cmd))}` "
                       f"exited with {r.returncode}")
    for out in outputs:
        if not out.exists():
            fail(name, f"expected output {rel(out)} was not produced")
        if out.is_file() and out.stat().st_size == 0:
            fail(name, f"output {rel(out)} is empty")
        if out.is_file() and out.stat().st_mtime < started - 1:
            fail(name, f"output {rel(out)} was not rewritten by this stage")
    sizes = ", ".join(rel(o) for o in outputs)
    print(f"[{name:<10}] ok in {time.monotonic() - t0:5.1f}s -> {sizes}", flush=True)


def archive_raw() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = ROOT / "raw_archive" / stamp
    dest.mkdir(parents=True)
    for p in COLLECTED:
        if p.exists():
            target = dest / p.relative_to(RAW)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(target))
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="archive current raw evidence to raw_archive/<UTC stamp>/ and collect again")
    ap.add_argument("--days", type=int, default=30, help="collection window for a fresh collection")
    args = ap.parse_args()
    t0 = time.monotonic()

    status = None if args.refresh else raw_status()
    if status:
        print(f"[{'collect':<10}] skipped: raw/ already populated ({status}); use --refresh to re-collect",
              flush=True)
    else:
        if args.refresh and (RAW / "piaddys").exists():
            dest = archive_raw()
            print(f"[{'archive':<10}] previous raw evidence moved to {rel(dest)}", flush=True)
        run_stage("collect", [[PY, "collect.py", "--days", str(args.days)], [PY, "collect.py", "--teams"]],
                  [META / "sample_manifest.json", META / "inventory.json", META / "teams.json"])
        if not raw_status():
            fail("collect", "raw/ is incomplete after collection (missing repo checkpoints or teams.json)")

    run_stage("normalize", [[PY, "normalize.py"]],
              [ROOT / "data" / "prs.csv", ROOT / "data" / "reviews.csv", ROOT / "data" / "findings.csv"])
    run_stage("metrics", [[PY, "metrics.py"]], [ROOT / "docs" / "metrics.md"])
    run_stage("dashboard", [[PY, "dashboard.py", "--lang", "all"]],
              [ROOT / "docs" / "dashboard.html", ROOT / "docs" / "dashboard-tr.html"])
    run_stage("notes", [[PY, "internal_notes.py"]], [ROOT / "docs" / "internal-notes.md"])
    print(f"[{'done':<10}] pipeline finished in {time.monotonic() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
