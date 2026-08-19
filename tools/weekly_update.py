#!/usr/bin/env python3
"""
Corridor of Uncertainty — weekly site update.
=============================================

Run from cron every Tuesday. In one pass it:

1. Discovers the channel's videos (RSS + YouTube Data API, reconciled).
2. Works out which ones have no page yet, by joining on front-matter
   ``youtube_id`` — not on filename, so re-runs are idempotent.
3. Generates a Quarto page for each via the Anthropic API, validating the
   output before it is written.
4. Renders the new page(s) with Quarto as a build gate.
5. Commits and pushes to ``main``, letting the existing GitHub Actions workflow
   publish to ``gh-pages``.
6. Emails a report either way.

    python tools/weekly_update.py                 # normal weekly run
    python tools/weekly_update.py --backfill      # catch up the whole back-catalogue
    python tools/weekly_update.py --dry-run       # detect only, write nothing
    python tools/weekly_update.py --no-push       # generate + commit locally

Exit codes: 0 success (including "nothing to do"), 1 partial/total failure,
2 configuration or precondition error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify  # noqa: E402
from cou_youtube import (  # noqa: E402
    KIND_BONUS,
    KIND_TRAILER,
    KIND_UNNUMBERED,
    YouTubeError,
    Video,
    discover_videos,
    episode_path,
    repo_episode_numbers,
    repo_video_ids,
    resolve_channel_id,
    ytdlp_cookie_options,
)
from episode_generator import (  # noqa: E402
    GenerationError,
    existing_episode_summary,
    generate_episode,
)

log = logging.getLogger("cou.weekly")

REPO_ROOT = Path(os.environ.get("COU_REPO", Path(__file__).resolve().parent.parent))
LOCK_PATH = Path(os.environ.get("COU_LOCK", "/tmp/cou-weekly.lock"))
LOG_DIR = Path(os.environ.get("COU_LOG_DIR", REPO_ROOT / ".logs"))
DEFAULT_WINDOW_DAYS = int(os.environ.get("COU_WINDOW_DAYS", "10"))


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


class SingleInstance:
    """
    ``flock``-based mutex.

    A weekly cron job overlapping with itself should be impossible, but a manual
    run during a slow automated one is not — and two concurrent processes both
    committing to ``main`` would produce a mess.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self._fh = self.path.open("w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            raise RuntimeError(f"Another run holds {self.path}; aborting.") from exc
        self._fh.write(f"{os.getpid()}\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git(*args: str, cwd: Path = REPO_ROOT, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=180
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def preflight(repo: Path, *, push: bool) -> list[str]:
    """Fail fast on anything that would make a mess halfway through."""
    problems = []

    if not (repo / ".git").is_dir():
        problems.append(f"{repo} is not a git repository.")
        return problems

    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if branch != "main":
        problems.append(f"On branch {branch!r}, expected 'main'.")

    dirty = git("status", "--porcelain", cwd=repo)
    if dirty:
        problems.append(
            "Working tree is not clean:\n  " + "\n  ".join(dirty.splitlines()[:10])
        )

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        problems.append("ANTHROPIC_API_KEY is not set.")

    if push:
        proc = subprocess.run(
            ["git", "push", "--dry-run", "origin", "main"],
            cwd=repo, capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            problems.append(
                "Cannot push to origin (SSH key not available to cron?):\n  "
                + proc.stderr.strip()[:400]
            )

    return problems


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    to_generate: list[tuple[Video, int]] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_plan(
    repo: Path,
    videos: list[Video],
    *,
    backfill: bool,
    window_days: int,
    today: dt.date | None = None,
) -> Plan:
    """
    Decide which videos need a page.

    The join key is the front-matter ``youtube_id``, so a video that already has
    a page is skipped no matter what it is called or numbered. A video whose
    episode number is taken by a *different* video is reported as a collision
    rather than overwriting anything.
    """
    today = today or dt.date.today()
    plan = Plan()

    known_ids = repo_video_ids(repo)
    taken_numbers = repo_episode_numbers(repo)
    cutoff = today - dt.timedelta(days=window_days)

    for vid, paths in known_ids.items():
        if len(paths) > 1:
            plan.warnings.append(
                f"{vid} is claimed by {len(paths)} pages: "
                + ", ".join(p.name for p in paths)
            )

    claimed_this_run: dict[int, str] = {}

    for video in videos:
        info = video.info

        if video.video_id in known_ids:
            plan.skipped.append(
                {
                    "video_id": video.video_id,
                    "title": video.title,
                    "reason": f"already on site ({known_ids[video.video_id][0].name})",
                }
            )
            continue

        if info.kind == KIND_TRAILER:
            plan.skipped.append(
                {"video_id": video.video_id, "title": video.title, "reason": "channel trailer"}
            )
            continue

        if info.kind == KIND_UNNUMBERED or info.number is None:
            reason = "no episode number in title — needs a manual page"
            plan.skipped.append(
                {"video_id": video.video_id, "title": video.title, "reason": reason}
            )
            plan.warnings.append(f"UNNUMBERED: {video.title!r} ({video.url})")
            continue

        if not backfill:
            if video.published is None:
                plan.warnings.append(
                    f"{video.video_id} has no publish date; skipped in windowed mode."
                )
                continue
            if video.published < cutoff:
                plan.skipped.append(
                    {
                        "video_id": video.video_id,
                        "title": video.title,
                        "reason": f"published {video.published}, outside the "
                                  f"{window_days}-day window (use --backfill)",
                    }
                )
                continue

        number = info.number
        if number in taken_numbers:
            plan.warnings.append(
                f"COLLISION: {video.title!r} parses to episode {number}, but "
                f"{episode_path(repo, number).name} already exists for a different "
                "video. Not overwriting — resolve by hand."
            )
            plan.skipped.append(
                {
                    "video_id": video.video_id,
                    "title": video.title,
                    "reason": f"episode-{number:03d}.qmd already exists (collision)",
                }
            )
            continue

        if number in claimed_this_run:
            plan.warnings.append(
                f"COLLISION: {video.video_id} and {claimed_this_run[number]} both "
                f"parse to episode {number}."
            )
            plan.skipped.append(
                {
                    "video_id": video.video_id,
                    "title": video.title,
                    "reason": f"episode {number} claimed by another video this run",
                }
            )
            continue

        claimed_this_run[number] = video.video_id
        if info.kind == KIND_BONUS:
            plan.warnings.append(
                f"{video.video_id} uses the 'N+1' bonus convention -> episode {number}."
            )
        plan.to_generate.append((video, number))

    # Oldest first, so navigation links between consecutive new episodes resolve.
    plan.to_generate.sort(key=lambda pair: pair[1])
    return plan


# ---------------------------------------------------------------------------
# Render gate
# ---------------------------------------------------------------------------


def quarto_render(repo: Path, targets: list[Path], *, full: bool = False) -> tuple[bool, str]:
    """
    Render as a syntax/build gate.

    Rendering only the new pages keeps the job to a few seconds; ``--render-full``
    rebuilds the whole site, which catches listing-level problems too.
    """
    if shutil.which("quarto") is None:
        return True, "quarto not installed — render gate skipped"

    args = ["quarto", "render"] + ([] if full else [str(p.relative_to(repo)) for p in targets])
    proc = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=1800)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return False, output[-4000:]
    return True, output[-1000:]


# ---------------------------------------------------------------------------
# Upcoming page consistency
# ---------------------------------------------------------------------------


def check_upcoming(repo: Path, added_numbers: list[int]) -> list[str]:
    """Warn if upcoming.qmd still advertises an episode that has now been published."""
    path = repo / "upcoming.qmd"
    if not path.is_file() or not added_numbers:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    warnings = []
    for m in re.finditer(r"Episode\s+(\d+)", text):
        n = int(m.group(1))
        if n <= max(added_numbers):
            warnings.append(
                f"upcoming.qmd still lists 'Episode {n}' as coming soon, but episode "
                f"{max(added_numbers)} has now been published — worth updating."
            )
            break
    return warnings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"weekly-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
    handlers: list[logging.Handler] = [
        logging.FileHandler(logfile, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    # Keep the last 30 runs.
    for old in sorted(LOG_DIR.glob("weekly-*.log"))[:-30]:
        old.unlink(missing_ok=True)
    return logfile


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly Corridor of Uncertainty site update.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--backfill", action="store_true",
                        help="Generate every missing episode, not just recent ones.")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help="Look-back window for the normal weekly run.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap the number of episodes generated in one run (0 = no cap).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the plan; generate nothing, write nothing.")
    parser.add_argument("--no-push", action="store_true", help="Commit locally, do not push.")
    parser.add_argument("--no-render", action="store_true", help="Skip the Quarto render gate.")
    parser.add_argument("--render-full", action="store_true", help="Render the whole site.")
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--no-whatsapp", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Proceed even if the working tree is dirty (not advised).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logfile = setup_logging(args.verbose)
    started = dt.datetime.now()
    repo: Path = args.repo.resolve()

    report: dict = {
        "status": "ok",
        "host": platform.node(),
        "started": started.strftime("%Y-%m-%d %H:%M:%S %Z").strip(),
        "added": [],
        "skipped": [],
        "failed": [],
        "warnings": [],
        "pushed": False,
    }

    def finish(code: int) -> int:
        elapsed = int((dt.datetime.now() - started).total_seconds())
        report["duration"] = str(dt.timedelta(seconds=elapsed))
        if code != 0 or report["failed"]:
            report["status"] = "error"
        elif report["added"]:
            report["status"] = "ok"
        else:
            report["status"] = "no_change"
        if code != 0 or report["failed"]:
            try:
                report["log_tail"] = "\n".join(
                    logfile.read_text(encoding="utf-8").splitlines()[-60:]
                )
            except OSError:
                pass
        if not (args.no_email and args.no_whatsapp):
            notify.send_report(
                report,
                email=not args.no_email,
                whatsapp=not args.no_whatsapp,
            )
        log.info("Finished with status=%s exit=%s", report["status"], code)
        return code

    log.info("Repo: %s | backfill=%s dry_run=%s", repo, args.backfill, args.dry_run)

    # --- preconditions ---
    problems = preflight(repo, push=not (args.no_push or args.dry_run))
    if args.allow_dirty:
        problems = [p for p in problems if not p.startswith("Working tree is not clean")]
    if problems and not args.dry_run:
        for p in problems:
            log.error(p)
        report["failed"].append({"video_id": "-", "title": "preflight", "error": "; ".join(problems)})
        return finish(2)

    try:
        with SingleInstance(LOCK_PATH):
            return _run(args, repo, report, finish)
    except RuntimeError as exc:
        log.error("%s", exc)
        report["failed"].append({"video_id": "-", "title": "lock", "error": str(exc)})
        return finish(2)


def _run(args, repo: Path, report: dict, finish) -> int:
    # --- sync ---
    if not args.dry_run:
        try:
            git("fetch", "origin", "main", cwd=repo)
            git("merge", "--ff-only", "origin/main", cwd=repo)
        except RuntimeError as exc:
            log.error("Could not fast-forward from origin/main: %s", exc)
            report["failed"].append({"video_id": "-", "title": "git sync", "error": str(exc)})
            return finish(2)

    # --- discover ---
    try:
        channel_id = resolve_channel_id()
        log.info("Channel: %s", channel_id)
        discovery = discover_videos(channel_id=channel_id)
    except YouTubeError as exc:
        log.error("Discovery failed: %s", exc)
        report["failed"].append({"video_id": "-", "title": "discovery", "error": str(exc)})
        return finish(1)

    report["warnings"].extend(discovery.warnings)
    for w in discovery.warnings:
        log.warning(w)
    log.info("Discovered %d videos via %s", len(discovery.videos), "+".join(discovery.sources_used))

    # --- plan ---
    plan = build_plan(
        repo, discovery.videos, backfill=args.backfill, window_days=args.window_days
    )
    report["skipped"] = plan.skipped
    report["warnings"].extend(plan.warnings)
    for w in plan.warnings:
        log.warning(w)

    if args.limit and len(plan.to_generate) > args.limit:
        log.warning("Capping this run at %d of %d episodes", args.limit, len(plan.to_generate))
        plan.to_generate = plan.to_generate[: args.limit]

    if not plan.to_generate:
        log.info("Nothing to generate.")
        return finish(0)

    log.info("Will generate %d episode(s): %s", len(plan.to_generate),
             ", ".join(str(n) for _, n in plan.to_generate))

    if args.dry_run:
        for video, number in plan.to_generate:
            log.info("  DRY RUN episode %03d <- %s  %s", number, video.video_id, video.title)
        return finish(0)

    # --- generate ---
    cookie_opts = ytdlp_cookie_options()
    related = existing_episode_summary(repo)
    written: list[Path] = []

    for video, number in plan.to_generate:
        log.info("Generating episode %03d from %s", number, video.url)
        try:
            result = generate_episode(
                video_url=video.url,
                video_id=video.video_id,
                episode_number=number,
                related_titles=related,
                cookie_opts=cookie_opts,
            )
        except (GenerationError, YouTubeError) as exc:
            log.error("Episode %03d failed: %s", number, exc)
            report["failed"].append(
                {"video_id": video.video_id, "title": video.title, "error": str(exc)}
            )
            continue

        for w in result.validation.warnings:
            log.warning("episode %03d: %s", number, w)
        if not result.validation.ok:
            for e in result.validation.errors:
                log.error("episode %03d: %s", number, e)
            report["failed"].append(
                {
                    "video_id": video.video_id,
                    "title": video.title,
                    "error": "validation failed: " + "; ".join(result.validation.errors),
                }
            )
            continue

        out = episode_path(repo, number)
        out.write_text(result.content, encoding="utf-8")
        written.append(out)
        report["added"].append(
            {
                "episode_number": number,
                "title": result.title,
                "url": video.url,
                "file": str(out.relative_to(repo)),
                "validation_warnings": result.validation.warnings,
            }
        )
        log.info("Wrote %s", out.relative_to(repo))
        time.sleep(1)  # be gentle with YouTube between videos

    if not written:
        log.error("No episodes were written successfully.")
        return finish(1)

    # --- render gate ---
    if not args.no_render:
        log.info("Running Quarto render gate on %d file(s)", len(written))
        ok, output = quarto_render(repo, written, full=args.render_full)
        if not ok:
            log.error("Quarto render failed; rolling back the generated files.\n%s", output)
            for p in written:
                p.unlink(missing_ok=True)
            report["added"] = []
            report["failed"].append(
                {"video_id": "-", "title": "quarto render", "error": output[-1500:]}
            )
            return finish(1)
        log.info("Render gate passed.")

    report["warnings"].extend(check_upcoming(repo, [a["episode_number"] for a in report["added"]]))

    # --- commit ---
    git("add", *[str(p.relative_to(repo)) for p in written], cwd=repo)
    numbers = [a["episode_number"] for a in report["added"]]
    if len(numbers) == 1:
        subject = f"Add episode {numbers[0]} (automated weekly update)"
    else:
        subject = f"Add episodes {numbers[0]}–{numbers[-1]} (automated weekly update)"
    body = "\n".join(f"- Episode {a['episode_number']}: {a['title']}" for a in report["added"])
    git("commit", "-m", subject, "-m", body, cwd=repo)
    log.info("Committed: %s", subject)

    if args.no_push:
        log.info("--no-push set; leaving the commit local.")
        return finish(0)

    try:
        git("push", "origin", "main", cwd=repo)
        report["pushed"] = True
        log.info("Pushed to origin/main; GitHub Actions will publish to gh-pages.")
    except RuntimeError as exc:
        log.error("Push failed: %s", exc)
        report["failed"].append({"video_id": "-", "title": "git push", "error": str(exc)})
        return finish(1)

    return finish(0)


if __name__ == "__main__":
    raise SystemExit(main())
