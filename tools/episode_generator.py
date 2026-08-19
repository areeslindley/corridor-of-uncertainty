#!/usr/bin/env python3
"""
Corridor of Uncertainty — episode page generator.
=================================================

Supersedes ``generate_episode.py`` and ``generate_episode_2.py``, which had
drifted apart. This version is importable (so the weekly job can call it in
process), retries on transient API errors, and — importantly — *validates* its
own output before anything reaches the repository.

Can still be run by hand:

    python tools/episode_generator.py https://youtu.be/abc12345678 24
    python tools/episode_generator.py URL 24 --dry-run --output-dir /tmp
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cou_youtube import (  # noqa: E402
    YouTubeError,
    clean_episode_title,
    fetch_transcript,
    fetch_video_metadata,
    ytdlp_cookie_options,
)

log = logging.getLogger("cou.generator")

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------

#: Weekly cost is roughly (transcript tokens + 1.5k) in / ~2k out per episode.
#: At Opus rates that is a few tens of pence a week; Sonnet is ~5x cheaper and
#: perfectly adequate if you would rather not think about it.
MODEL = os.environ.get("COU_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("COU_MAX_TOKENS", "8000"))

#: The old scripts truncated the transcript at 12,000 characters — roughly
#: 2,000 words, i.e. under half of a typical 30-minute episode. Everything
#: after that point was invisible to the model, which is why later-episode
#: sections were thin. There is no context-window reason for a limit this
#: tight; 120k characters comfortably fits a full episode.
TRANSCRIPT_CHAR_LIMIT = int(os.environ.get("COU_TRANSCRIPT_CHARS", "120000"))
TIMESTAMP_CHAR_LIMIT = int(os.environ.get("COU_TIMESTAMP_CHARS", "40000"))

API_RETRIES = int(os.environ.get("COU_API_RETRIES", "4"))


class GenerationError(RuntimeError):
    """Raised when a page could not be generated or failed validation."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You write episode pages for "The Corridor of Uncertainty", a maths-and-sport \
podcast hosted by Dr Jess Hargreaves and Dr Rich Bingham in the Department of \
Mathematics at the University of York.

House style:
- Technical but accessible. Name statistical methods precisely and correctly \
(e.g. "Poisson regression", "Elo rating", "expected goals model"), then explain \
them in plain English. Do not dumb down; do not use a term the episode did not use.
- Conversational, matching the tone of the show itself. British English spelling.
- Every direct quotation must appear verbatim in the transcript you are given. \
Never invent, paraphrase-into-quotes, or tidy up a quote.
- If something cannot be determined from the transcript or metadata (a related \
episode, an external paper link), write the literal marker [NEEDS INPUT] rather \
than guessing. Do not fabricate references, DOIs or URLs.

Output the raw Quarto (.qmd) file content and nothing else: no preamble, no \
explanation, no surrounding code fences.
"""


def build_prompt(
    *,
    video_url: str,
    video_id: str,
    embed_url: str,
    episode_number: int,
    title: str,
    duration: str,
    publish_date: str,
    transcript: str,
    timestamped: str,
    related_titles: str = "",
) -> str:
    clean_title = clean_episode_title(title)
    prev_n, next_n = episode_number - 1, episode_number + 1

    related_block = (
        f"\nEXISTING EPISODES ON THE SITE (for the Related Episodes section — only "
        f"link to episodes listed here):\n{related_titles}\n"
        if related_titles
        else ""
    )

    return f"""\
Produce the Quarto episode page for the episode below.

EPISODE METADATA (authoritative — use these values, do not infer them):
- Episode number : {episode_number}
- YouTube title  : {title}
- Suggested clean title : {clean_title}
- Video URL      : {video_url}
- Embed URL      : {embed_url}
- Video ID       : {video_id}
- Duration       : {duration}
- Publish date   : {publish_date}
{related_block}
FULL TRANSCRIPT:
<transcript>
{transcript[:TRANSCRIPT_CHAR_LIMIT]}
</transcript>

TIMESTAMPED TRANSCRIPT (use to build the Timestamps section — pick 4-8 genuine
topic changes, not arbitrary intervals):
<timestamps>
{timestamped[:TIMESTAMP_CHAR_LIMIT]}
</timestamps>

TASKS:
1. Write a clean episode title: strip channel branding ("| CoU 24",
   "| Corridor of Uncertainty 24") and any "Episode N:" prefix.
2. Use the publish date exactly as given above.
3. Choose 3-5 category tags. Reuse the site's existing vocabulary where it fits
   (Football, Basketball, Cricket, Premier League, NBA, Probability, Elo Ratings,
   Poisson Distribution, Expected Goals, Competitive Balance, Sabermetrics,
   Prediction, Statistical Modelling).
4. Write a 2-3 sentence overview.
5. Identify 2-3 key statistical or mathematical concepts actually discussed, and
   explain each in 2-3 sentences with correct terminology.
6. Build the Timestamps section from the timestamped transcript.
7. List any papers, datasets, or links genuinely mentioned. If none, write
   "[NEEDS INPUT] no external resources identified in the transcript".
8. Pull 2-3 verbatim quotes from the transcript, each with a sentence of context.
9. Write 1-2 paragraphs connecting the episode's methods to broader themes in
   sports analytics.
10. Fill the navigation links with episode-{prev_n:03d}.qmd and episode-{next_n:03d}.qmd.
11. Reproduce the youtube_id exactly as given.

TEMPLATE — follow it exactly, replacing every [BRACKETED] item with real content:

---
title: "Episode {episode_number}: [CLEAN TITLE]"
date: {publish_date}
description: "[One sentence for the episode listing card]"
categories: [TOPIC1, TOPIC2, TOPIC3]
youtube_id: "{video_id}"
youtube_episode: {episode_number}
---

::: {{.grid}}

::: {{.g-col-6}}
**Published**: {publish_date}
**Duration**: {duration}
**YouTube**: [Watch here]({video_url})
:::

::: {{.g-col-6}}
**Topics Covered**: [TOPIC1, TOPIC2, TOPIC3]
:::

:::

## Video

<iframe width="100%" height="400" src="{embed_url}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>

## Overview

[2-3 sentence overview]

---

**Timestamps:**

- [MM:SS] [Topic]
- [MM:SS] [Topic]

**References:**

- [Papers, links or resources mentioned]

## Key Concepts

::: {{.callout-tip}}
## [CONCEPT 1 NAME]
[2-3 sentence explanation as discussed in the episode]
:::

::: {{.callout-tip}}
## [CONCEPT 2 NAME]
[2-3 sentence explanation as discussed in the episode]
:::

## Resources & References

- [Resource 1]

## Transcript Highlights

> "[Verbatim quote from the transcript]"

[1-2 sentences of context]

---

> "[Another verbatim quote]"

[Context]

## Discussion

[1-2 paragraphs connecting the episode's methods to broader sports analytics themes]

::: {{.callout-note}}
## Related Episodes
- [Related episode, or [NEEDS INPUT]]
:::

---

::: {{.grid}}

::: {{.g-col-6}}
[⬅️ Episode {prev_n}](episode-{prev_n:03d}.qmd)
:::

::: {{.g-col-6 .text-end}}
[➡️ Episode {next_n}](episode-{next_n:03d}.qmd)
:::

:::
"""


# ---------------------------------------------------------------------------
# Anthropic call
# ---------------------------------------------------------------------------


def call_model(prompt: str, *, model: str = MODEL, retries: int = API_RETRIES) -> str:
    """Call the Messages API, retrying with exponential backoff on 429/5xx."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise GenerationError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(retries):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in msg.content if getattr(block, "type", "") == "text"
            )
            if not text.strip():
                raise GenerationError("Model returned an empty response.")
            log.info(
                "Model %s: %d in / %d out tokens",
                model, msg.usage.input_tokens, msg.usage.output_tokens,
            )
            return text
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            status = getattr(exc, "status_code", None)
            retryable = isinstance(
                exc, (anthropic.RateLimitError, anthropic.APIConnectionError)
            ) or (status is not None and status >= 500)
            if not retryable or attempt == retries - 1:
                raise GenerationError(f"Anthropic API error: {exc}") from exc
            delay = 2 ** attempt * 5
            log.warning("API error (%s); retrying in %ss", exc, delay)
            time.sleep(delay)

    raise GenerationError("Exhausted API retries.")


# ---------------------------------------------------------------------------
# Post-processing and validation
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"\A\s*```(?:markdown|qmd|yaml)?\s*\n(.*)\n```\s*\Z", re.DOTALL)
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

#: An unfilled template placeholder: ALL-CAPS in square brackets, not part of a
#: markdown link ``[text](url)`` and not a timestamp like ``[MM:SS]``.
_PLACEHOLDER_RE = re.compile(r"\[([A-Z][A-Z0-9 _/&-]{2,})\](?!\()")

REQUIRED_FIELDS = ("title", "date", "description", "categories", "youtube_id")
REQUIRED_SECTIONS = ("## Video", "## Overview", "## Key Concepts", "## Discussion")


def strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text.strip()


def normalise_front_matter(qmd: str, *, video_id: str, episode_number: int) -> str:
    """Force ``youtube_id`` and ``youtube_episode`` to the known-correct values."""
    fm = _FRONT_MATTER_RE.match(qmd)
    if not fm:
        return qmd
    block = fm.group(1)

    def upsert(b: str, key: str, value: str) -> str:
        pattern = re.compile(rf"^{key}:.*$", re.M)
        return (
            pattern.sub(f"{key}: {value}", b, count=1)
            if pattern.search(b)
            else f"{b}\n{key}: {value}"
        )

    block = upsert(block, "youtube_id", f'"{video_id}"')
    block = upsert(block, "youtube_episode", str(episode_number))
    return f"---\n{block}\n---\n" + qmd[fm.end():]


@dataclass
class Validation:
    ok: bool
    errors: list[str]
    warnings: list[str]


def validate_qmd(qmd: str, *, video_id: str, transcript: str) -> Validation:
    """
    Structural and factual checks before anything is written to the repo.

    The quote check is the important one: it verifies that every block-quoted
    line actually occurs in the transcript, which is the main failure mode a
    fully autonomous pipeline needs to guard against.
    """
    errors: list[str] = []
    warnings: list[str] = []

    fm = _FRONT_MATTER_RE.match(qmd)
    if not fm:
        errors.append("No YAML front matter at the top of the file.")
    else:
        block = fm.group(1)
        for field_name in REQUIRED_FIELDS:
            if not re.search(rf"^{field_name}:", block, re.M):
                errors.append(f"Front matter is missing '{field_name}'.")
        if video_id not in block:
            errors.append(f"Front matter youtube_id does not contain {video_id}.")

    for section in REQUIRED_SECTIONS:
        if section not in qmd:
            errors.append(f"Missing section '{section}'.")

    body = qmd[fm.end():] if fm else qmd
    placeholders = sorted(set(_PLACEHOLDER_RE.findall(body)))
    placeholders = [p for p in placeholders if not re.fullmatch(r"MM:SS|NEEDS INPUT", p)]
    if placeholders:
        errors.append(f"Unfilled template placeholders: {', '.join(placeholders[:8])}")

    if "[NEEDS INPUT]" in qmd:
        warnings.append(
            f"{qmd.count('[NEEDS INPUT]')} [NEEDS INPUT] marker(s) left for you to fill."
        )

    # --- quote fidelity ---
    norm_transcript = _normalise_for_match(transcript)
    for raw_quote in re.findall(r'^>\s*"?(.+?)"?\s*$', qmd, re.M):
        quote = raw_quote.strip()
        if len(quote) < 25:
            continue
        if _normalise_for_match(quote) not in norm_transcript:
            warnings.append(f"Quote not found verbatim in transcript: {quote[:90]!r}")

    if qmd.count("```") % 2 != 0:
        errors.append("Unbalanced code fences.")
    open_divs = len(re.findall(r"^::: \{", qmd, re.M))
    close_divs = len(re.findall(r"^:::\s*$", qmd, re.M))
    if open_divs != close_divs:
        errors.append(f"Unbalanced Quarto divs: {open_divs} opened, {close_divs} closed.")

    return Validation(ok=not errors, errors=errors, warnings=warnings)


def _normalise_for_match(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace, for fuzzy quote matching."""
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


@dataclass
class GeneratedEpisode:
    episode_number: int
    video_id: str
    title: str
    publish_date: str
    content: str
    validation: Validation


def generate_episode(
    *,
    video_url: str,
    video_id: str,
    episode_number: int,
    related_titles: str = "",
    cookie_opts: dict | None = None,
    model: str = MODEL,
) -> GeneratedEpisode:
    """Fetch metadata + transcript, call the model, normalise and validate."""
    cookie_opts = cookie_opts if cookie_opts is not None else ytdlp_cookie_options()

    log.info("Fetching metadata for %s", video_url)
    meta = fetch_video_metadata(video_url, extra_opts=cookie_opts or None)
    log.info("  %s (%s, published %s)", meta["title"], meta["duration"], meta["publish_date"])

    log.info("Fetching transcript for %s", video_id)
    transcript, timestamped = fetch_transcript(video_id)
    words = len(transcript.split())
    log.info("  transcript: %d words / %d chars", words, len(transcript))
    if len(transcript) > TRANSCRIPT_CHAR_LIMIT:
        log.warning(
            "Transcript truncated to %d of %d chars", TRANSCRIPT_CHAR_LIMIT, len(transcript)
        )
    if words < 300:
        raise GenerationError(
            f"Transcript for {video_id} is only {words} words — captions are probably "
            "still being generated by YouTube. Try again later."
        )

    prompt = build_prompt(
        video_url=video_url,
        video_id=video_id,
        embed_url=f"https://www.youtube.com/embed/{video_id}",
        episode_number=episode_number,
        title=meta["title"],
        duration=meta["duration"],
        publish_date=meta["publish_date"],
        transcript=transcript,
        timestamped=timestamped,
        related_titles=related_titles,
    )

    content = strip_fences(call_model(prompt, model=model))
    content = normalise_front_matter(
        content, video_id=video_id, episode_number=episode_number
    )
    if not content.endswith("\n"):
        content += "\n"

    validation = validate_qmd(content, video_id=video_id, transcript=transcript)

    return GeneratedEpisode(
        episode_number=episode_number,
        video_id=video_id,
        title=meta["title"],
        publish_date=meta["publish_date"],
        content=content,
        validation=validation,
    )


def existing_episode_summary(repo: Path, limit: int = 40) -> str:
    """A compact 'episode N — title' list, used to ground Related Episodes links."""
    rows = []
    for path in sorted((repo / "episodes").glob("episode-*.qmd")):
        m = re.match(r"episode-(\d{3})\.qmd", path.name)
        if not m:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = _FRONT_MATTER_RE.match(text)
        if not fm:
            continue
        tm = re.search(r'^title:\s*"?(.+?)"?\s*$', fm.group(1), re.M)
        if tm:
            rows.append(f"- episode-{m.group(1)}.qmd — {tm.group(1)}")
    return "\n".join(rows[-limit:])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one Quarto episode page.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("episode_number", type=int)
    parser.add_argument("--output-dir", default="episodes", type=Path)
    parser.add_argument("--repo", default=Path(__file__).resolve().parent.parent, type=Path)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--dry-run", action="store_true", help="Print instead of writing.")
    parser.add_argument("--force", action="store_true", help="Write even if validation fails.")
    parser.add_argument("--cookies", type=Path)
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([0-9A-Za-z_-]{11})", args.url)
    if not m:
        log.error("Could not extract a video ID from %s", args.url)
        return 2
    video_id = m.group(1)

    try:
        result = generate_episode(
            video_url=args.url,
            video_id=video_id,
            episode_number=args.episode_number,
            related_titles=existing_episode_summary(args.repo),
            cookie_opts=ytdlp_cookie_options(args.cookies, args.cookies_from_browser),
            model=args.model,
        )
    except (GenerationError, YouTubeError) as exc:
        log.error("%s", exc)
        return 1

    for w in result.validation.warnings:
        log.warning("%s", w)
    for e in result.validation.errors:
        log.error("%s", e)

    if not result.validation.ok and not args.force:
        log.error("Validation failed; not writing. Re-run with --force to override.")
        return 1

    if args.dry_run:
        print(result.content)
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"episode-{args.episode_number:03d}.qmd"
    out.write_text(result.content, encoding="utf-8")
    log.info("Written %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
