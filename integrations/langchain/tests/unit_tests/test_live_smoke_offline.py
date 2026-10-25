"""examples/live_smoke.py itself, run against the offline API so the script cannot rot."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from .conftest import FakeMembase

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "live_smoke.py"


def load_smoke():
    spec = importlib.util.spec_from_file_location("live_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skips_without_a_key(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("MEMBASE_API_KEY", raising=False)
    assert load_smoke().main() == 0
    assert "skipping" in capsys.readouterr().out


def test_runs_end_to_end_offline(
    fake: FakeMembase, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_smoke")
    monkeypatch.setenv("MEMBASE_SMOKE_WRITE", "1")
    monkeypatch.setenv("MEMBASE_SMOKE_CONTAINER", "Reading notes")
    smoke = load_smoke()
    monkeypatch.setattr(smoke, "Membase", lambda **_: fake.client("mbk_smoke"))
    assert smoke.main() == 0
    out = capsys.readouterr().out
    assert "retriever: 5 documents" in out
    assert "[Project decisions]" in out
    assert "middleware: recall injected=True" in out
    assert "save tool: Saved to Reading notes." in out
    assert fake.requests[-1]["body"]["container"] == "mv-reading"
