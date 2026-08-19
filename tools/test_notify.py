"""Tests for tools/notify.py."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import notify


def test_render_whatsapp_summary_skips_no_change():
    assert notify.render_whatsapp_summary({"status": "no_change", "added": []}) is None


def test_render_whatsapp_summary_pushed_episode():
    report = {
        "status": "ok",
        "pushed": True,
        "added": [
            {
                "episode_number": 30,
                "title": "How Competitive is Formula 1?",
                "url": "https://www.youtube.com/watch?v=V4ipZEAEUO8",
            }
        ],
    }
    text = notify.render_whatsapp_summary(report)
    assert text is not None
    assert "Episode 30" in text
    assert "pushed to the site" in text
    assert "V4ipZEAEUO8" in text


def test_render_whatsapp_summary_failure():
    report = {
        "status": "error",
        "failed": [{"title": "quarto render", "error": "build failed"}],
    }
    text = notify.render_whatsapp_summary(report)
    assert text is not None
    assert "FAILED" in text
    assert "quarto render" in text


@patch.dict(
    os.environ,
    {"COU_WHATSAPP_PHONE": "+447700900123", "COU_WHATSAPP_APIKEY": "testkey"},
    clear=False,
)
@patch("notify.urllib.request.urlopen")
def test_send_whatsapp_success(mock_urlopen):
    mock_urlopen.return_value.__enter__.return_value = MagicMock()
    assert notify.send_whatsapp("hello") is True
    called_url = mock_urlopen.call_args[0][0]
    assert "api.callmebot.com" in called_url
    assert "hello" in called_url


@patch.dict(os.environ, {}, clear=True)
def test_send_whatsapp_not_configured():
    assert notify.send_whatsapp("hello") is False
