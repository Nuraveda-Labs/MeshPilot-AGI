#!/usr/bin/env python
"""Materialise the public template: the ship manifest, into a clean directory, with no history.

§ 3 of docs/plans/2026-09-21-oss-template-extraction.md chose a FRESH REPO over `git filter-repo`:
history is where the exposure lives, and a filtered history is only as safe as our confidence about
every commit ever made. That confidence is not warranted — a `.env.bak` with 8 live secrets reached
a commit on 2026-09-17 and was stopped by GitHub push protection, not by us.

⚠️ **The manifest is imported from `prepublish_gate`, never re-declared here.** Two lists of what
ships would drift, and this repo has been bitten three times by an allow-list that was wrong in a
direction nobody could see. One source, one place to be wrong.

This writes a DIRECTORY. It does not create a repo, add a remote, or push — publishing is an
outward, irreversible act and stays a human decision. `--git` will make the single initial commit
locally so the result can be inspected as a repo, still with no remote.

    uv run python scripts/export_template.py --out /tmp/meshpilot-template
    uv run python scripts/export_template.py --out /tmp/meshpilot-template --git
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import prepublish_gate as gate  # noqa: E402


def export(out: pathlib.Path, *, make_commit: bool = False) -> tuple[int, list[str]]:
    """Copy manifest ∩ tracked into `out`. Returns (file count, relative paths)."""
    if out.exists():
        # Refuse rather than merge: a stale file from a previous export silently becomes part of
        # the next one, which is the same class of accident this whole lane exists to prevent.
        if any(out.iterdir()):
            raise SystemExit(f"refusing to export into a non-empty directory: {out}")
    out.mkdir(parents=True, exist_ok=True)

    rels: list[str] = []
    for src in gate.ship_files():
        rel = src.relative_to(gate.ROOT)
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        rels.append(str(rel))

    if make_commit:
        # A single clean initial commit, NO remote. `git init` only — adding a remote or pushing
        # is deliberately not automated.
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=out, check=True)
        subprocess.run(["git", "add", "-A"], cwd=out, check=True)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q",
                        "-m", "Initial commit — MeshPilot template"], cwd=out, check=True)
    return len(rels), sorted(rels)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--git", action="store_true", help="make the single initial commit (no remote)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    n, rels = export(args.out.resolve(), make_commit=args.git)
    if not args.quiet:
        tops: dict[str, int] = {}
        for r in rels:
            tops[r.split("/")[0]] = tops.get(r.split("/")[0], 0) + 1
        print(f"exported {n} files to {args.out}")
        for k, v in sorted(tops.items(), key=lambda kv: -kv[1]):
            print(f"   {k:28} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
