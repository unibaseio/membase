"""``membase``: the Membase command line, hosted or local."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ._version import __version__
from .client import Membase
from .errors import MembaseError
from .media import cli as media_cli


def _client(args: argparse.Namespace) -> Membase:
    if args.local or args.store:
        return Membase(local=args.store or True)
    try:
        return Membase(api_key=args.api_key, base_url=args.base_url)
    except ValueError:
        raise SystemExit("no API key: pass --api-key or set MEMBASE_API_KEY (Connect › Developer keys), "
                         "or use --local") from None


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


_IMPORTABLE = {".json", ".md", ".markdown", ".txt"}


def _import_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in _IMPORTABLE)
        elif p.is_file():
            out.append(p)
        else:
            raise SystemExit(f"no such file or directory: {raw}")
    return out


def _cmd_import(args: argparse.Namespace, m: Membase) -> Any:
    try:
        from membase_core.sources import detect_format, load_sessions
    except ImportError:
        raise SystemExit("membase import reads chat exports with the local engine: pip install 'unibaseio-membase[local]'")
    sessions: list[dict] = []
    found: list[dict] = []
    for f in _import_files(args.files):
        got = load_sessions(f)
        sessions += got
        found.append({"file": str(f), "format": detect_format(f), "sessions": len(got)})
    if not sessions:
        raise SystemExit("no sessions found")
    if args.dry_run:
        return {"would_import": len(sessions), "files": found}
    transport = m._http._transport  # noqa: SLF001 - the local transport, when local
    backend = getattr(transport, "backend", None)
    if backend is not None:
        return backend.import_sessions(sessions, args.container)
    out = []
    for s in sessions:
        text = "\n".join(f"{t['role']}: {t['content']}" for t in s["turns"])
        out.append(m.add(text, container=args.container, title=s["session_id"],
                         custom_id=f"session:{s['session_id']}"))
    return {"sessions": len(out), "documents": out}


def _load_trace(path: str) -> tuple[list[dict], dict]:
    raw = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, list):
        return data, {}
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"], {k: data[k] for k in ("agent_id", "session_id") if data.get(k)}
    raise SystemExit("a trace is a JSON list of messages or an object with a 'messages' list")


def _cmd_agent(args: argparse.Namespace, m: Membase) -> Any:
    backend = getattr(m._http._transport, "backend", None)  # noqa: SLF001
    if backend is None:
        raise SystemExit("agent memory is local: use membase --local agent ...")
    if args.agent_cmd == "ingest":
        messages, meta = _load_trace(args.trace)
        session = args.session or meta.get("session_id") or (Path(args.trace).stem if args.trace != "-" else None)
        return backend.agent_ingest(messages, agent_id=args.agent, session_id=session, container=args.container)
    if args.agent_cmd == "search":
        return backend.agent_search(args.q, agent_id=args.agent, kind=args.kind, limit=args.limit,
                                    container=args.container)
    return backend.agent_skills(args.agent, container=args.container)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="membase", description="Membase — long-term memory for AI.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--local", action="store_true",
                   help="use local memory (store ~/.membase) instead of the hosted API")
    p.add_argument("--store", default=None, metavar="DIR",
                   help="local store directory (default ~/.membase); implies --local")
    p.add_argument("--api-key", default=None, help="hosted API key (default MEMBASE_API_KEY)")
    p.add_argument("--base-url", default=None, help="hosted API base URL (default MEMBASE_BASE_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("containers", help="list the containers in reach")

    s = sub.add_parser("search", help="search memory")
    s.add_argument("q")
    s.add_argument("--container", help="one container id (default: every container in reach)")
    s.add_argument("--limit", type=int, help="passages in all, 1-50")

    a = sub.add_parser("add", help="remember one fact or a short exchange")
    a.add_argument("content")
    a.add_argument("--container", help="container id (may be omitted when one is in reach)")
    a.add_argument("--static", action="store_true", help="a standing fact about the user (profile)")
    a.add_argument("--title", default="")

    d = sub.add_parser("documents", help="documents: add / list / get / delete")
    dsub = d.add_subparsers(dest="doc_cmd", required=True)
    da = dsub.add_parser("add", help="give a container raw material: a file, --url or --text")
    da.add_argument("path", nargs="?")
    da.add_argument("--url", help="a public web address to fetch")
    da.add_argument("--text", help="the document's text")
    da.add_argument("--container", default=None, help="container id (default: default)")
    da.add_argument("--title", default="")
    da.add_argument("--custom-id", default="", help="your own id; the same id again is a no-op")
    dl = dsub.add_parser("list", help="documents, newest first")
    dl.add_argument("--container")
    dg = dsub.add_parser("get", help="one document, with whether it is learned")
    dg.add_argument("document_id")
    dd = dsub.add_parser("delete", help="remove a document")
    dd.add_argument("document_id")
    dd.add_argument("--confirm", action="store_true", help="remove it; without this, only show what would go")

    f = sub.add_parser("forget", help="forget one memory by id")
    f.add_argument("memory_id")
    f.add_argument("--container")
    f.add_argument("--confirm", action="store_true", help="forget it; without this, only show what would go")

    pr = sub.add_parser("profile", help="who the user is")
    pr.add_argument("q", nargs="?")

    k = sub.add_parser("ask", help="an answer composed from memory")
    k.add_argument("message")

    i = sub.add_parser("import", help="import chat exports (Claude, ChatGPT, markdown, JSON); directories are scanned")
    i.add_argument("files", nargs="+")
    i.add_argument("--container")
    i.add_argument("--dry-run", action="store_true", help="only report what would be imported")

    g = sub.add_parser("agent", help="agent memory (local): traces to cases and skills")
    gsub = g.add_subparsers(dest="agent_cmd", required=True)
    gi = gsub.add_parser("ingest", help="learn from an agent trace (chat-completions JSON, or '-' for stdin)")
    gi.add_argument("trace")
    gi.add_argument("--agent", required=True, help="the agent's id (owner of its cases and skills)")
    gi.add_argument("--session", default=None)
    gi.add_argument("--container")
    gs = gsub.add_parser("search", help="search an agent's cases and skills")
    gs.add_argument("q")
    gs.add_argument("--agent", required=True)
    gs.add_argument("--kind", choices=("cases", "skills", "both"), default="both")
    gs.add_argument("--limit", type=int, default=10)
    gs.add_argument("--container")
    gk = gsub.add_parser("skills", help="list an agent's skills")
    gk.add_argument("--agent", required=True)
    gk.add_argument("--container")

    media_cli.add_parser(sub)

    sv = sub.add_parser("serve", help="serve local memory over HTTP on the /v1 routes (always local; "
                                      "MEMBASE_LOCAL_TOKEN requires a bearer)")
    sv.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1")
    sv.add_argument("--port", type=int, default=8787, help="default 8787")

    mc = sub.add_parser("mcp", help="run the MCP server (hosted, or local with --local)")
    mc.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    mc.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (sse / streamable-http)")
    mc.add_argument("--port", type=int, default=8765, help="default 8765 (sse / streamable-http)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "serve":
            from .local.server import serve

            serve(args.host, args.port, root=args.store)
            return 0
        if args.cmd == "mcp":
            from .requirements import require_mcp

            require_mcp()
        m = _client(args)
        if args.cmd == "mcp":
            from .mcp import run

            run(m, transport=args.transport, host=args.host, port=args.port)
            return 0
        if args.cmd == "containers":
            out = m.containers.list()
        elif args.cmd == "search":
            out = m.search(args.q, container=args.container, limit=args.limit)
        elif args.cmd == "add":
            out = m.memories.add(args.content, container=args.container, static=args.static, title=args.title)
        elif args.cmd == "documents":
            if args.doc_cmd == "add":
                if args.path:
                    content = Path(args.path).read_text()
                    title = args.title or Path(args.path).stem
                else:
                    content, title = args.text, args.title
                if (content is None) == (args.url is None):
                    raise SystemExit("documents add needs exactly one of PATH, --text or --url")
                out = m.add(content, container=args.container or "default", url=args.url,
                            title=title, custom_id=args.custom_id)
            elif args.doc_cmd == "list":
                out = m.documents.list(container=args.container)
            elif args.doc_cmd == "get":
                out = m.documents.get(args.document_id)
            else:
                out = m.documents.delete(args.document_id, confirm=args.confirm)
        elif args.cmd == "forget":
            out = m.memories.forget(args.memory_id, container=args.container, confirm=args.confirm)
        elif args.cmd == "profile":
            out = m.profile(args.q)
        elif args.cmd == "ask":
            out = m.ask(args.message)
        elif args.cmd == "agent":
            out = _cmd_agent(args, m)
        elif args.cmd == "media":
            out = media_cli.run(args, m)
        else:
            out = _cmd_import(args, m)
        _print(out)
        m.close()
        return 0
    except MembaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
