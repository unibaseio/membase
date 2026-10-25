"""The unibaseio-membase provider, loaded and driven through Hermes' own memory plumbing."""

from __future__ import annotations

import json
import logging
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

KEY = "mbk_test_owner_key"


def tool(mgr, name: str, **args: Any) -> dict[str, Any]:
    return json.loads(mgr.handle_tool_call(name, args))


def write_settings(home: Path, **values: Any) -> None:
    (home / "unibaseio-membase.json").write_text(json.dumps(values), encoding="utf-8")


# ---- registration and availability --------------------------------------------------------------


def test_hermes_discovers_and_loads_the_user_plugin(home, load):
    from plugins.memory import discover_memory_providers

    listed = {name: available for name, _desc, available in discover_memory_providers()}
    assert listed.get("unibaseio-membase") is True

    provider = load()
    assert type(provider).__name__ == "MembaseMemoryProvider"
    assert provider.name == "unibaseio-membase"
    assert {s["name"] for s in provider.get_tool_schemas()} == {"membase_search", "membase_save", "membase_profile"}


def test_unavailable_without_a_key(home, load, monkeypatch):
    monkeypatch.delenv("MEMBASE_API_KEY")
    provider = load()
    assert provider.is_available() is False
    assert "MEMBASE_API_KEY" in provider.unavailable_reason()


def test_tools_are_routed_by_the_manager(manager):
    mgr, _ = manager()
    assert {"membase_search", "membase_save", "membase_profile"} <= mgr.get_all_tool_names()
    assert "Membase" in mgr.build_system_prompt()


def test_key_comes_from_the_profile_secret_scope(manager, fake, monkeypatch):
    """Rule 11: the active profile's scoped secret wins over the process environment."""
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_some_other_profile")
    token = set_secret_scope({"MEMBASE_API_KEY": "mbk_scoped", "MEMBASE_BASE_URL": "https://membase.example.test"})
    try:
        mgr, _ = manager()
        tool(mgr, "membase_search", query="ledger")
    finally:
        reset_secret_scope(token)
    request = fake.calls("POST", "/v1/search")[0]
    assert request.headers["authorization"] == "Bearer mbk_scoped"
    assert request.url.host == "membase.example.test"


def test_a_key_rotated_under_a_running_session_is_used_on_the_next_call(manager, fake):
    """A host that hands each turn a fresh short-lived key (or an owner who rotates theirs) must
    not leave the session calling with the old one."""
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    token = set_secret_scope({"MEMBASE_API_KEY": "mbk_turn_one"})
    try:
        mgr, _ = manager()
        tool(mgr, "membase_search", query="ledger")
    finally:
        reset_secret_scope(token)
    token = set_secret_scope({"MEMBASE_API_KEY": "mbk_turn_two"})
    try:
        tool(mgr, "membase_search", query="ledger")
    finally:
        reset_secret_scope(token)
    auth = [r.headers["authorization"] for r in fake.calls("POST", "/v1/search")]
    assert auth == ["Bearer mbk_turn_one", "Bearer mbk_turn_two"]


def test_multiplexed_gateway_without_scope_is_unavailable_not_crashing(home, load):
    from agent.secret_scope import set_multiplex_active

    provider = load()
    set_multiplex_active(True)
    try:
        assert provider.is_available() is False
    finally:
        set_multiplex_active(False)


# ---- automatic recall (prefetch) ----------------------------------------------------------------


def test_prefetch_injects_passages_citing_their_memory(manager, fake):
    mgr, _ = manager()
    block = mgr.prefetch_all("what did we decide about the ledger database?", session_id="s-1")
    assert "- [Project decisions] We settled on Postgres for the ledger." in block
    assert "- [Travel] Window seat, always." in block
    assert "recalled 2 memories" in mgr.describe_recall()

    (search,) = fake.calls("POST", "/v1/search")
    assert search.url.host == "api.app.membase.com"
    assert search.headers["authorization"] == f"Bearer {KEY}"
    assert fake.body(search) == {"q": "what did we decide about the ledger database?", "limit": 5}
    assert fake.timeout(search) == 8.0  # the default recall budget


def test_prefetch_uses_the_configured_budget_and_default_container(manager, fake, home):
    write_settings(home, container="travel", recall_budget_seconds=2.5, recall_limit=3)
    mgr, _ = manager()
    mgr.prefetch_all("where do I like to sit on planes?")
    mgr.prefetch_all("and on trains?")
    assert len(fake.calls("GET", "/v1/containers")) == 1  # the name is resolved once, then cached
    searches = fake.calls("POST", "/v1/search")
    assert [fake.body(r)["container"] for r in searches] == ["mv-travel", "mv-travel"]
    assert fake.body(searches[0])["limit"] == 3
    assert fake.timeout(searches[0]) == 2.5


def test_recall_budget_never_exceeds_what_hermes_waits_for(manager, fake, home):
    write_settings(home, recall_budget_seconds=30)
    mgr, _ = manager()
    mgr.prefetch_all("what did we decide about the ledger?")
    assert fake.timeout(fake.calls("POST", "/v1/search")[0]) == 8.0


def test_prefetch_fails_open_on_a_cold_start_timeout(manager, fake, caplog):
    fake.fail["/v1/search"] = httpx.ReadTimeout("cold start")
    mgr, _ = manager()
    with caplog.at_level(logging.INFO):
        assert mgr.prefetch_all("what did we decide about the ledger?") == ""
    assert len(fake.calls("POST", "/v1/search")) == 1  # the recall client never retries
    assert "waking up" in caplog.text
    assert mgr.describe_recall() == ""


def test_a_slow_memory_never_holds_the_turn(manager, fake):
    """Past Hermes' prefetch bound the turn goes on without memory (the call finishes in the background)."""
    fake.delay["/v1/search"] = 1.5
    mgr, _ = manager(timeout=0.3)
    started = time.monotonic()
    assert mgr.prefetch_all("what did we decide about the ledger?") == ""
    assert time.monotonic() - started < 1.2


def test_prefetch_reports_memories_that_could_not_answer(manager, fake):
    fake.search_response = {"results": [], "containers": [
        {"id": "mv-decisions", "name": "Project decisions"},
        {"id": "mv-travel", "name": "Travel", "error": "no working model"},
    ]}
    mgr, _ = manager()
    block = mgr.prefetch_all("where do I like to sit?")
    assert "No passage matched this turn." in block
    assert "Travel (no working model)" in block
    assert mgr.describe_recall() == ""  # nothing recalled, but the gap is stated


def test_revoked_key_stops_automatic_recall_but_tools_still_explain(manager, fake):
    fake.fail["/v1/search"] = 403
    mgr, _ = manager()
    assert mgr.prefetch_all("anything about the ledger?") == ""
    assert mgr.prefetch_all("and about travel?") == ""
    assert len(fake.calls("POST", "/v1/search")) == 1  # not retried turn after turn
    err = tool(mgr, "membase_search", query="ledger")["error"]
    assert "narrowed or revoked" in err


def test_trivial_prompts_do_not_search(manager, fake):
    from agent.turn_context import _memory_turn_start_and_prefetch

    mgr, _ = manager()

    class Agent:
        _memory_manager = mgr
        _user_turn_count = 1
        session_id = "s-1"

        def _emit_status(self, _text):
            pass

    assert _memory_turn_start_and_prefetch(Agent(), "thanks!") == ""
    assert fake.calls("POST", "/v1/search") == []


def test_recall_can_be_switched_off(manager, fake, home):
    write_settings(home, auto_recall=False)
    mgr, _ = manager()
    assert mgr.prefetch_all("what did we decide about the ledger?") == ""
    assert fake.requests == []


# ---- tools --------------------------------------------------------------------------------------


def test_search_tool_names_containers_and_uses_the_sdk_timeout(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_search", query="ledger", container="Project decisions", limit=2)
    assert out["results"][0] == {"container": "Project decisions", "content": "We settled on Postgres for the ledger."}
    search = fake.calls("POST", "/v1/search")[0]
    assert fake.body(search) == {"q": "ledger", "container": "mv-decisions", "limit": 2}
    assert fake.timeout(search) == 90.0  # explicit calls wait out a cold start


def test_search_tool_reports_unavailable_memories(manager, fake):
    fake.search_response["containers"][1]["error"] = "dormant"
    mgr, _ = manager()
    out = tool(mgr, "membase_search", query="seat")
    assert out["unavailable"] == [{"container": "Travel", "error": "dormant"}]
    assert "prove nothing" in out["note"]


def test_save_tool_writes_a_note_to_the_named_memory(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_save", content="We moved the ledger launch to March.", container="project decisions")
    assert out["saved"] is True and out["status"] == "queued" and out["document_id"] == "srcitem_1"
    body = fake.body(fake.calls("POST", "/v1/memories")[0])
    assert body == {"content": "We moved the ledger launch to March.", "static": False, "title": "",
                    "container": "mv-decisions"}


def test_save_tool_records_a_standing_fact_in_the_profile(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_save", content="I prefer dark mode.", static=True)
    assert out["where"] == "profile" and out["status"] == "recorded"
    body = fake.body(fake.calls("POST", "/v1/memories")[0])
    assert body["static"] is True and "container" not in body


def test_a_fact_the_built_in_memory_tool_keeps_about_the_user_reaches_the_profile(manager, fake):
    """Hermes tells every provider when its built-in memory tool writes. What it records about
    the user is mirrored into their Membase profile; the agent's own notes and removals are not."""
    mgr, _ = manager()
    mgr.on_memory_write("add", "user", "Favourite city: Lisbon.")
    mgr.on_memory_write("add", "memory", "The deploy script lives in scripts/.")
    mgr.on_memory_write("remove", "user", "Favourite city: Lisbon.")
    (write,) = fake.calls("POST", "/v1/memories")
    assert fake.body(write) == {"content": "Favourite city: Lisbon.", "static": True, "title": ""}


def test_save_without_container_when_several_are_in_reach(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_save", content="Remember the ledger runs on Postgres.")
    assert "several Memories" in out["error"]
    assert out["available"] == ["Project decisions", "Travel"]


def test_save_with_a_read_only_key_is_refused_clearly(manager, fake):
    fake.fail["/v1/memories"] = lambda r: fake.error(
        403, "tool 'add_memory' is not in this agent's capability profile")
    mgr, _ = manager()
    err = tool(mgr, "membase_save", content="x", container="Travel")["error"]
    assert "403" in err and "narrowed or revoked" in err and "capability profile" in err
    assert KEY not in err


def test_unknown_container_lists_what_is_in_reach(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_search", query="x", container="Recipes")
    assert "No Memory named 'Recipes'" in out["error"]
    assert out["available"] == ["Project decisions", "Travel"]
    assert fake.calls("POST", "/v1/search") == []


def test_profile_tool(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_profile", query="travel")
    assert out["static"] == ["Works on the ledger service."]
    assert out["dynamic"] == ["Planning a trip to Lisbon."]
    assert fake.calls("GET", "/v1/profile")[0].url.params["q"] == "travel"


def test_profile_not_ticked_on_the_key(manager, fake):
    fake.fail["/v1/profile"] = 403
    mgr, _ = manager()
    assert "does not include the profile" in tool(mgr, "membase_profile")["error"]


def test_missing_arguments(manager, fake):
    mgr, _ = manager()
    assert tool(mgr, "membase_search", query=" ")["error"] == "query is required"
    assert tool(mgr, "membase_save", content="")["error"] == "content is required"
    assert fake.requests == []


# ---- capture (sync_turn) ------------------------------------------------------------------------


def test_turns_are_not_captured_by_default(manager, fake):
    mgr, _ = manager()
    mgr.sync_all("my passport expires in May", "Noted.", session_id="s-1")
    assert mgr.flush_pending(5)
    assert fake.calls("POST", "/v1/memories") == []


def test_capture_is_opt_in(manager, fake, home):
    write_settings(home, capture_turns="on", container="Travel")
    mgr, _ = manager()
    mgr.sync_all("my passport expires in May", "Noted, I will remind you in April.", session_id="s-1")
    assert mgr.flush_pending(5)
    mgr.sync_all("I prefer aisle seats on long flights", "Got it.", session_id="s-1")
    assert mgr.flush_pending(5)
    writes = fake.calls("POST", "/v1/memories")
    assert len(writes) == 2  # once per turn
    bodies = [fake.body(w) for w in writes]
    assert [b["content"] for b in bodies] == ["my passport expires in May", "I prefer aisle seats on long flights"]
    for body in bodies:  # the user's words only: no assistant text, no role labels
        assert body["container"] == "mv-travel" and body["static"] is False
        assert "remind you" not in body["content"] and "Got it" not in body["content"]
        assert "User:" not in body["content"] and "Assistant:" not in body["content"]


@pytest.mark.parametrize("context", ["cron", "subagent"])
def test_capture_skips_unattended_runs(manager, fake, home, context):
    write_settings(home, capture_turns=True, container="Travel")
    mgr, _ = manager(agent_context=context)
    mgr.sync_all("a scheduled report", "done", session_id="s-1")
    assert mgr.flush_pending(5)
    assert fake.calls("POST", "/v1/memories") == []


def test_capture_failure_is_logged_not_raised(manager, fake, home, caplog):
    write_settings(home, capture_turns=True)
    mgr, _ = manager()
    with caplog.at_level(logging.WARNING):
        mgr.sync_all("remember my seat preference", "ok", session_id="s-1")
        assert mgr.flush_pending(5)
    assert "Set `container`" in caplog.text


# ---- setup --------------------------------------------------------------------------------------


def test_config_schema_and_save_config(home, load):
    provider = load()
    schema = {f["key"]: f for f in provider.get_config_schema()}
    assert schema["api_key"]["secret"] is True and schema["api_key"]["env_var"] == "MEMBASE_API_KEY"
    assert {"base_url", "container", "recall_budget_seconds", "capture_turns"} <= set(schema)

    provider.save_config({"container": "Travel", "recall_budget_seconds": "5", "capture_turns": "on",
                          "api_key": "must-not-be-written"}, str(home))
    path = home / "unibaseio-membase.json"
    saved = json.loads(path.read_text())
    assert saved == {"capture_turns": True, "container": "Travel", "recall_budget_seconds": 5.0}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    status = provider.get_status_config({})
    assert status["api_key"] == "set" and status["capture_turns"] == "on"
    assert KEY not in json.dumps(status)


def test_shutdown_closes_the_clients(manager):
    mgr, provider = manager()
    clients = [provider._client, provider._recall_client]
    mgr.shutdown_all()
    assert all(c._http.is_closed for c in clients)


# ---- one key per person -------------------------------------------------------------------------


def test_client_for_routes_each_turn_to_its_author(manager, fake, load):
    from membase import Membase

    module = type(load())
    made: dict[str, Membase] = {}

    def client_for(user_id: str) -> Membase:
        made[user_id] = Membase(api_key=f"mbk_{user_id}")
        return made[user_id]

    mgr, provider = manager(provider=module(client_for=client_for), user_id="alice", platform="telegram")
    mgr.prefetch_all("what did we decide about the ledger?")
    provider.on_turn_start(2, "hi", author_id="bob")
    tool(mgr, "membase_search", query="seat")
    bearers = [r.headers["authorization"] for r in fake.calls("POST", "/v1/search")]
    assert bearers == ["Bearer mbk_alice", "Bearer mbk_bob"]


def test_client_for_without_a_user_id_uses_no_ones_memory(manager, fake, load):
    from membase import Membase

    module = type(load())
    mgr, _ = manager(provider=module(client_for=lambda uid: Membase(api_key=f"mbk_{uid}")))
    assert mgr.prefetch_all("what did we decide about the ledger?") == ""
    assert "no key for the person" in tool(mgr, "membase_search", query="x")["error"]
    assert fake.requests == []


def test_runs_in_the_plugin_host(home, load):
    """With plugins.isolation: host the provider's code runs in Hermes' plugin-host process."""
    from agent.memory_manager import MemoryManager

    (home / "config.yaml").write_text("memory:\n  provider: unibaseio-membase\nplugins:\n  isolation: host\n", encoding="utf-8")
    provider = load()
    assert type(provider).__name__.startswith("Hosted")  # a proxy; the instance lives in the host
    assert provider.is_available()
    mgr = MemoryManager()
    mgr.add_provider(provider)
    mgr.initialize_all(session_id="s-1", platform="cli", hermes_home=str(home))
    try:
        assert {"membase_search", "membase_save", "membase_profile"} <= mgr.get_all_tool_names()
        assert json.loads(mgr.handle_tool_call("membase_search", {"query": ""})) == {"error": "query is required"}
    finally:
        mgr.shutdown_all()
