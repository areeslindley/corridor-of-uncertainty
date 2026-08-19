#!/usr/bin/env python3
"""
Corridor of Uncertainty — shared YouTube / repository helpers.
=============================================================

This module is the single source of truth for three things that were
previously duplicated (and inconsistent) across ``generate_episode.py``,
``generate_episode_2.py`` and ``audit_youtube_episodes.py``:

1. **Parsing an episode number out of a YouTube title.** The channel has used
   at least four title conventions. See :func:`classify_title`.
2. **Discovering the channel's videos.** Two independent sources are used and
   reconciled — an Atom/RSS feed (cheap, unauthenticated, last ~15 uploads) and
   the YouTube Data API v3 (authoritative, full history). See
   :func:`discover_videos`.
3. **Working out what is already on the website.** Front-matter ``youtube_id``
   values are the join key between YouTube and the Quarto pages. See
   :func:`repo_video_ids`.

Nothing in here writes to the repository or calls an LLM; it is pure
read-only I/O plus parsing, which makes it cheap to unit-test.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger("cou.youtube")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHANNEL_HANDLE = os.environ.get("COU_CHANNEL_HANDLE", "CorridorOfUncertainty")

#: Resolved once and cached to disk; can be overridden entirely via env.
CHANNEL_ID_ENV = "COU_CHANNEL_ID"
CHANNEL_ID_CACHE = Path(
    os.environ.get("COU_CACHE_DIR", Path.home() / ".cache" / "corridor-of-uncertainty")
) / "channel_id"

RSS_URL_TMPL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
DATA_API_PLAYLIST_ITEMS = "https://www.googleapis.com/youtube/v3/playlistItems"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: Sanity bound on parsed episode numbers. Guards against a stray year ("2026")
#: or scoreline in a title being read as an episode number.
MAX_PLAUSIBLE_EPISODE = 500

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


class YouTubeError(RuntimeError):
    """Raised when a video source cannot be reached or parsed."""


# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------

# Kinds:
#   main_numbered  — a normal numbered episode, maps to episodes/episode-NNN.qmd
#   bonus_fraction — the "N+1" convention, historically mapped to episode N+1
#   trailer        — channel trailer / welcome video, no page wanted
#   unnumbered     — has no parseable number; must not be guessed at
KIND_MAIN = "main_numbered"
KIND_BONUS = "bonus_fraction"
KIND_TRAILER = "trailer"
KIND_UNNUMBERED = "unnumbered"

#: Matches the show name in any of its written forms, including the bare
#: initialism the channel switched to around May 2026 ("| CoU 18"). The absence
#: of a ``CoU`` branch in the original audit script is why every episode from
#: CoU 18 onwards was silently classified as ``unnumbered``.
_SHOW = r"(?:corridor\s+of\s+uncertainty|c\.?o\.?u\.?)"

_RE_TRAILER = re.compile(r"welcome\s+to\s+the\s+corridor\s+of\s+uncertainty", re.I)
_RE_BONUS = re.compile(rf"{_SHOW}\s*#?\s*(\d+)\s*\+\s*1", re.I)
_RE_EPISODE_PREFIX = re.compile(r"\bepisode\s*#?\s*(\d+)\s*[:\-–—]", re.I)
#: The number must directly follow the show name and be at the end of its
#: pipe-delimited segment, so "2016 GSW vs 2026 OKC | CoU 18" yields 18, not 2016.
_RE_SHOW_NUMBER = re.compile(rf"{_SHOW}\s*#?\s*(\d+)\s*(?=\||$)", re.I)


@dataclass(frozen=True)
class TitleInfo:
    """The result of parsing a YouTube title."""

    kind: str
    label: str = ""          # human-readable hint, e.g. "18" or "12+1"
    number: int | None = None  # the episode-NNN.qmd number this maps to

    @property
    def is_page_worthy(self) -> bool:
        return self.kind in (KIND_MAIN, KIND_BONUS) and self.number is not None


def _bounded(n: int) -> int | None:
    return n if 1 <= n <= MAX_PLAUSIBLE_EPISODE else None


def classify_title(title: str) -> TitleInfo:
    """
    Parse a YouTube title into a :class:`TitleInfo`.

    Rules are applied in order of specificity. The ``N+1`` bonus convention is
    tested before the plain-number rule, because "Corridor of Uncertainty 12+1"
    would otherwise match the plain rule and collapse onto episode 12 — which
    already exists.

    >>> classify_title("... | CoU 24").number
    24
    >>> classify_title("Episode 4: How random is the NBA?").number
    4
    >>> classify_title("Man U | ... | Corridor of Uncertainty 12+1").number
    13
    >>> classify_title("What does Team GB win? | Corridor of Uncertainty").kind
    'unnumbered'
    """
    t = (title or "").strip()

    if _RE_TRAILER.search(t):
        return TitleInfo(KIND_TRAILER)

    m = _RE_BONUS.search(t)
    if m:
        base = int(m.group(1))
        return TitleInfo(KIND_BONUS, f"{base}+1", _bounded(base + 1))

    m = _RE_EPISODE_PREFIX.search(t)
    if m:
        n = int(m.group(1))
        return TitleInfo(KIND_MAIN, str(n), _bounded(n))

    m = _RE_SHOW_NUMBER.search(t)
    if m:
        n = int(m.group(1))
        return TitleInfo(KIND_MAIN, str(n), _bounded(n))

    return TitleInfo(KIND_UNNUMBERED)


def clean_episode_title(title: str) -> str:
    """
    Strip channel branding and numbering from a title, leaving the topic.

    "Is there a Big Four in the WSL? | Competitive Balance Block Model | CoU 24"
    becomes "Is there a Big Four in the WSL? | Competitive Balance Block Model".
    """
    t = (title or "").strip()
    t = _RE_EPISODE_PREFIX.sub("", t).strip()
    segments = [s.strip() for s in t.split("|")]
    kept = [
        s
        for s in segments
        if s and not re.fullmatch(rf"{_SHOW}\s*#?\s*\d*(?:\s*\+\s*1)?", s, re.I)
    ]
    return " | ".join(kept) if kept else t


# ---------------------------------------------------------------------------
# Video record
# ---------------------------------------------------------------------------


@dataclass
class Video:
    """One video on the channel, as seen by one or more discovery sources."""

    video_id: str
    title: str
    published: date | None = None
    sources: set[str] = field(default_factory=set)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def embed_url(self) -> str:
        return f"https://www.youtube.com/embed/{self.video_id}"

    @property
    def info(self) -> TitleInfo:
        return classify_title(self.title)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _http_get(url: str, *, timeout: int = 20, retries: int = 3) -> bytes:
    """GET with exponential backoff on 429/5xx and transient network errors."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                delay = 2 ** attempt
                log.warning("HTTP %s from %s; retrying in %ss", exc.code, url, delay)
                time.sleep(delay)
                continue
            raise YouTubeError(f"HTTP {exc.code} fetching {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries - 1:
                delay = 2 ** attempt
                log.warning("Network error on %s (%s); retrying in %ss", url, exc, delay)
                time.sleep(delay)
                continue
    raise YouTubeError(f"Could not fetch {url}: {last}")


# ---------------------------------------------------------------------------
# Channel identity
# ---------------------------------------------------------------------------


def resolve_channel_id(handle: str = CHANNEL_HANDLE, *, use_cache: bool = True) -> str:
    """
    Return the ``UC…`` channel ID for a channel handle.

    The RSS feed is only addressable by channel ID, not by handle, so this has
    to be resolved once. Order of preference: environment variable, on-disk
    cache, then scraping the handle page for ``"externalId":"UC…"``.
    """
    env = os.environ.get(CHANNEL_ID_ENV, "").strip()
    if env:
        return env

    if use_cache and CHANNEL_ID_CACHE.is_file():
        cached = CHANNEL_ID_CACHE.read_text(encoding="utf-8").strip()
        if cached.startswith("UC"):
            return cached

    html = _http_get(f"https://www.youtube.com/@{handle}").decode("utf-8", "replace")
    m = re.search(r'"(?:externalId|channelId)":"(UC[0-9A-Za-z_-]{22})"', html)
    if not m:
        raise YouTubeError(
            f"Could not resolve channel ID for @{handle}. "
            f"Set {CHANNEL_ID_ENV} in the environment to bypass this lookup."
        )
    channel_id = m.group(1)
    try:
        CHANNEL_ID_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CHANNEL_ID_CACHE.write_text(channel_id, encoding="utf-8")
    except OSError as exc:  # cache is an optimisation, never fatal
        log.debug("Could not write channel-id cache: %s", exc)
    return channel_id


def uploads_playlist_id(channel_id: str) -> str:
    """
    Every channel has an implicit "uploads" playlist whose ID is the channel ID
    with the ``UC`` prefix replaced by ``UU``. Saves a ``channels.list`` call.
    """
    if not channel_id.startswith("UC"):
        raise ValueError(f"Not a channel ID: {channel_id!r}")
    return "UU" + channel_id[2:]


# ---------------------------------------------------------------------------
# Source 1 — Atom/RSS feed (no key, no quota, last ~15 uploads)
# ---------------------------------------------------------------------------


def fetch_rss(channel_id: str) -> list[Video]:
    """Return the ~15 most recent uploads from the channel's Atom feed."""
    raw = _http_get(RSS_URL_TMPL.format(channel_id=channel_id))
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise YouTubeError(f"Malformed RSS for channel {channel_id}: {exc}") from exc

    videos: list[Video] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        vid_el = entry.find("yt:videoId", ATOM_NS)
        title_el = entry.find("atom:title", ATOM_NS)
        pub_el = entry.find("atom:published", ATOM_NS)
        if vid_el is None or not (vid_el.text or "").strip():
            continue
        videos.append(
            Video(
                video_id=vid_el.text.strip(),
                title=(title_el.text or "").strip() if title_el is not None else "",
                published=_parse_iso_date(pub_el.text if pub_el is not None else None),
                sources={"rss"},
            )
        )
    log.info("RSS: %d videos", len(videos))
    return videos


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Source 2 — YouTube Data API v3 (authoritative, full history, needs a key)
# ---------------------------------------------------------------------------


def fetch_data_api(
    channel_id: str,
    api_key: str,
    *,
    max_pages: int = 20,
) -> list[Video]:
    """
    Page through ``playlistItems.list`` on the uploads playlist.

    Quota cost is 1 unit per page of 50, i.e. trivial against the default
    10,000 units/day. Returns newest-first, matching the RSS ordering.
    """
    playlist_id = uploads_playlist_id(channel_id)
    videos: list[Video] = []
    page_token = ""

    for _ in range(max_pages):
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": "50",
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{DATA_API_PLAYLIST_ITEMS}?{urllib.parse.urlencode(params)}"
        payload = json.loads(_http_get(url).decode("utf-8"))

        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            details = item.get("contentDetails", {})
            vid = details.get("videoId") or snippet.get("resourceId", {}).get("videoId")
            if not vid:
                continue
            videos.append(
                Video(
                    video_id=vid,
                    title=(snippet.get("title") or "").strip(),
                    published=_parse_iso_date(
                        details.get("videoPublishedAt") or snippet.get("publishedAt")
                    ),
                    sources={"data_api"},
                )
            )

        page_token = payload.get("nextPageToken", "")
        if not page_token:
            break

    log.info("Data API: %d videos", len(videos))
    return videos


# ---------------------------------------------------------------------------
# Source 3 — yt-dlp playlist dump (fallback when no API key is configured)
# ---------------------------------------------------------------------------


def fetch_ytdlp_playlist(handle: str = CHANNEL_HANDLE, *, limit: int = 200) -> list[Video]:
    """Flat playlist dump via the yt-dlp CLI. Slower and more block-prone."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end", str(limit),
        "--no-warnings",
        f"https://www.youtube.com/@{handle}/videos",
    ]
    cookies = os.environ.get("COU_YOUTUBE_COOKIES", "").strip()
    if cookies:
        cmd[-1:-1] = ["--cookies", cookies]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise YouTubeError(f"yt-dlp playlist dump failed: {proc.stderr.strip()[:500]}")

    payload = json.loads(proc.stdout)
    videos = []
    for entry in payload.get("entries", []):
        vid = entry.get("id")
        if not vid:
            continue
        ts = entry.get("timestamp")
        published = (
            datetime.fromtimestamp(ts, tz=timezone.utc).date() if ts else None
        )
        videos.append(
            Video(
                video_id=vid,
                title=(entry.get("title") or "").strip(),
                published=published,
                sources={"ytdlp"},
            )
        )
    log.info("yt-dlp: %d videos", len(videos))
    return videos


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass
class Discovery:
    videos: list[Video]
    warnings: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)


def discover_videos(
    *,
    channel_id: str | None = None,
    api_key: str | None = None,
    require_verification: bool = False,
) -> Discovery:
    """
    Fetch from every configured source and merge on ``video_id``.

    RSS is the cheap trigger; the Data API is the verifier and provides the
    full back-catalogue needed for backfill. Disagreements between the two are
    surfaced as warnings rather than silently resolved — if the RSS feed shows
    a video the API has not yet indexed (or vice versa) that is worth knowing.

    Set ``require_verification=True`` to make a missing/failing Data API a hard
    error instead of degrading to RSS-only.
    """
    channel_id = channel_id or resolve_channel_id()
    api_key = api_key if api_key is not None else os.environ.get("YOUTUBE_API_KEY", "")

    merged: dict[str, Video] = {}
    warnings: list[str] = []
    used: list[str] = []

    def absorb(batch: list[Video], source: str) -> None:
        used.append(source)
        for v in batch:
            existing = merged.get(v.video_id)
            if existing is None:
                merged[v.video_id] = v
                continue
            existing.sources |= v.sources
            # Prefer a non-null date; prefer the Data API's title as canonical.
            if existing.published is None:
                existing.published = v.published
            elif v.published and v.published != existing.published:
                warnings.append(
                    f"{v.video_id}: publish date differs between sources "
                    f"({existing.published} vs {v.published}); keeping the earlier."
                )
                existing.published = min(existing.published, v.published)
            if source == "data_api" and v.title:
                existing.title = v.title

    # --- RSS ---
    try:
        absorb(fetch_rss(channel_id), "rss")
    except YouTubeError as exc:
        warnings.append(f"RSS source failed: {exc}")

    # --- Data API ---
    if api_key:
        try:
            absorb(fetch_data_api(channel_id, api_key), "data_api")
        except YouTubeError as exc:
            if require_verification:
                raise
            warnings.append(f"Data API source failed: {exc}")
    else:
        msg = "YOUTUBE_API_KEY is not set — no Data API verification, no backfill history."
        if require_verification:
            raise YouTubeError(msg)
        warnings.append(msg)
        try:
            absorb(fetch_ytdlp_playlist(), "ytdlp")
        except Exception as exc:  # noqa: BLE001 — fallback of a fallback
            warnings.append(f"yt-dlp fallback failed: {exc}")

    if not merged:
        raise YouTubeError("No videos discovered from any source.")

    # Videos seen by exactly one source when two were queried: worth flagging.
    if len(used) > 1:
        for v in merged.values():
            if len(v.sources) == 1 and "rss" not in v.sources:
                continue  # RSS only covers ~15, so API-only is expected for old ones
            if v.sources == {"rss"} and "data_api" in used:
                warnings.append(
                    f"{v.video_id} ({v.title[:60]!r}) is in RSS but not the Data API — "
                    "likely published within the last few minutes."
                )

    videos = sorted(
        merged.values(),
        key=lambda v: (v.published or date.min, v.video_id),
        reverse=True,
    )
    return Discovery(videos=videos, warnings=warnings, sources_used=used)


# ---------------------------------------------------------------------------
# Repository state
# ---------------------------------------------------------------------------

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_YOUTUBE_ID_RE = re.compile(r"^youtube_id:\s*[\"']?([0-9A-Za-z_-]{11})[\"']?\s*$", re.M)
_EPISODE_FILE_RE = re.compile(r"^episode-(\d{3})\.qmd$")


def repo_video_ids(repo: Path) -> dict[str, list[Path]]:
    """
    Map ``youtube_id`` -> the .qmd file(s) that claim it.

    A list rather than a single path because the repo currently contains
    duplicates (e.g. ``bonus-001.qmd`` and ``episode-bonus-001.qmd`` both point
    at ``n4g1kiPJw3k``), and silently picking one would hide that.
    """
    found: dict[str, list[Path]] = {}
    episodes = repo / "episodes"
    if not episodes.is_dir():
        return found

    for path in sorted(episodes.glob("*.qmd")):
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = _FRONT_MATTER_RE.match(text)
        if not fm:
            continue
        for vid in _YOUTUBE_ID_RE.findall(fm.group(1)):
            found.setdefault(vid, []).append(path)
    return found


def repo_episode_numbers(repo: Path) -> set[int]:
    """Episode numbers with an existing ``episodes/episode-NNN.qmd``."""
    episodes = repo / "episodes"
    if not episodes.is_dir():
        return set()
    return {
        int(m.group(1))
        for p in episodes.glob("episode-*.qmd")
        if (m := _EPISODE_FILE_RE.match(p.name))
    }


def episode_path(repo: Path, number: int) -> Path:
    return repo / "episodes" / f"episode-{number:03d}.qmd"


# ---------------------------------------------------------------------------
# Per-video metadata and transcript (yt-dlp + youtube-transcript-api)
# ---------------------------------------------------------------------------


def ytdlp_cookie_options(
    cookies_file: str | Path | None = None,
    cookies_from_browser: str | None = None,
) -> dict:
    """Build yt-dlp options for authenticated extraction."""
    cookies_file = cookies_file or os.environ.get("COU_YOUTUBE_COOKIES") or None
    if cookies_file:
        path = Path(cookies_file).expanduser().resolve()
        if not path.is_file():
            raise YouTubeError(f"Cookies file not found: {path}")
        return {"cookiefile": str(path)}
    if cookies_from_browser:
        return {"cookiesfrombrowser": (cookies_from_browser.strip().lower(), None, None, None)}
    return {}


def fetch_video_metadata(url: str, extra_opts: dict | None = None) -> dict:
    """Title, uploader, duration (formatted) and publish date via yt-dlp."""
    import yt_dlp  # imported lazily so the module is importable without it

    opts = {"quiet": True, "skip_download": True, "no_warnings": True}
    if extra_opts:
        opts.update(extra_opts)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    secs = int(info.get("duration") or 0)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    duration = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    raw = info.get("upload_date") or info.get("release_date") or ""
    try:
        publish_date = datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        publish_date = datetime.today().strftime("%Y-%m-%d")

    return {
        "title": info.get("title", "Unknown Title"),
        "author": info.get("uploader", "Unknown"),
        "duration": duration,
        "duration_seconds": secs,
        "publish_date": publish_date,
    }


def _transcript_segments(video_id: str):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

    ytt = YouTubeTranscriptApi()
    try:
        return list(ytt.fetch(video_id, languages=["en", "en-GB", "en-US"]))
    except NoTranscriptFound:
        try:
            listing = ytt.list(video_id)
            return list(
                listing.find_generated_transcript(["en", "en-GB", "en-US"]).fetch()
            )
        except Exception as exc:  # noqa: BLE001
            raise YouTubeError(f"No transcript available for {video_id}: {exc}") from exc
    except TranscriptsDisabled as exc:
        raise YouTubeError(f"Transcripts are disabled for {video_id}.") from exc


def fetch_transcript(video_id: str) -> tuple[str, str]:
    """
    Return ``(plain_text, timestamped_text)`` for a video.

    Caption artefacts in square brackets ([Music], [Applause]) are stripped from
    the plain text, which is what gets fed to the model as prose; the
    timestamped variant is left intact for chapter detection.
    """
    segments = _transcript_segments(video_id)

    plain = " ".join(seg.text for seg in segments)
    plain = re.sub(r"\[.*?\]", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip()

    lines = []
    for seg in segments:
        mm, ss = int(seg.start // 60), int(seg.start % 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {seg.text}")

    return plain, "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover — tiny manual smoke test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cid = resolve_channel_id()
    print(f"channel_id = {cid}")
    result = discover_videos(channel_id=cid)
    for w in result.warnings:
        print(f"WARN  {w}")
    for v in result.videos[:20]:
        i = v.info
        print(f"{v.published}  {v.video_id}  {i.kind:<14} {str(i.number or '-'):>4}  {v.title[:70]}")
