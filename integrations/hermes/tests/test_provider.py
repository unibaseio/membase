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
    assert fake.body(search) == {"q": "what did we decide about the ledger database?", "mode": "full", "limit": 5}
    assert fake.timeout(search) == 8.0  # the default recall budget


def test_prefetch_uses_the_configured_budget_and_names_no_memory(manager, fake, home):
    write_settings(home, recall_budget_seconds=2.5, recall_limit=3)
    mgr, _ = manager()
    mgr.prefetch_all("where do I like to sit on planes?")
    mgr.prefetch_all("and on trains?")
    searches = fake.calls("POST", "/v1/search")
    assert all("container" not in fake.body(r) for r in searches)
    assert fake.body(searches[0])["limit"] == 3
    assert fake.timeout(searches[0]) == 2.5


def test_a_retired_container_setting_is_called_out(manager, fake, home, caplog):
    write_settings(home, container="Travel")
    with caplog.at_level(logging.WARNING):
        mgr, _ = manager()
        mgr.prefetch_all("where do I like to sit?")
    assert "`container` setting (and MEMBASE_CONTAINER) is no longer used" in caplog.text
    assert "container" not in fake.body(fake.calls("POST", "/v1/search")[0])


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
    fake.search_response = {"items": [], "partial": [
        {"memory_name": "Travel", "reason": "error"},
        {"memory_name": "Project decisions", "reason": "truncated"},
    ]}
    mgr, _ = manager()
    block = mgr.prefetch_all("where do I like to sit?")
    assert "No passage matched this turn." in block
    assert "absence of a passage proves nothing for: Travel" in block
    assert "Project decisions" not in block  # truncated: it answered, its passages lost to the limit
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


def test_search_tool_names_memories_and_uses_the_sdk_timeout(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_search", query="ledger", limit=2)
    assert out["results"][0] == {"memory": "Project decisions", "content": "We settled on Postgres for the ledger."}
    search = fake.calls("POST", "/v1/search")[0]
    assert fake.body(search) == {"q": "ledger", "mode": "full", "limit": 2}
    assert fake.timeout(search) == 90.0  # explicit calls wait out a cold start


def test_search_tool_reports_unavailable_memories(manager, fake):
    fake.search_response["partial"] = [{"memory_name": "Travel", "reason": "error"}]
    mgr, _ = manager()
    out = tool(mgr, "membase_search", query="seat")
    assert out["unavailable"] == [{"memory": "Travel"}]
    assert "prove nothing" in out["note"]


def test_no_tool_takes_a_memory_argument(manager):
    mgr, provider = manager()
    for schema in provider.get_tool_schemas():
        assert not {"container", "static"} & set(schema["parameters"].get("properties", {})), schema["name"]


def test_save_tool_writes_a_note_to_the_write_target(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_save", content="We moved the ledger launch to March.")
    assert out["saved"] is True and out["status"] == "processing" and out["id"] == "doc_bm90ZQ"
    assert out["memory"] == "Project decisions"
    body = fake.body(fake.calls("POST", "/v1/memories/add")[0])
    assert body == {"content": "We moved the ledger launch to March.", "title": ""}


def test_save_when_the_key_has_no_write_target(manager, fake):
    fake.write_target = None
    mgr, _ = manager()
    err = tool(mgr, "membase_save", content="Remember the ledger runs on Postgres.")["error"]
    assert "several Memories and has no write target, so nothing was saved" in err
    assert "Connect > Developer keys" in err


def test_built_in_memory_writes_are_not_mirrored(manager, fake):
    """The memory API has no standing-fact write, so what the built-in tool keeps stays Hermes's own."""
    mgr, _ = manager()
    mgr.on_memory_write("add", "user", "Favourite city: Lisbon.")
    assert fake.calls("POST", "/v1/memories/add") == []


def test_save_with_a_read_only_key_is_refused_clearly(manager, fake):
    fake.fail["/v1/memories/add"] = lambda r: fake.error(
        403, "tool 'add_memory' is not in this agent's capability profile")
    mgr, _ = manager()
    err = tool(mgr, "membase_save", content="x")["error"]
    assert "403" in err and "narrowed or revoked" in err and "capability profile" in err
    assert KEY not in err


def test_profile_tool(manager, fake):
    mgr, _ = manager()
    out = tool(mgr, "membase_profile")
    assert out == {"facts": ["Works on the ledger service.", "Planning a trip to Lisbon."]}
    assert len(fake.calls("GET", "/v1/profile-facts")) == 1


def test_profile_not_ticked_on_the_key(manager, fake):
    fake.fail["/v1/profile-facts"] = 403
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
    assert fake.calls("POST", "/v1/memories/add") == []


def test_capture_is_opt_in(manager, fake, home):
    write_settings(home, capture_turns="on")
    mgr, _ = manager()
    mgr.sync_all("my passport expires in May", "Noted, I will remind you in April.", session_id="s-1")
    assert mgr.flush_pending(5)
    mgr.sync_all("I prefer aisle seats on long flights", "Got it.", session_id="s-1")
    assert mgr.flush_pending(5)
    writes = fake.calls("POST", "/v1/memories/add")
    assert len(writes) == 2  # once per turn
    bodies = [fake.body(w) for w in writes]
    assert [b["content"] for b in bodies] == ["my passport expires in May", "I prefer aisle seats on long flights"]
    for body in bodies:  # the user's words only: no assistant text, no role labels
        assert set(body) == {"content", "title"}
        assert "remind you" not in body["content"] and "Got it" not in body["content"]
        assert "User:" not in body["content"] and "Assistant:" not in body["content"]


@pytest.mark.parametrize("context", ["cron", "subagent"])
def test_capture_skips_unattended_runs(manager, fake, home, context):
    write_settings(home, capture_turns=True)
    mgr, _ = manager(agent_context=context)
    mgr.sync_all("a scheduled report", "done", session_id="s-1")
    assert mgr.flush_pending(5)
    assert fake.calls("POST", "/v1/memories/add") == []


def test_capture_failure_is_logged_not_raised(manager, fake, home, caplog):
    write_settings(home, capture_turns=True)
    fake.write_target = None
    mgr, _ = manager()
    with caplog.at_level(logging.WARNING):
        mgr.sync_all("remember my seat preference", "ok", session_id="s-1")
        assert mgr.flush_pending(5)
    assert "no write target" in caplog.text


# ---- setup --------------------------------------------------------------------------------------


def test_config_schema_and_save_config(home, load):
    provider = load()
    schema = {f["key"]: f for f in provider.get_config_schema()}
    assert schema["api_key"]["secret"] is True and schema["api_key"]["env_var"] == "MEMBASE_API_KEY"
    assert {"base_url", "recall_budget_seconds", "capture_turns"} <= set(schema)
    assert "container" not in schema

    provider.save_config({"container": "Travel", "recall_budget_seconds": "5", "capture_turns": "on",
                          "api_key": "must-not-be-written"}, str(home))
    path = home / "unibaseio-membase.json"
    saved = json.loads(path.read_text())
    assert saved == {"capture_turns": True, "recall_budget_seconds": 5.0}
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



# ---- a host that leaves memory to prefetch (tools off, fast recall, the profile each turn) --------


def user_store_provider(home: Path) -> None:
    (home / "config.yaml").write_text("memory:\n  provider: unibaseio-membase\n  user_store: provider\n",
                                      encoding="utf-8")


def test_tools_can_be_switched_off(manager, monkeypatch):
    """Memory left to prefetch: no turn spends a model call deciding whether to search or save."""
    monkeypatch.setenv("MEMBASE_TOOLS", "off")
    mgr, provider = manager()
    assert provider.get_tool_schemas() == []
    assert "membase_search" not in mgr.build_system_prompt()


def test_fast_recall_is_one_round_with_more_passages(manager, fake, monkeypatch):
    monkeypatch.setenv("MEMBASE_RECALL_MODE", "fast")
    mgr, _ = manager()
    mgr.prefetch_all("what did we decide about the ledger database?")
    (search,) = fake.calls("POST", "/v1/search")
    assert fake.body(search)["mode"] == "fast"
    assert fake.body(search)["limit"] == 10, "the agent's own model judges more passages"


def test_the_profile_is_recalled_beside_the_search_when_the_provider_keeps_it(manager, fake, home):
    user_store_provider(home)
    mgr, _ = manager()
    block = mgr.prefetch_all("what did we decide about the ledger database?")
    assert "the user's profile" in block and "- Works on the ledger service." in block
    assert "- [Project decisions] We settled on Postgres for the ledger." in block
    assert len(fake.calls("GET", "/v1/profile-facts")) == 1
    assert "injected before each turn as data" in mgr.build_system_prompt()


def test_a_profile_that_could_not_be_read_says_so(manager, fake, home):
    user_store_provider(home)
    fake.fail["/v1/profile-facts"] = 403
    mgr, _ = manager()
    block = mgr.prefetch_all("anything about me?")
    assert "Not read this turn (this key may not read it)" in block
    assert "do not assume it is empty" in block


def test_the_profile_is_not_read_each_turn_by_default(manager, fake):
    mgr, _ = manager()
    mgr.prefetch_all("what did we decide about the ledger database?")
    assert fake.calls("GET", "/v1/profile-facts") == []


def test_a_fact_about_the_user_goes_to_the_profile(manager, fake, home):
    user_store_provider(home)
    _, provider = manager()
    out = provider.record_user_fact("  Prefers tea to coffee. ")
    assert out == {"recorded": True, "id": "pf_Mw", "created": True}
    (add,) = fake.calls("POST", "/v1/profile-facts/add")
    assert fake.body(add) == {"content": "Prefers tea to coffee."}


@pytest.mark.parametrize("context", ["cron", "subagent"])
def test_an_unattended_run_records_no_fact_about_the_user(manager, fake, context):
    _, provider = manager(agent_context=context)
    with pytest.raises(RuntimeError, match="does not write"):
        provider.record_user_fact("Prefers tea.")
    assert fake.calls("POST", "/v1/profile-facts/add") == []


def test_a_refused_fact_raises_one_actionable_sentence(manager, fake):
    fake.fail["/v1/profile-facts/add"] = 403
    _, provider = manager()
    with pytest.raises(RuntimeError, match="does not include the profile"):
        provider.record_user_fact("Prefers tea.")

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
