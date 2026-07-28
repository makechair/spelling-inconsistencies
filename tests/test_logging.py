"""Secret redaction in logs (spec 4.5, spec-review D-8)."""

from __future__ import annotations

import io
import logging

from usstocks.logging_setup import configure_logging, redact, register_secret


def test_redacts_key_value_secrets():
    assert "sk_live_abcdef123456" not in redact("token=sk_live_abcdef123456")
    assert "***" in redact("token=sk_live_abcdef123456")
    assert "hunter2hunter2" not in redact('{"apiKey": "hunter2hunter2"}')
    assert "supersecretvalue" not in redact("Authorization: supersecretvalue")


def test_redacts_bearer_tokens():
    cleaned = redact("Authorization: Bearer abc123DEF456ghi789")
    assert "abc123DEF456ghi789" not in cleaned


def test_redacts_jwt_shaped_values():
    token = "eyJhbGciOiJSUzI1NiJ9.eyJlbWFpbCI6ImFAYi5jb20ifQ.c2lnbmF0dXJlaGVyZQ"
    cleaned = redact(f"verifying {token} now")
    assert token not in cleaned
    assert "<jwt>" in cleaned


def test_registered_literals_are_masked_anywhere():
    register_secret("my-tiingo-key-value")
    assert "my-tiingo-key-value" not in redact("GET /prices?token=my-tiingo-key-value")
    # Even in a form the patterns would not catch.
    assert "my-tiingo-key-value" not in redact("connecting with my-tiingo-key-value ok")


def test_short_values_are_not_registered():
    register_secret("abc")
    assert "abcdefgh" in redact("value abcdefgh")


def test_filter_is_installed_on_the_root_logger():
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("test").info("connecting with token=verysecretvalue123")
    output = stream.getvalue()
    assert "verysecretvalue123" not in output
    assert "***" in output


def test_ordinary_messages_pass_through():
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("test").info("backfilled AAPL: 30 bars")
    assert "backfilled AAPL: 30 bars" in stream.getvalue()
