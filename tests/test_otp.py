"""One-time-code handling tests."""

from __future__ import annotations

import builtins

import pytest

from faym_returns import otp


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123456", "123456"),
        (" 123 456 ", "123456"),
        ("12-34-56", "123456"),
        ("OTP: 998877", "998877"),
        ("", None),
        ("   ", None),
        ("abcdef", None),
        (None, None),
    ],
)
def test_clean_keeps_only_digits(raw, expected):
    """Operators paste codes with spaces, dashes and the SMS prose around them."""
    assert otp.clean(raw) == expected


def test_console_provider_returns_the_typed_code(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *_: "123 456")
    code = otp.console_otp(platform="Flipkart", phone="9000000000")
    assert code == "123456"


def test_empty_input_means_fall_back_to_a_manual_sign_in(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *_: "")
    assert otp.console_otp(platform="Flipkart", phone="9000000000") is None


@pytest.mark.parametrize("boom", [EOFError, KeyboardInterrupt])
def test_interrupting_the_prompt_falls_back_rather_than_crashing(monkeypatch, boom):
    def raise_it(*_args):
        raise boom

    monkeypatch.setattr(builtins, "input", raise_it)
    assert otp.console_otp(platform="Flipkart", phone="9000000000") is None
