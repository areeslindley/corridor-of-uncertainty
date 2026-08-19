#!/usr/bin/env python3
"""
Offline dry-run of the planner — no network, no API keys, no cost.

Feeds a CSV of channel videos through the real ``build_plan`` used by the
weekly job and prints what it would do. Useful for checking the effect of a
window change, a numbering rule, or a repo tidy-up without touching YouTube.

    python tools/simulate_plan.py                          # uses the audit CSV
    python tools/simulate_plan.py --today 2026-09-01 --backfill
    python tools/simulate_plan.py --csv some_other_audit.csv

Regenerate the input CSV with:  python tools/audit_youtube_episodes.py
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cou_youtube import Video  # noqa: E402
from weekly_update import build_plan  # noqa: E402


def load_csv(path: Path) -> list[Video]:
    videos = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row.get("video_id"):
                continue
            try:
                published = dt.date.fromisoformat(row["upload_date"])
            except (KeyError, ValueError):
                published = None
            videos.append(
                Video(
                    video_id=row["video_id"],
                    title=row.get("youtube_title", ""),
                    published=published,
                    sources={"csv"},
                )
            )
    return videos


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--window-days", type=int, default=10)
    parser.add_argument("--today", default=None, help="YYYY-MM-DD; defaults to today.")
    parser.add_argument("--show-skipped", action="store_true")
    args = parser.parse_args()

    csv_path = args.csv or (args.repo / "youtube_episode_audit.csv")
    if not csv_path.is_file():
        print(f"No CSV at {csv_path}. Run: python tools/audit_youtube_episodes.py", file=sys.stderr)
        return 2

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    videos = load_csv(csv_path)
    plan = build_plan(
        args.repo, videos,
        backfill=args.backfill, window_days=args.window_days, today=today,
    )

    mode = "BACKFILL" if args.backfill else f"WINDOWED ({args.window_days} days)"
    print(f"{len(videos)} videos from {csv_path.name} | mode: {mode} | as at {today}\n")

    print(f"WOULD GENERATE ({len(plan.to_generate)})")
    for video, number in plan.to_generate:
        print(f"  episode-{number:03d}.qmd  <-  {video.video_id}  {video.title[:64]}")
    if not plan.to_generate:
        print("  (nothing)")

    print(f"\nWARNINGS ({len(plan.warnings)})")
    for w in plan.warnings:
        print(f"  - {w}")
    if not plan.warnings:
        print("  (none)")

    if args.show_skipped:
        print(f"\nSKIPPED ({len(plan.skipped)})")
        for s in plan.skipped:
            print(f"  - {s['title'][:60]:<60} {s['reason']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
