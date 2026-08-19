#!/usr/bin/env python3
"""
Fetch all videos from the Corridor of Uncertainty YouTube channel, compare to
local .qmd episode links, and write a CSV audit plus a shell snippet of
generate_episode_2.py commands for gaps.

Matching: if a .qmd YAML front matter contains `youtube_id`, that value is the
canonical ID for the file (embedded watch/embed URLs are only used as fallback
or to flag mismatches).

Requires: yt-dlp on PATH (same as generate_episode_2.py).

Usage:
  python audit_youtube_episodes.py
  python audit_youtube_episodes.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cou_youtube import classify_title as _classify_title  # noqa: E402

CHANNEL_VIDEOS_URL = "https://www.youtube.com/@CorridorOfUncertainty/videos"
DEFAULT_OUT_CSV = "youtube_episode_audit.csv"
DEFAULT_OUT_SH = "youtube_episode_audit_commands.sh"


def run_yt_dlp_playlist() -> list[tuple[str, str, str]]:
    """Return list of (video_id, title, upload_date_yyyymmdd)."""
    # Tab-separated: video titles often contain "|", which breaks pipe-delimited output.
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--ignore-errors",
        "--print",
        "%(id)s\t%(title)s\t%(upload_date)s",
        "--playlist-end",
        "200",
        CHANNEL_VIDEOS_URL,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        sys.stderr.write(proc.stderr or "")
        raise RuntimeError(f"yt-dlp failed (exit {proc.returncode})")

    rows: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("WARNING:"):
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        vid, title, udate = parts
        if not re.fullmatch(r"[0-9A-Za-z_-]{11}", vid):
            continue
        rows.append((vid, title, udate))
    return rows


def format_upload_date(raw: str) -> str:
    if not raw or raw == "NA" or not raw.isdigit() or len(raw) != 8:
        return ""
    try:
        return datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return raw


VIDEO_ID_PATTERNS = [
    re.compile(r"(?:v=|/)([0-9A-Za-z_-]{11})(?:[&\s\"]|$)"),
    re.compile(r"youtu\.be/([0-9A-Za-z_-]{11})"),
    re.compile(r"embed/([0-9A-Za-z_-]{11})"),
]


def extract_video_ids_from_text(text: str) -> set[str]:
    found: set[str] = set()
    for pat in VIDEO_ID_PATTERNS:
        for m in pat.finditer(text):
            found.add(m.group(1))
    return found


FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_youtube_id_from_yaml(text: str) -> str | None:
    """Return youtube_id from Quarto YAML front matter, if valid."""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return None
    fm = m.group(1)
    for line in fm.splitlines():
        mm = re.match(r"^youtube_id:\s*(.+?)\s*$", line)
        if not mm:
            continue
        val = mm.group(1).strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if re.fullmatch(r"[0-9A-Za-z_-]{11}", val):
            return val
    return None


def scan_repo_for_video_ids(
    root: Path,
) -> tuple[dict[str, list[str]], dict[str, str], list[str]]:
    """
    Build canonical video_id -> .qmd paths.

    Returns:
        by_id: video ID -> list of repo-relative paths
        vid_match_source: video ID -> 'yaml' (from front matter) or 'url' (links only)
        warnings: YAML vs embed URL inconsistencies
    """
    by_id: dict[str, list[str]] = defaultdict(list)
    vid_match_source: dict[str, str] = {}
    warnings: list[str] = []

    for path in root.rglob("*.qmd"):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == "_site":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        rel_s = str(rel)
        yaml_id = parse_youtube_id_from_yaml(text)
        body_ids = extract_video_ids_from_text(text)

        if yaml_id:
            by_id[yaml_id].append(rel_s)
            vid_match_source[yaml_id] = "yaml"
            if body_ids and yaml_id not in body_ids:
                warnings.append(
                    f"{rel_s}: YAML youtube_id {yaml_id} not found in watch/embed "
                    f"URLs {sorted(body_ids)}"
                )
            stray = body_ids - {yaml_id}
            if stray:
                warnings.append(
                    f"{rel_s}: embedded IDs {sorted(stray)} differ from "
                    f"YAML youtube_id {yaml_id}"
                )
        else:
            for vid in body_ids:
                by_id[vid].append(rel_s)
                if vid_match_source.get(vid) != "yaml":
                    vid_match_source[vid] = "url"

    return dict(by_id), vid_match_source, warnings


def classify_title(title: str) -> tuple[str, str]:
    """
    Return (kind, label) where kind is
    main_numbered | bonus_fraction | unnumbered | trailer.

    Thin shim over ``cou_youtube.classify_title``. The implementation that used
    to live here had no branch for the "CoU N" title format the channel adopted
    around May 2026, so every episode from CoU 18 onwards was reported as
    ``unnumbered`` and excluded from the generated command list. Keeping one
    parser, in one place, is what stops that recurring.
    """
    info = _classify_title(title)
    return info.kind, info.label


def next_bonus_suffix(repo: Path) -> int:
    """Next episode-bonus-NNN number from existing files."""
    max_n = 0
    for path in (repo / "episodes").glob("episode-bonus-*.qmd"):
        m = re.search(r"episode-bonus-(\d+)", path.name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    for path in (repo / "episodes").glob("bonus-*.qmd"):
        m = re.search(r"bonus-(\d+)", path.name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def temp_episode_number_for_bonus(repo: Path, used: set[int]) -> int:
    """Pick a free integer for generate_episode_2 output before optional rename."""
    n = 900
    while n in used or (repo / "episodes" / f"episode-{n:03d}.qmd").exists():
        n += 1
        if n > 999:
            raise RuntimeError("Could not allocate temp episode number for bonus.")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit YouTube channel vs local Quarto episode pages."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Repository root (default: script directory)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help=f"Output CSV path (default: {DEFAULT_OUT_CSV} under root)",
    )
    parser.add_argument(
        "--commands",
        type=Path,
        default=None,
        help=f"Output shell snippet path (default: {DEFAULT_OUT_SH} under root)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    out_csv = args.csv or (root / DEFAULT_OUT_CSV)
    out_sh = args.commands or (root / DEFAULT_OUT_SH)

    print("Fetching channel list with yt-dlp (may take ~30s)...", file=sys.stderr)
    yt_rows = run_yt_dlp_playlist()
    site_map, vid_match_source, id_warnings = scan_repo_for_video_ids(root)
    for msg in id_warnings:
        print(f"youtube_id audit: {msg}", file=sys.stderr)
    yt_ids = {r[0] for r in yt_rows}

    used_episode_nums: set[int] = set()
    for path in (root / "episodes").glob("episode-*.qmd"):
        m = re.match(r"episode-(\d+)\.qmd$", path.name)
        if m:
            used_episode_nums.add(int(m.group(1)))

    next_bonus = next_bonus_suffix(root)

    audit_rows: list[dict[str, str]] = []
    commands: list[str] = [
        "#!/usr/bin/env bash",
        "# Auto-generated: run from repo root with ANTHROPIC_API_KEY set.",
        "# If YouTube blocks yt-dlp, set COU_YOUTUBE_COOKIES=/path/to/cookies.txt or add",
        "#   --cookies FILE / --cookies-from-browser chrome to each python line below.",
        "# Review the CSV notes first (bonus / unnumbered rows).",
        "set -euo pipefail",
        "cd \"$(dirname \"$0\")\"",
        "",
    ]

    for playlist_index, (vid, title, raw_date) in enumerate(yt_rows, start=1):
        date_iso = format_upload_date(raw_date)
        kind, label = classify_title(title)
        site_files = site_map.get(vid, [])
        on_site = "yes" if site_files else "no"
        files_cell = "; ".join(sorted(site_files))
        matched_via = vid_match_source.get(vid, "") if site_files else ""

        if kind == "trailer":
            status = "optional_trailer"
            cmd = ""
            notes = "Channel welcome video; episode page usually not needed."
        elif on_site == "yes":
            status = "on_site"
            cmd = ""
            notes = ""
        elif kind == "main_numbered" and label.isdigit():
            status = "missing_main"
            n = int(label)
            cmd = (
                f'python generate_episode_2.py "https://www.youtube.com/watch?v={vid}" '
                f"{n} --output-dir episodes"
            )
            commands.append(cmd)
            notes = ""
        elif kind == "bonus_fraction":
            status = "missing_bonus"
            temp_n = temp_episode_number_for_bonus(root, used_episode_nums)
            used_episode_nums.add(temp_n)
            target = f"episode-bonus-{next_bonus:03d}.qmd"
            cmd = (
                f'python generate_episode_2.py "https://www.youtube.com/watch?v={vid}" '
                f"{temp_n} --output-dir episodes && "
                f"mv episodes/episode-{temp_n:03d}.qmd episodes/{target}"
            )
            commands.append(f"# Bonus ({label}): renames generator output to {target}")
            commands.append(cmd)
            notes = (
                f"YouTube labels this {label}; generator cannot emit bonus filenames "
                f"directly — output is renamed to {target}."
            )
            next_bonus += 1
        else:
            status = "missing_unnumbered"
            cmd = ""
            notes = (
                "No episode number in title; add a .qmd manually or extend the "
                "generator. Not auto-commanded."
            )

        audit_rows.append(
            {
                "playlist_index": str(playlist_index),
                "video_id": vid,
                "youtube_url": f"https://www.youtube.com/watch?v={vid}",
                "youtube_title": title,
                "upload_date": date_iso,
                "youtube_kind": kind,
                "youtube_number_hint": label,
                "on_website": on_site,
                "matched_via": matched_via,
                "website_qmd_files": files_cell,
                "status": status,
                "generate_episode_command": cmd,
                "notes": notes,
            }
        )

    # Site-only IDs (e.g. retired YouTube or typo)
    for vid, paths in sorted(site_map.items()):
        if vid not in yt_ids:
            audit_rows.append(
                {
                    "playlist_index": "",
                    "video_id": vid,
                    "youtube_url": f"https://www.youtube.com/watch?v={vid}",
                    "youtube_title": "",
                    "upload_date": "",
                    "youtube_kind": "site_only",
                    "youtube_number_hint": "",
                    "on_website": "yes",
                    "matched_via": vid_match_source.get(vid, ""),
                    "website_qmd_files": "; ".join(sorted(paths)),
                    "status": "on_site_not_in_channel_feed",
                    "generate_episode_command": "",
                    "notes": "ID appears in local .qmd but was not in the channel "
                    "playlist fetch — check if video was removed or unlisted.",
                }
            )

    fieldnames = [
        "playlist_index",
        "video_id",
        "youtube_url",
        "youtube_title",
        "upload_date",
        "youtube_kind",
        "youtube_number_hint",
        "on_website",
        "matched_via",
        "website_qmd_files",
        "status",
        "generate_episode_command",
        "notes",
    ]
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(audit_rows)

    with out_sh.open("w", encoding="utf-8") as f:
        f.write("\n".join(commands) + "\n")
    out_sh.chmod(out_sh.stat().st_mode | 0o111)

    print(f"Wrote {out_csv}", file=sys.stderr)
    print(f"Wrote {out_sh}", file=sys.stderr)


if __name__ == "__main__":
    main()
