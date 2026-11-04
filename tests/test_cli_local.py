"""The local-only commands that used to lived in the engine's CLI: import and agent."""

from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("membase_core", reason="needs the local extra")

from membase import cli  # noqa: E402
from membase.local.backend import LocalBackend  # noqa: E402


def test_import_scans_directories_and_dry_runs(tmp_path, capsys):
    chats = tmp_path / "chats" / "nested"
    chats.mkdir(parents=True)
    (chats / "a.md").write_text("**User:** I moved to Lisbon.\n**Assistant:** Noted.\n")
    (chats / "notes.bin").write_bytes(b"\x00")
    assert cli.main(["--store", str(tmp_path / "s"), "import", str(tmp_path / "chats"), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["would_import"] == 1 and out["files"][0]["format"] == "markdown"
    assert not (tmp_path / "s" / "memory.db").exists()


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._lock = threading.RLock()

    def ingest_agent_trace(self, messages, *, agent_id, session_id=None):
        self.calls.append(("ingest", len(messages), agent_id, session_id))
        return {"cases": 1, "skills_added": 1, "status": "extracted"}

    def search_agent(self, q, *, agent_id, kind, top_k):
        self.calls.append(("search", q, agent_id, kind, top_k))
        return {"cases": [], "skills": [{"id": f"{agent_id}_Deploy_fix"}]}

    def list_skills(self, agent_id):
        return [{"id": f"{agent_id}_Deploy_fix"}]

    def save(self) -> None:
        pass


def test_agent_commands(tmp_path, capsys, monkeypatch):
    fake = _FakeEngine()
    monkeypatch.setattr(LocalBackend, "engine", lambda self, cid: (fake, fake._lock))
    trace = tmp_path / "fix-deploy.json"
    trace.write_text(json.dumps({"messages": [{"role": "user", "content": "deploy the hotfix"}]}))
    store = ["--store", str(tmp_path / "s")]
    assert cli.main([*store, "agent", "ingest", str(trace), "--agent", "coder"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "extracted"
    assert fake.calls[0] == ("ingest", 1, "coder", "fix-deploy")
    assert cli.main([*store, "agent", "search", "deploy", "--agent", "coder", "--kind", "skills"]) == 0
    assert json.loads(capsys.readouterr().out)["skills"][0]["id"] == "coder_Deploy_fix"
    assert cli.main([*store, "agent", "skills", "--agent", "coder"]) == 0
    assert json.loads(capsys.readouterr().out)["skills"][0]["id"] == "coder_Deploy_fix"


def test_cli_speaks_the_memory_api(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MEMBASE_WRITE_TARGET", raising=False)
    store = ["--store", str(tmp_path / "s")]
    assert cli.main([*store, "memories", "list", "--limit", "5"]) == 0
    assert json.loads(capsys.readouterr().out) == {"items": [], "has_more": False, "partial": []}
    assert cli.main([*store, "documents", "list", "--kind", "file"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    assert cli.main([*store, "capabilities"]) == 0
    assert "memories.add" in json.loads(capsys.readouterr().out)["actions"]
    assert cli.main([*store, "profile-facts", "add", "Prefers dark mode."]) == 0
    fact = json.loads(capsys.readouterr().out)
    assert fact["id"].startswith("pf_") and fact["created"] is True
    assert cli.main([*store, "profile-facts", "list", "--limit", "5"]) == 0
    assert [f["id"] for f in json.loads(capsys.readouterr().out)["items"]] == [fact["id"]]
    assert cli.main([*store, "profile-facts", "forget", fact["id"]]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "confirmation_required"
    assert cli.main([*store, "profile-facts", "forget", fact["id"], "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": fact["id"], "status": "forgotten"}
    for retired in (["containers"], ["search", "x", "--container", "a"], ["add", "x", "--static"],
                    ["profile"], ["rules"]):
        with pytest.raises(SystemExit):
            cli.main([*store, *retired])
        capsys.readouterr()


def test_a_removal_without_confirm_shows_what_would_go(tmp_path, capsys, monkeypatch):
    fake = _FakeEngine()
    fake.episodes = lambda limit=20, owner="": [{"id": 1, "content": "Prefers tea", "created_at": "",
                                                 "title": "", "session_date": "", "session_id": "s"}]
    monkeypatch.setattr(LocalBackend, "engine", lambda self, cid: (fake, fake._lock))
    from membase.local import ids

    store = ["--store", str(tmp_path / "s")]
    LocalBackend(tmp_path / "s")._create("default")
    mid = ids.encode("memory", "default:1")
    assert cli.main([*store, "forget", mid]) == 1
    captured = capsys.readouterr()
    error = json.loads(captured.out)["error"]
    assert error["code"] == "confirmation_required" and error["details"]["content"] == "Prefers tea"
    assert captured.err.startswith("error: ")


def test_agent_memory_is_local_only(monkeypatch, capsys):
    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_test")
    monkeypatch.delenv("MEMBASE_LOCAL", raising=False)
    with pytest.raises(SystemExit, match="agent memory is local"):
        cli.main(["agent", "skills", "--agent", "coder"])
