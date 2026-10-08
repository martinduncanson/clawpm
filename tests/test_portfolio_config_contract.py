"""CLAWP-094 — load_portfolio_config fail-open default contract.

Absence of a portfolio is a legitimate fresh-install state and falls back to
defaults. An EXPLICIT operator pointer (CLAWPM_PORTFOLIO / portfolio_path arg)
that does not resolve to a portfolio is a degraded path and must be marked.
A malformed portfolio.toml must stay loud (raise), never fall through.
"""

from __future__ import annotations

import logging

import pytest

from clawpm import discovery


@pytest.fixture(autouse=True)
def _reset_warned():
    discovery._PORTFOLIO_FALLBACK_WARNED.clear()
    yield
    discovery._PORTFOLIO_FALLBACK_WARNED.clear()


def test_env_pointing_at_missing_dir_warns_and_uses_defaults(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path / "nope"))
    with caplog.at_level(logging.WARNING, logger="clawpm.discovery"):
        cfg = discovery.load_portfolio_config()
    assert cfg is not None  # contract: defaults still returned
    assert any("CLAWPM_PORTFOLIO" in r.getMessage() for r in caplog.records), caplog.text


def test_explicit_path_without_portfolio_toml_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("CLAWPM_PORTFOLIO", raising=False)
    with caplog.at_level(logging.WARNING, logger="clawpm.discovery"):
        cfg = discovery.load_portfolio_config(tmp_path)
    assert cfg is not None
    assert any("portfolio.toml" in r.getMessage() for r in caplog.records), caplog.text


def test_warning_is_deduplicated(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path / "nope"))
    with caplog.at_level(logging.WARNING, logger="clawpm.discovery"):
        discovery.load_portfolio_config()
        discovery.load_portfolio_config()
    assert len([r for r in caplog.records if "CLAWPM_PORTFOLIO" in r.getMessage()]) == 1


def test_valid_portfolio_emits_no_warning(isolated_portfolio, caplog):
    with caplog.at_level(logging.WARNING, logger="clawpm.discovery"):
        discovery.load_portfolio_config()
    assert not [r for r in caplog.records if "portfolio" in r.getMessage().lower()]


def test_malformed_portfolio_toml_stays_loud(tmp_path, monkeypatch):
    (tmp_path / "portfolio.toml").write_text("not = = toml [[", encoding="utf-8")
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    with pytest.raises(Exception):
        discovery.load_portfolio_config()
