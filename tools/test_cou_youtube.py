#!/usr/bin/env python3
"""
Tests for the title parser and the run planner.

Run with:  .venv/bin/python -m pytest tools/ -q

The title corpus below is every title the channel has published to date, taken
verbatim from youtube_episode_audit.csv. It is deliberately real data rather
than invented examples: the bug this suite exists to prevent (no "CoU N" branch
in the parser) was invisible to synthetic tests but affected 6 real episodes.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cou_youtube import (  # noqa: E402
    KIND_BONUS,
    KIND_MAIN,
    KIND_TRAILER,
    KIND_UNNUMBERED,
    Video,
    classify_title,
    clean_episode_title,
)
from weekly_update import build_plan  # noqa: E402


# (title, expected_kind, expected_number)
CORPUS = [
    ("Is there a Big Four in the Women's Super League? | Competitive Balance Block Model | CoU 24", KIND_MAIN, 24),
    ("WSL Season Review | Did Man City Deserve the title? | CoU 23", KIND_MAIN, 23),
    ("Is there a group of death at the World Cup? | Who will England Play? | CoU 22", KIND_MAIN, 22),
    ("Who will win the World Cup | Can Maths decide? | Corridor of Uncertainty 21", KIND_MAIN, 21),
    ("Premier League Season Review | How did our predictions turn out? | CoU 20", KIND_MAIN, 20),
    ("How Fair is the Women's Super League? | What is Competitive Balance? | CoU 19", KIND_MAIN, 19),
    ("How good were the 2016 Golden State Warriors Really? | 2016 GSW vs 2026 OKC | CoU 18", KIND_MAIN, 18),
    ("Can we find the Corridor of Uncertainty? | Analysing football event data |Corridor of Uncertainty 17", KIND_MAIN, 17),
    ("What is xG? | How to actually calculate it! | Corridor of Uncertainty 16", KIND_MAIN, 16),
    ("West is Best? | Ranking NBA divisions using Maths | Corridor of Uncertainty 15", KIND_MAIN, 15),
    ("Who will win the NBA Championship? | Basketball & Elo ratings | Corridor of Uncertainty 14", KIND_MAIN, 14),
    ("Man U: Amorim vs Carrick | How would the season have gone? | Corridor of Uncertainty 12+1", KIND_BONUS, 13),
    ("How to predict Premier League Games | Could Arsenal lose the title? | Corridor of Uncertainty 12", KIND_MAIN, 12),
    ("The Pythagorean Theorem of Baseball | Corridor of Uncertainty 11", KIND_MAIN, 11),
    ("RSS Sports Statistic of the Year 2025 | Corridor of Uncertainty 10", KIND_MAIN, 10),
    ("Have the Dodgers broken Moneyball? | How MLB teams use maths | Corridor of Uncertainty 9", KIND_MAIN, 9),
    ("Curling by Numbers: Why GB Won Silver | Winter Olympics 2026 | Corridor of Uncertainty 8", KIND_MAIN, 8),
    ("Why did Man City sign Semenyo? | How football clubs use data | Corridor of Uncertainty 7", KIND_MAIN, 7),
    ("What does Team GB win at the Winter Olympics? | Corridor of Uncertainty", KIND_UNNUMBERED, None),
    ("How to Win at Curling (using maths) | Winter Olympics 2026 | Corridor of Uncertainty 6", KIND_MAIN, 6),
    ("Best January Transfer Signings Ever?  |  Can Stats Decide?  |  Corridor of Uncertainty 5", KIND_MAIN, 5),
    ("Episode 4: How random is the NBA? - Can statistics prove that basketball is a bad sport?", KIND_MAIN, 4),
    ("Episode 3: The CoU Sports Statistic of the Year 2025", KIND_MAIN, 3),
    ("Which Sports win BBC Sports Personality of the Year the most? The Corridor of Uncertainty", KIND_UNNUMBERED, None),
    ("Episode 2: Sports Personality of the Year - Can we use stats to rank sports?", KIND_MAIN, 2),
    ("Episode 1: The SPLit - How fair is Scottish Football?", KIND_MAIN, 1),
    ("Welcome to the Corridor of Uncertainty!", KIND_TRAILER, None),
]


@pytest.mark.parametrize("title,kind,number", CORPUS)
def test_classify_real_titles(title, kind, number):
    info = classify_title(title)
    assert info.kind == kind, f"{title!r} -> {info.kind}"
    assert info.number == number, f"{title!r} -> {info.number}"


def test_every_published_episode_is_page_worthy():
    """Only the trailer and the two genuinely unnumbered one-offs may be skipped."""
    skipped = [t for t, k, _ in CORPUS if k in (KIND_UNNUMBERED, KIND_TRAILER)]
    assert len(skipped) == 3


@pytest.mark.parametrize(
    "title,expected",
    [
        # A year in the title must not be mistaken for an episode number.
        ("Winter Olympics 2026 preview | CoU 30", 30),
        # Nor a scoreline or a stray number in the middle of a segment.
        ("Arsenal 3 Spurs 2 | What went wrong | CoU 31", 31),
        # Hash and dot-separated variants of the initialism.
        ("Something interesting | CoU #32", 32),
        ("Something interesting | C.O.U. 33", 33),
        # Case insensitivity.
        ("Something | cou 34", 34),
    ],
)
def test_future_title_variants(title, expected):
    assert classify_title(title).number == expected


@pytest.mark.parametrize(
    "title",
    [
        "Corridor of Uncertainty 2026 season preview",  # 2026 exceeds the sanity bound
        "A chat about sport",
        "",
    ],
)
def test_implausible_or_absent_numbers_are_not_guessed(title):
    info = classify_title(title)
    assert info.number is None


def test_clean_episode_title_strips_branding():
    assert clean_episode_title(
        "WSL Season Review | Did Man City Deserve the title? | CoU 23"
    ) == "WSL Season Review | Did Man City Deserve the title?"
    assert clean_episode_title(
        "Episode 4: How random is the NBA?"
    ) == "How random is the NBA?"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    (episodes / "episode-023.qmd").write_text(
        '---\ntitle: "Episode 23: WSL Season Review"\ndate: 2026-06-16\n'
        'description: "x"\ncategories: [Football]\nyoutube_id: "bwtBbsXz_GA"\n---\n\nbody\n',
        encoding="utf-8",
    )
    return tmp_path


def _video(vid, title, published):
    return Video(video_id=vid, title=title, published=published, sources={"rss"})


def test_plan_skips_videos_already_on_site(fake_repo):
    videos = [_video("bwtBbsXz_GA", "WSL Season Review | ... | CoU 23", dt.date(2026, 6, 16))]
    plan = build_plan(fake_repo, videos, backfill=True, window_days=10)
    assert plan.to_generate == []
    assert "already on site" in plan.skipped[0]["reason"]


def test_plan_picks_up_a_new_numbered_episode(fake_repo):
    videos = [_video("aaaaaaaaaaa", "Big Four in the WSL | CoU 24", dt.date(2026, 6, 23))]
    plan = build_plan(
        fake_repo, videos, backfill=True, window_days=10, today=dt.date(2026, 6, 24)
    )
    assert [n for _, n in plan.to_generate] == [24]


def test_plan_refuses_to_overwrite_on_number_collision(fake_repo):
    """A different video parsing to an existing episode number must not clobber it."""
    videos = [_video("zzzzzzzzzzz", "Some other video | CoU 23", dt.date(2026, 7, 1))]
    plan = build_plan(fake_repo, videos, backfill=True, window_days=10)
    assert plan.to_generate == []
    assert any("COLLISION" in w for w in plan.warnings)


def test_plan_never_guesses_a_number_for_unnumbered_videos(fake_repo):
    videos = [_video("yyyyyyyyyyy", "A one-off chat | Corridor of Uncertainty", dt.date(2026, 7, 1))]
    plan = build_plan(fake_repo, videos, backfill=True, window_days=10)
    assert plan.to_generate == []
    assert any("UNNUMBERED" in w for w in plan.warnings)


def test_window_excludes_old_videos_but_backfill_includes_them(fake_repo):
    videos = [_video("aaaaaaaaaaa", "Big Four in the WSL | CoU 24", dt.date(2026, 6, 23))]
    today = dt.date(2026, 8, 10)

    windowed = build_plan(fake_repo, videos, backfill=False, window_days=10, today=today)
    assert windowed.to_generate == []

    backfilled = build_plan(fake_repo, videos, backfill=True, window_days=10, today=today)
    assert [n for _, n in backfilled.to_generate] == [24]


def test_plan_orders_oldest_first(fake_repo):
    videos = [
        _video("bbbbbbbbbbb", "Later | CoU 26", dt.date(2026, 7, 7)),
        _video("aaaaaaaaaaa", "Earlier | CoU 24", dt.date(2026, 6, 23)),
        _video("ccccccccccc", "Middle | CoU 25", dt.date(2026, 6, 30)),
    ]
    plan = build_plan(fake_repo, videos, backfill=True, window_days=10)
    assert [n for _, n in plan.to_generate] == [24, 25, 26]


def test_duplicate_youtube_ids_in_repo_are_reported(tmp_path):
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    for name in ("bonus-001.qmd", "episode-bonus-001.qmd"):
        (episodes / name).write_text(
            f'---\ntitle: "{name}"\nyoutube_id: "n4g1kiPJw3k"\n---\n\nbody\n',
            encoding="utf-8",
        )
    plan = build_plan(tmp_path, [], backfill=True, window_days=10)
    assert any("claimed by 2 pages" in w for w in plan.warnings)
