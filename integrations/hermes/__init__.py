"""Membase memory provider for Hermes Agent.

Membase (https://membase.com) is a hosted memory API: one API key is one person's account, and
that person's Memories (containers) hold what their memory agent learned. This provider connects a
Hermes profile to it:

- prefetch: a time-boxed search before each turn (default 8 s, never retried). A miss, a timeout
  or any error logs and continues with no recall; the first search after a quiet spell can take
  up to a minute while the account's memory wakes. Every passage names the Memory it came from.
- Tools: membase_search, membase_save (only when the user asks), membase_profile.
- sync_turn: opt-in capture of the user's own words, once per completed turn (``capture_turns``,
  default off), primary agent only. The assistant's reply is never sent.

Settings live in $HERMES_HOME/unibaseio-membase.json; the key (MEMBASE_API_KEY) and the optional
MEMBASE_BASE_URL / MEMBASE_CONTAINER are read through the Hermes secret scope of the active profile.
The only network peer is the Membase API (https://api.app.membase.com unless overridden).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agent.memory_provider import MemoryProvider

try:  # Hermes >= 0.22; older builds have no recall status line, which is only cosmetic
    from agent.memory_provider import RecallStatus
except ImportError:  # pragma: no cover - depends on the Hermes build
    RecallStatus = None
from agent.secret_scope import get_secret

try:  # declared in pyproject.toml; Hermes installs it with the plugin
    import membase
    from membase import Membase
except ImportError:  # pragma: no cover - exercised only before the dependency is installed
    membase = None  # type: ignore[assignment]
    Membase = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "0.1.0"
CONFIG_FILE = "unibaseio-membase.json"
KEY_URL = "https://app.membase.com"  # Connect > Developer keys
# The plugin's own default: it does not depend on which SDK release is installed.
DEFAULT_BASE_URL = "https://api.app.membase.com"

DEFAULTS: dict[str, Any] = {
    "base_url": "",                  # empty: MEMBASE_BASE_URL, else DEFAULT_BASE_URL
    "container": "",                 # empty: MEMBASE_CONTAINER, else every Memory the key reaches
    "auto_recall": True,             # search before each turn
    "recall_budget_seconds": 8.0,    # per-turn recall budget; fail open past it
    "recall_limit": 5,               # passages injected per turn
    "recall_max_chars": 4000,        # ceiling on the injected block
    "capture_turns": False,          # save each completed turn as a note (opt-in)
}
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}
_PASSAGE_CHARS = 1200
_CAPTURE_CHARS = 8000
_NON_PRIMARY = {"cron", "subagent", "flush"}
HERMES_PREFETCH_CAP = 8.0  # agent.memory_manager._EXTERNAL_PREFETCH_TIMEOUT_S

SEARCH_SCHEMA = {
    "name": "membase_search",
    "description": (
        "Search the user's long-term memory in Membase: passages their Memories hold on a question, most "
        "relevant first, each naming the Memory (container) it came from. Use it when the answer may depend "
        "on what the user told you or their material before; cite the Memory name when you use a passage. "
        "An empty result with entries under 'unavailable' means those Memories could not be searched, not "
        "that nothing is remembered."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The question, in natural language."},
            "container": {"type": "string",
                          "description": "Optional Memory name or id to search; omit to search every Memory in reach."},
            "limit": {"type": "integer", "description": "Passages to return in all, 1-50."},
        },
        "required": ["query"],
    },
}

SAVE_SCHEMA = {
    "name": "membase_save",
    "description": (
        "Save something to the user's Membase memory. Call it ONLY when the user asks you to remember "
        "something or plainly means it, and save it in the user's own words; never save your own answers, "
        "guesses or routine chatter. static=true records a standing fact about the user (who they are, a "
        "lasting preference) in their profile; otherwise the note goes to a Memory, which learns it on its "
        "next run (it is searchable after that, not instantly)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "What to remember, in the user's words."},
            "container": {"type": "string",
                          "description": "Memory name or id for the note. Needed when the key reaches several "
                                         "Memories and no default is configured; ignored when static is true."},
            "static": {"type": "boolean",
                       "description": "true: a standing fact about the user, recorded in their profile."},
        },
        "required": ["content"],
    },
}

PROFILE_SCHEMA = {
    "name": "membase_profile",
    "description": (
        "Read the user's Membase profile: standing facts about them ('static') and the most recently "
        "changed ones ('dynamic'); with a query, also passages relevant to it. Read it once near the start "
        "of a conversation when knowing the person matters."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Optional topic to focus 'results' on."}},
    },
}


# ---------------------------------------------------------------------------------------------
# configuration


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    return default


def _as_number(value: Any, default: float, lo: float, hi: float, cast: Callable[[Any], Any]) -> Any:
    try:
        return max(lo, min(hi, cast(value)))
    except (TypeError, ValueError):
        return default


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg["base_url"] = str(raw.get("base_url") or "").strip().rstrip("/")
    cfg["container"] = str(raw.get("container") or "").strip()
    cfg["auto_recall"] = _as_bool(raw.get("auto_recall"), DEFAULTS["auto_recall"])
    cfg["capture_turns"] = _as_bool(raw.get("capture_turns"), DEFAULTS["capture_turns"])
    # Hermes stops waiting for an external provider's recall after 8 s, so a longer budget buys nothing.
    cfg["recall_budget_seconds"] = _as_number(raw.get("recall_budget_seconds", DEFAULTS["recall_budget_seconds"]),
                                              DEFAULTS["recall_budget_seconds"], 0.5, HERMES_PREFETCH_CAP, float)
    cfg["recall_limit"] = _as_number(raw.get("recall_limit", DEFAULTS["recall_limit"]),
                                     DEFAULTS["recall_limit"], 1, 50, int)
    cfg["recall_max_chars"] = _as_number(raw.get("recall_max_chars", DEFAULTS["recall_max_chars"]),
                                         DEFAULTS["recall_max_chars"], 500, 50000, int)
    return cfg


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    """Hermes's own reader when it has one (`utils.read_json_or_empty`), else the same thing here:
    a missing or unreadable file is an empty config, never an error."""
    try:
        from utils import read_json_or_empty
    except ImportError:  # pragma: no cover - older Hermes builds
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
    return read_json_or_empty(path)


def load_config(hermes_home: Optional[str]) -> dict[str, Any]:
    """Defaults overlaid with $hermes_home/unibaseio-membase.json; unparseable values fall back to defaults."""
    raw: dict[str, Any] = {}
    if hermes_home:
        raw = _read_json_or_empty(Path(hermes_home) / CONFIG_FILE)
    return _normalize(raw)


def _secret(name: str) -> str:
    """A profile-scoped secret; '' when unset or when no profile scope is active (fail closed)."""
    try:
        return (get_secret(name, "") or "").strip()
    except Exception:  # UnscopedSecretError under a multiplexed gateway with no scope
        return ""


# ---------------------------------------------------------------------------------------------
# formatting and errors


def _container_label(item: dict[str, Any]) -> str:
    return str(item.get("container_name") or item.get("name") or item.get("container") or item.get("id") or "Membase")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_recall(response: dict[str, Any], *, limit: int, max_chars: int) -> tuple[str, int]:
    """The prefetch block (passages tagged with their Memory) and how many passages it carries."""
    lines: list[str] = []
    used = 0
    for item in (response or {}).get("results") or []:
        content = str((item or {}).get("content") or "").strip()
        if not content:
            continue
        body = _clip(content, _PASSAGE_CHARS).replace("\n", "\n  ")
        line = f"- [{_container_label(item)}] {body}"
        if used + len(line) > max_chars and lines:
            break
        lines.append(line)
        used += len(line) + 1
        if len(lines) >= limit:
            break
    unavailable = [f"{_container_label(c)} ({c.get('error')})"
                   for c in (response or {}).get("containers") or [] if isinstance(c, dict) and c.get("error")]
    if not lines and not unavailable:
        return "", 0
    out = ["## Membase: the user's long-term memory (each passage names the Memory it came from)"]
    out += lines or ["No passage matched this turn."]
    if unavailable:
        out.append("Not searched this turn, so absence of a passage proves nothing for: " + "; ".join(unavailable))
    return "\n".join(out), len(lines)


def _tool_error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message[:1000], **extra}, ensure_ascii=False)


def explain_error(exc: BaseException, *, timeout: Optional[float] = None, action: str = "") -> str:
    """One actionable sentence for a Membase failure (the token never appears: the SDK does not echo it)."""
    errors = membase  # None only before the dependency is installed
    server = getattr(exc, "message", "") or str(exc)
    if errors is not None and isinstance(exc, errors.PermissionDeniedError):
        extra = " or the key does not include the profile" if action == "profile" else ""
        return ("Membase refused this key (403): its owner narrowed or revoked it, or this action is above its "
                f"access level{extra}. Do not retry with this key; the owner can check Connect > Developer keys "
                f"in the Membase app. Server said: {server}")
    if errors is not None and isinstance(exc, errors.AuthenticationError):
        return "Membase got no API key (401): set MEMBASE_API_KEY for this profile (hermes memory setup)."
    if errors is not None and isinstance(exc, errors.APITimeoutError):
        wait = f" within {timeout:g} s" if timeout else ""
        return (f"Membase did not answer{wait}. The user's memory may be waking up after a quiet spell (this can "
                "take up to a minute); try again shortly.")
    if errors is not None and isinstance(exc, errors.APIConnectionError):
        return f"Could not reach Membase: {server}"
    if errors is not None and isinstance(exc, errors.RateLimitError):
        return f"Membase is busy for this account right now (429): {server}. Try again in a little while."
    if errors is not None and isinstance(exc, errors.UnprocessableEntityError):
        code = getattr(exc, "code", "")
        return (f"Membase could not run this on the user's memory (422{' ' + code if code else ''}): {server}. "
                "The account may have no working model under AI Setup, or its free turns are spent.")
    if errors is not None and isinstance(exc, errors.APIStatusError):
        return f"Membase answered {getattr(exc, 'status', '?')}: {server}"
    return f"Membase call failed: {exc}"


class UnknownContainer(ValueError):
    def __init__(self, value: str, names: list[str]):
        super().__init__(f"No Memory named {value!r} is in this key's reach. Available: "
                         + (", ".join(names) if names else "none"))
        self.names = names


# ---------------------------------------------------------------------------------------------
# provider


class MembaseMemoryProvider(MemoryProvider):
    """Hermes memory provider backed by the Membase SDK.

    ``client`` (a ready ``membase.Membase``) or ``client_for`` (user id -> ``Membase``, one key or
    local directory per person) are for code that registers this provider itself; the catalog
    install constructs it with neither and uses the profile's MEMBASE_API_KEY: one key, one person.
    """

    def __init__(self, client: Any = None, *, client_for: Optional[Callable[[str], Any]] = None) -> None:
        if client is not None and client_for is not None:
            raise ValueError("pass client or client_for, not both")
        self._given_client = client
        self._client_for = client_for
        self._cfg = _normalize({})
        self._client: Any = None          # explicit calls: SDK default timeout and retries
        self._recall_client: Any = None   # prefetch: recall budget, no retries
        self._owns_clients = False
        self._per_user: dict[str, Any] = {}
        self._containers: dict[int, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._session_user = ""
        self._turn_user = ""
        self._writes = True
        self._active = False
        self._error = ""
        self._last_count = 0
        self._recall_refused = False
        self._capture_warned = False

    # ---- identity and availability --------------------------------------------------------

    @property
    def name(self) -> str:
        return "unibaseio-membase"

    def is_available(self) -> bool:
        if Membase is None:
            return False
        if self._given_client is not None or self._client_for is not None:
            return True
        return bool(_secret("MEMBASE_API_KEY"))

    def unavailable_reason(self) -> str:
        if Membase is None:
            return "the unibaseio-membase package is not installed; reinstall the plugin (hermes plugins install unibaseio-membase)"
        return (f"MEMBASE_API_KEY is not set for this profile: run `hermes memory setup` and paste a developer key "
                f"from {KEY_URL} (Connect > Developer keys)")

    # ---- setup ------------------------------------------------------------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "api_key", "description": "Membase API key (Connect > Developer keys; Read & write to save)",
             "secret": True, "required": True, "env_var": "MEMBASE_API_KEY", "url": KEY_URL},
            {"key": "base_url", "description": "Membase API URL (blank: https://api.app.membase.com)", "default": ""},
            {"key": "container",
             "description": "Default Memory name or id for recall and saves (blank: every Memory the key reaches)",
             "default": ""},
            {"key": "recall_budget_seconds", "description": "Seconds the per-turn recall may take before Hermes "
             "continues without it (0.5-8)", "default": "8", "type": "number", "minimum": 0.5, "maximum": 8},
            {"key": "capture_turns", "description": "Save the user's words from every turn to Membase (off: save only on request)",
             "default": "off", "choices": ["off", "on"]},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        from utils import atomic_json_write, read_json_or_empty

        path = Path(hermes_home) / CONFIG_FILE
        current = read_json_or_empty(path)
        for key, value in (values or {}).items():
            if key in DEFAULTS:
                current[key] = value
        current = {k: v for k, v in _normalize(current).items() if k in current}
        atomic_json_write(path, current, mode=0o600, sort_keys=True)

    def get_status_config(self, provider_config: dict) -> dict:
        """What `hermes memory status` prints: effective settings, no network call, never the key."""
        from hermes_constants import get_hermes_home

        cfg = load_config(str(get_hermes_home()))
        return {
            "api_key": "set" if _secret("MEMBASE_API_KEY") else "not set",
            "base_url": cfg["base_url"] or _secret("MEMBASE_BASE_URL") or DEFAULT_BASE_URL,
            "container": cfg["container"] or _secret("MEMBASE_CONTAINER") or "(every Memory in reach)",
            "auto_recall": "on" if cfg["auto_recall"] else "off",
            "recall_budget_seconds": cfg["recall_budget_seconds"],
            "capture_turns": "on" if cfg["capture_turns"] else "off",
            "config_file": str(Path(get_hermes_home()) / CONFIG_FILE),
        }

    # ---- lifecycle ----------------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self.shutdown()  # a re-initialize must not leak the previous connection pools
        home = str(kwargs.get("hermes_home") or "")
        if not home:
            from hermes_constants import get_hermes_home

            home = str(get_hermes_home())
        self._cfg = load_config(home)
        if not self._cfg["base_url"]:
            self._cfg["base_url"] = _secret("MEMBASE_BASE_URL").rstrip("/")
        if not self._cfg["container"]:
            self._cfg["container"] = _secret("MEMBASE_CONTAINER")
        self._writes = str(kwargs.get("agent_context") or "primary") not in _NON_PRIMARY
        self._session_user = str(kwargs.get("user_id") or "")
        self._turn_user = self._session_user
        self._error = ""
        self._active = False
        self._recall_refused = False
        if Membase is None:
            self._error = self.unavailable_reason()
            return
        if self._given_client is not None:
            self._client = self._recall_client = self._given_client
        elif self._client_for is None:
            api_key = _secret("MEMBASE_API_KEY")
            if not api_key:
                self._error = self.unavailable_reason()
                logger.warning("Membase: %s", self._error)
                return
            self._build_clients(api_key)
            if self._session_user:
                logger.info("Membase: this profile's one key serves every person who talks to it, gateway user "
                            "%s included; give each person their own profile and key to keep memories apart",
                            self._session_user)
        self._active = True

    def _build_clients(self, api_key: str) -> None:
        base_url = self._cfg["base_url"] or DEFAULT_BASE_URL
        self._client = Membase(api_key=api_key, base_url=base_url)
        self._recall_client = Membase(api_key=api_key, base_url=base_url,
                                      timeout=self._cfg["recall_budget_seconds"], max_retries=0)
        self._owns_clients = True
        self._api_key = api_key

    def _current_key_clients(self) -> tuple[Any, Any]:
        """The clients for the key the profile holds NOW. A key rotated under a running session (the
        owner's new key, or a host that hands each turn a fresh short-lived one) is picked up on the
        next call instead of the session keeping a key that no longer works."""
        key = _secret("MEMBASE_API_KEY")
        if key and key != getattr(self, "_api_key", ""):
            with self._lock:
                if key != getattr(self, "_api_key", ""):
                    old = {id(c): c for c in (self._client, self._recall_client) if c is not None}
                    self._build_clients(key)
                    self._recall_refused = False
                    for client in old.values():
                        try:
                            client.close()
                        except Exception:  # pragma: no cover
                            pass
        return self._client, self._recall_client

    def shutdown(self) -> None:
        if self._owns_clients:
            for client in {id(c): c for c in (self._client, self._recall_client) if c is not None}.values():
                try:
                    client.close()
                except Exception:  # pragma: no cover - closing a pool never matters to the user
                    pass
        self._client = self._recall_client = None
        self._owns_clients = False
        self._active = False

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        # In a shared chat the author of THIS turn decides whose memory a client_for setup reaches.
        self._turn_user = str(kwargs.get("author_id") or self._session_user or "")

    def system_prompt_block(self) -> str:
        if not self._active:
            return ""
        return "\n".join([
            "## Long-term memory: Membase",
            "Passages from the user's Membase memory may be injected before a turn; each names the Memory it "
            "came from. Cite that name when you rely on one.",
            "- `membase_search` to look further, `membase_profile` for standing facts about the user.",
            "- `membase_save` only when the user asks you to remember something (or plainly means it), in their "
            "words. Never save your own answers.",
        ])

    # ---- clients ------------------------------------------------------------------------------

    def _clients_now(self, user: str = "") -> tuple[Any, Any]:
        """(tool client, recall client) for the person this call serves; (None, None) refuses."""
        if not self._active:
            return None, None
        if self._client_for is None:
            return self._current_key_clients() if self._owns_clients else (self._client, self._recall_client)
        uid = user or self._turn_user or self._session_user
        if not uid:
            # Never fall back to someone else's key: no user id, no memory.
            return None, None
        with self._lock:
            client = self._per_user.get(uid)
            if client is None:
                client = self._per_user[uid] = self._client_for(uid)
        return client, client

    def _container_id(self, client: Any, value: str) -> Optional[str]:
        """A Memory name or id -> its id, from the key's container list (cached per client, re-read on a miss)."""
        value = (value or "").strip()
        if not value:
            return None

        def match(listing: list[dict[str, Any]]) -> Optional[str]:
            for c in listing:
                if c.get("id") == value:
                    return value
            folded = value.casefold()
            return next((str(c["id"]) for c in listing if str(c.get("name") or "").casefold() == folded), None)

        found = match(self._containers.get(id(client)) or [])
        if found:
            return found
        listing = list((client.containers.list() or {}).get("containers") or [])
        self._containers[id(client)] = listing
        found = match(listing)
        if found:
            return found
        raise UnknownContainer(value, [str(c.get("name") or c.get("id")) for c in listing])

    # ---- recall -------------------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_count = 0
        if not self._cfg["auto_recall"] or self._recall_refused or not (query or "").strip():
            return ""
        _, client = self._clients_now()
        if client is None:
            return ""
        try:
            container = self._container_id(client, self._cfg["container"])
            response = client.search(query.strip()[:2000], container=container, limit=self._cfg["recall_limit"])
        except UnknownContainer as e:
            logger.warning("Membase recall skipped: %s", e)
            return ""
        except Exception as e:  # fail open: the turn goes on without memory
            if isinstance(e, membase.PermissionDeniedError):
                # Withdrawn access is not retried every turn; explicit tool calls still report it.
                self._recall_refused = True
                logger.warning("Membase recall stopped for this session: %s", explain_error(e))
            else:
                logger.info("Membase recall skipped this turn: %s",
                            explain_error(e, timeout=self._cfg["recall_budget_seconds"]))
            return ""
        for c in (response or {}).get("containers") or []:
            if isinstance(c, dict) and c.get("error"):
                logger.info("Membase: Memory %r could not be searched: %s", _container_label(c), c["error"])
        block, count = format_recall(response, limit=self._cfg["recall_limit"],
                                     max_chars=self._cfg["recall_max_chars"])
        self._last_count = count
        return block

    def recall_status(self) -> Any:
        if RecallStatus is None or not self._last_count:
            return None
        return RecallStatus(provider_label="Membase", count=self._last_count)

    # ---- capture (opt-in) ---------------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: Optional[list[dict[str, Any]]] = None,
                  turn_author: Optional[dict[str, Any]] = None) -> None:
        # The manager runs this on its background worker, so a blocking call is fine here.
        if not self._cfg["capture_turns"] or not self._writes:
            return
        author = turn_author if isinstance(turn_author, dict) else {}
        if author.get("is_bot"):
            return
        # Only the user's own words: an AI answer must never be learned as a fact about them.
        user_text = (user_content or "").strip()
        if not user_text:
            return
        client, _ = self._clients_now(str(author.get("id") or ""))
        if client is None:
            return
        now = datetime.now(timezone.utc)
        content = _clip(user_text, _CAPTURE_CHARS)
        try:
            container = self._container_id(client, self._cfg["container"])
            client.memories.add(content, container=container, title=f"Hermes conversation {now:%Y-%m-%d}")
        except Exception as e:
            hint = ""
            if isinstance(e, membase.BadRequestError) and not self._cfg["container"]:
                hint = " Set `container` in unibaseio-membase.json (or MEMBASE_CONTAINER) to the Memory captures go to."
            level = logging.WARNING if not self._capture_warned else logging.DEBUG
            self._capture_warned = True
            logger.log(level, "Membase capture skipped: %s%s", explain_error(e), hint)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[dict[str, Any]] = None) -> None:
        """Mirror what the built-in memory tool records about the user into their Membase profile,
        so a fact the agent kept in its own notes is one every app reading their memory sees.
        Only facts about the user (``target == "user"``) and only additions: the agent's working
        notes stay its own, and nothing is removed here (forgetting is the owner's to confirm)."""
        if action not in ("add", "replace") or target != "user" or not self._writes:
            return
        text = (content or "").strip()
        if not text or not self._active:
            return
        client, _ = self._clients_now()
        if client is None:
            return
        try:
            client.memories.add(_clip(text, _CAPTURE_CHARS), static=True)
        except Exception as e:  # a read-only key, a lapsed one: the built-in note stands either way
            logger.debug("Membase mirror of a built-in memory write skipped: %s", explain_error(e))

    # ---- tools --------------------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(s)) for s in (SEARCH_SCHEMA, SAVE_SCHEMA, PROFILE_SCHEMA)]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        handlers = {"membase_search": self._tool_search, "membase_save": self._tool_save,
                    "membase_profile": self._tool_profile}
        handler = handlers.get(tool_name)
        if handler is None:
            return _tool_error(f"unknown tool {tool_name}")
        if not self._active:
            return _tool_error(f"Membase is not connected: {self._error or 'not initialized'}")
        client, _ = self._clients_now()
        if client is None:
            return _tool_error("Membase has no key for the person in this conversation, so their memory is not used.")
        try:
            return json.dumps(handler(client, args or {}), ensure_ascii=False)
        except UnknownContainer as e:
            return _tool_error(str(e), available=e.names)
        except ValueError as e:
            return _tool_error(str(e))
        except membase.MembaseError as e:
            action = tool_name.removeprefix("membase_")
            if action == "save" and isinstance(e, membase.BadRequestError) and not (args or {}).get("container"):
                try:
                    names = [str(c.get("name")) for c in (client.containers.list() or {}).get("containers") or []]
                except membase.MembaseError:
                    names = []
                if len(names) > 1:
                    return _tool_error("This key reaches several Memories: pass container with one of them.",
                                       available=names)
            return _tool_error(explain_error(e, action=action))

    def _tool_search(self, client: Any, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        container = self._container_id(client, str(args.get("container") or "") or self._cfg["container"])
        limit = args.get("limit")
        limit = None if limit in (None, "") else _as_number(limit, 8, 1, 50, int)
        response = client.search(query, container=container, limit=limit) or {}
        results = [{"container": _container_label(r), "content": r.get("content", "")}
                   for r in response.get("results") or [] if isinstance(r, dict)]
        out: dict[str, Any] = {"results": results}
        unavailable = [{"container": _container_label(c), "error": c.get("error")}
                       for c in response.get("containers") or [] if isinstance(c, dict) and c.get("error")]
        if unavailable:
            out["unavailable"] = unavailable
            out["note"] = "These Memories could not be searched, so missing passages from them prove nothing."
        return out

    def _tool_save(self, client: Any, args: dict[str, Any]) -> dict[str, Any]:
        content = str(args.get("content") or "").strip()
        if not content:
            raise ValueError("content is required")
        static = _as_bool(args.get("static"), False)
        if static:
            response = client.memories.add(content, static=True) or {}
            return {"saved": True, "status": response.get("status", "recorded"), "where": "profile",
                    "note": "Recorded in the user's profile."}
        container = self._container_id(client, str(args.get("container") or "") or self._cfg["container"])
        response = client.memories.add(content, container=container) or {}
        return {"saved": True, "status": response.get("status", "queued"),
                "memory_id": response.get("memory_id"), "document_id": response.get("document_id"),
                "note": "Queued: the Memory learns it on its next run; it is searchable after that."}

    def _tool_profile(self, client: Any, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip() or None
        response = client.profile(q=query) or {}
        out = {"static": response.get("static") or [], "dynamic": response.get("dynamic") or []}
        if query:
            out["results"] = [{"container": _container_label(r), "content": r.get("content", "")}
                              for r in response.get("results") or [] if isinstance(r, dict)]
        return out


def register(ctx) -> None:
    ctx.register_memory_provider(MembaseMemoryProvider())
