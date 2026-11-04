"""``membase media``: media memory on the command line."""

from __future__ import annotations

import argparse
from typing import Any

from ..client import DEFAULT_BASE_URL, Membase


def add_parser(sub: Any) -> None:
    md = sub.add_parser("media", help="media memory: photos and videos (and, locally, audio, with "
                                      "add and ask)")
    msub = md.add_subparsers(dest="media_cmd", required=True)
    ma = msub.add_parser("add", help="store an image, video or audio file (or --url); local only")
    ma.add_argument("path", nargs="?")
    ma.add_argument("--url", help="a public address of the file")
    ma.add_argument("--title", default="", help="name to show instead of the file name")
    ma.add_argument("--custom-id", default="", help="your own id; the same id again is a no-op")
    ml = msub.add_parser("list", help="stored media, newest first")
    ml.add_argument("--limit", type=int, help="items, 1-1000 (default 100)")
    mg = msub.add_parser("get", help="one item with its shots and spoken lines")
    mg.add_argument("media_id")
    mdel = msub.add_parser("delete", help="remove an item and its file")
    mdel.add_argument("media_id")
    mdel.add_argument("--confirm", action="store_true",
                      help="remove it; without this nothing is removed and the answer names what would be")
    ms = msub.add_parser("search", help="search media in any language")
    ms.add_argument("q")
    ms.add_argument("--kind", choices=("image", "video", "audio", "text"))
    ms.add_argument("--limit", type=int, help="items, 1-50 (default 12)")
    mk = msub.add_parser("ask", help="an answer from media memory, with its sources; local only")
    mk.add_argument("message")


def run(args: argparse.Namespace, m: Membase) -> Any:
    local = getattr(m._http._transport, "backend", None) is not None
    if args.media_cmd in ("add", "ask") and not local and m.base_url == DEFAULT_BASE_URL:
        raise SystemExit(f"media {args.media_cmd} is local for now: use membase --local media {args.media_cmd} ...")
    if args.media_cmd == "add":
        if (args.path is None) == (args.url is None):
            raise SystemExit("media add needs exactly one of PATH or --url")
        return m.media.add(args.path, url=args.url, title=args.title, custom_id=args.custom_id)
    if args.media_cmd == "list":
        return m.media.list(limit=args.limit)
    if args.media_cmd == "get":
        return m.media.get(args.media_id)
    if args.media_cmd == "delete":
        return m.media.delete(args.media_id, confirm=args.confirm)
    if args.media_cmd == "search":
        return m.media.search(args.q, kind=args.kind, limit=args.limit)
    return m.media.ask(args.message)
