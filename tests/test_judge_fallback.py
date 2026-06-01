"""Tests for the judge primary→local-fallback chain (CLAWP-041).

`claude -p` stays the default primary judge; a local model (Ollama by default)
is tried when the primary is UNAVAILABLE (not installed / non-zero exit). A
primary TIMEOUT does not fall back. The fallback is disableable and
configurable via CLAWPM_JUDGE_FALLBACK_CMD.
"""

from __future__ import annotations

import subprocess as _subprocess
import types

import pytest

from clawpm.judges import stop_condition as sc


# ---------------------------------------------------------------------------
# A fake subprocess.run keyed on the binary name (cmd[0]), so one fake can make
# the primary fail and the fallback succeed (or both fail).
# ---------------------------------------------------------------------------


class FakeRun:
    def __init__(self, behavior: dict):
        # behavior[cmd0] in {("ok", stdout), ("nonzero", stderr),
        #                     "notfound", "timeout"}
        self.behavior = behavior
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        b = self.behavior[cmd[0]]
        if b == "notfound":
            raise FileNotFoundError(cmd[0])
        if b == "timeout":
            raise _subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))
        kind, payload = b
        if kind == "ok":
            return _subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
        return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr=payload)

    @property
    def binaries(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def patch_subprocess(monkeypatch):
    """Install a FakeRun + matching TimeoutExpired into the module's
    ``subprocess`` reference, isolated to the test."""
    def _install(behavior):
        fake = FakeRun(behavior)
        ns = types.SimpleNamespace(
            run=fake, TimeoutExpired=_subprocess.TimeoutExpired
        )
        monkeypatch.setattr(sc, "subprocess", ns)
        return fake
    return _install


@pytest.fixture(autouse=True)
def clean_judge_env(monkeypatch):
    monkeypatch.delenv("CLAWPM_JUDGE_CMD", raising=False)
    monkeypatch.delenv("CLAWPM_JUDGE_FALLBACK_CMD", raising=False)


OK = ("ok", '{"ok": true, "reason": "done"}')


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_defaults(self):
        primary, fallback = sc._resolve_judge_cmds()
        assert primary == sc.DEFAULT_JUDGE_CMD
        assert fallback == sc.DEFAULT_JUDGE_FALLBACK_CMD

    def test_empty_fallback_env_disables(self, monkeypatch):
        monkeypatch.setenv("CLAWPM_JUDGE_FALLBACK_CMD", "")
        _, fallback = sc._resolve_judge_cmds()
        assert fallback is None

    def test_fallback_env_override(self, monkeypatch):
        monkeypatch.setenv("CLAWPM_JUDGE_FALLBACK_CMD", "ollama run qwen2.5")
        _, fallback = sc._resolve_judge_cmds()
        assert fallback == ["ollama", "run", "qwen2.5"]

    def test_primary_override_beats_env(self, monkeypatch):
        monkeypatch.setenv("CLAWPM_JUDGE_CMD", "claude -p --model env")
        primary, _ = sc._resolve_judge_cmds(judge_cmd_override="claude -p --model flag")
        assert primary == ["claude", "-p", "--model", "flag"]


# ---------------------------------------------------------------------------
# Fallback behaviour
# ---------------------------------------------------------------------------


class TestFallback:
    def test_primary_not_found_falls_back(self, patch_subprocess):
        fake = patch_subprocess({"claude": "notfound", "ollama": OK})
        out = sc.make_judge_invoker()("prompt")
        assert out == OK[1]
        assert fake.binaries == ["claude", "ollama"]

    def test_primary_nonzero_falls_back(self, patch_subprocess):
        fake = patch_subprocess({"claude": ("nonzero", "auth failed"), "ollama": OK})
        out = sc.make_judge_invoker()("prompt")
        assert out == OK[1]
        assert fake.binaries == ["claude", "ollama"]

    def test_primary_success_skips_fallback(self, patch_subprocess):
        fake = patch_subprocess({"claude": OK})
        out = sc.make_judge_invoker()("prompt")
        assert out == OK[1]
        assert fake.binaries == ["claude"]  # ollama never invoked

    def test_primary_timeout_does_not_fall_back(self, patch_subprocess):
        fake = patch_subprocess({"claude": "timeout"})
        with pytest.raises(sc.JudgeTimeout):
            sc.make_judge_invoker()("prompt")
        assert fake.binaries == ["claude"]  # no fallback on timeout

    def test_fallback_disabled_reraises(self, patch_subprocess, monkeypatch):
        monkeypatch.setenv("CLAWPM_JUDGE_FALLBACK_CMD", "")
        fake = patch_subprocess({"claude": "notfound"})
        with pytest.raises(RuntimeError, match="no fallback configured"):
            sc.make_judge_invoker()("prompt")
        assert fake.binaries == ["claude"]

    def test_both_fail_raises_combined(self, patch_subprocess):
        fake = patch_subprocess({"claude": "notfound", "ollama": "notfound"})
        with pytest.raises(RuntimeError, match="Both primary and fallback"):
            sc.make_judge_invoker()("prompt")
        assert fake.binaries == ["claude", "ollama"]

    def test_errors_subclass_runtimeerror(self):
        # Existing `except RuntimeError` handlers must keep catching these.
        assert issubclass(sc.JudgeUnavailable, RuntimeError)
        assert issubclass(sc.JudgeTimeout, RuntimeError)

    def test_fallback_used_by_evaluate_stop_condition(self, patch_subprocess):
        # End to end through the public judge entry: primary down → local
        # fallback grades → verdict parses.
        patch_subprocess({"claude": "notfound", "ollama": OK})
        verdict = sc.evaluate_stop_condition("rubric", "transcript")
        assert verdict.ok is True
        assert verdict.reason == "done"
