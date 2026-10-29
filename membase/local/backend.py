"""The agent protocol's operations on this machine, over the unibaseio-membase-core engine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CONTAINER = "default"
_PROFILE = "__profile__"
_LIMIT_MAX = 50
_LIMIT_FLOOR = 8
_DYNAMIC = 10
_ROLE_LINE = re.compile(r"(?m)^\s*\**\s*(user|assistant|human|ai|claude|chatgpt|you|me)\s*\**\s*:", re.I)


class LocalError(Exception):
    """A refusal, carried to the caller as the platform's error envelope."""

    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:64] or DEFAULT_CONTAINER


class LocalBackend:
    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self.root = Path(root or "~/.membase").expanduser()
        self._engines: dict[str, Any] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._media = None


    def _dir(self, cid: str) -> Path:
        if cid == _PROFILE:
            return self.root / "profile"
        return self.root if cid == DEFAULT_CONTAINER else self.root / "containers" / cid

    def _meta_path(self, cid: str) -> Path:
        return self._dir(cid) / ("container.json" if cid != DEFAULT_CONTAINER else "default-container.json")

    def _exists(self, cid: str) -> bool:
        return self._meta_path(cid).exists() or (self._dir(cid) / "memory.db").exists()

    def _meta(self, cid: str) -> dict:
        p = self._meta_path(cid)
        meta = json.loads(p.read_text()) if p.exists() else {}
        db = self._dir(cid) / "memory.db"
        stamp = (
            datetime.fromtimestamp(db.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if db.exists() else _now()
        )
        return {
            "id": cid,
            "name": meta.get("name") or cid,
            "description": meta.get("description", ""),
            "created_at": meta.get("created_at") or stamp,
            "updated_at": stamp,
        }

    def _create(self, name: str) -> str:
        cid = _slug(name)
        if not self._exists(cid):
            self._dir(cid).mkdir(parents=True, exist_ok=True)
            self._meta_path(cid).write_text(json.dumps(
                {"name": name.strip() or cid, "description": "", "created_at": _now()}
            ))
        return cid

    def _ids(self) -> list[str]:
        ids = [DEFAULT_CONTAINER] if self._exists(DEFAULT_CONTAINER) else []
        base = self.root / "containers"
        if base.is_dir():
            ids += sorted(p.name for p in base.iterdir() if p.is_dir() and self._exists(p.name))
        return ids

    def _resolve(self, container: str | None, *, create: bool = False) -> str:
        if container:
            for cid in self._ids():
                if container in (cid, self._meta(cid)["name"]):
                    return cid
            if create:
                return self._create(container)
            raise LocalError(403, "unauthorized", f"container {container!r} is not in reach")
        ids = self._ids()
        if len(ids) == 1:
            return ids[0]
        if DEFAULT_CONTAINER in ids or create or not ids:
            return self._create(DEFAULT_CONTAINER) if create else DEFAULT_CONTAINER
        raise LocalError(400, "bad_request", "several containers are in reach; name one")

    def engine(self, cid: str):
        with self._guard:
            if cid not in self._engines:
                try:
                    from membase_core import CoreMemoryEngine
                except ImportError as e:  # pragma: no cover - depends on the install
                    from ..requirements import local_unavailable

                    raise LocalError(
                        422, "capability_unavailable",
                        local_unavailable() or "local memory needs the engine: pip install 'unibaseio-membase[local]'",
                    ) from e
                d = self._dir(cid)
                d.mkdir(parents=True, exist_ok=True)
                self._engines[cid] = CoreMemoryEngine(str(d / "memory.db"))
                self._locks[cid] = threading.RLock()
            return self._engines[cid], self._locks[cid]

    @property
    def media(self):
        """Media memory (photos, videos, audio): its own engines, search and ask."""
        if self._media is None:
            from ..media.local import LocalMedia

            self._media = LocalMedia(self)
        return self._media

    def close(self) -> None:
        with self._guard:
            for e in self._engines.values():
                e.close()
            self._engines.clear()
        if self._media is not None:
            self._media.close()

    def containers(self) -> dict:
        return {"containers": [self._meta(cid) for cid in self._ids()]}


    def _hits(self, cid: str, q: str, k: int) -> list[dict]:
        engine, _ = self.engine(cid)
        name = self._meta(cid)["name"]
        episodes = [
            {
                "content": c.text,
                "score": float(c.score or c.rrf),
                "container": cid,
                "container_name": name,
                "source": f"episode:{cid}:{c.id}",
                "title": c.subject or "",
                "session_date": c.valid_at,
            }
            for c in engine.search(q).observations_top
        ]
        docs: list[dict] = []
        if engine.list_documents(page_size=1).total:
            for h in engine.search_knowledge(q, top_k=k, include_content=True).hits:
                docs.append({
                    "content": h.content or h.summary,
                    "score": float(h.score),
                    "container": cid,
                    "container_name": name,
                    "source": f"document:{h.document.doc_id}#{h.topic_id}",
                    "title": h.topic_path,
                })
        merged: list[dict] = []
        for i in range(max(len(episodes), len(docs))):
            merged += [lst[i] for lst in (episodes, docs) if i < len(lst)]
        return merged[:k]

    def search(self, q: str, container: str | None = None, limit: int | None = None) -> dict:
        if not (q or "").strip():
            raise LocalError(400, "bad_request", "q is required")
        targets = [self._resolve(container)] if container else self._ids()
        if limit is not None and not 1 <= int(limit) <= _LIMIT_MAX:
            raise LocalError(400, "bad_request", f"limit must be 1-{_LIMIT_MAX}")
        k = int(limit) if limit is not None else max(_LIMIT_FLOOR, len(targets))
        answers: list[tuple[str, list[dict], str | None]] = []
        for cid in targets:
            try:
                answers.append((cid, self._hits(cid, q, k), None))
            except LocalError:
                raise
            except Exception as e:  # noqa: BLE001 - one container failing is reported beside the rest
                answers.append((cid, [], f"{type(e).__name__}: {e}"))
        results: list[dict] = []
# Interleave by rank, as the hosted fan-out does: every container's best before any second.
        for rank in range(max((len(h) for _, h, _ in answers), default=0)):
            results += [h[rank] for _, h, _ in answers if rank < len(h)]
        kept = results[:k]
        heard = {r["container"] for r in kept}
        return {
            "query": q,
            "results": kept,
            "containers": [
                {
                    "id": cid,
                    "name": self._meta(cid)["name"],
                    **({"error": err} if err else {}),
                    **({"truncated": True} if hits and cid not in heard else {}),
                }
                for cid, hits, err in answers
            ],
        }

    def profile(self, q: str | None = None) -> dict:
        out: dict[str, Any] = {"available": True, "ready": True, "static": [], "dynamic": []}
        if (self._dir(_PROFILE) / "memory.db").exists():
            engine, _ = self.engine(_PROFILE)
            out["static"] = [p["description"] for p in engine.profile() if p["category"] == "static"]
        recent: list[dict] = []
        for cid in self._ids():
            engine, _ = self.engine(cid)
            recent += [
                {"id": f"{cid}:{e['id']}", "content": e["content"], "updated_at": e["created_at"],
                 "title": e["title"], "session_date": e["session_date"]}
                for e in engine.episodes(limit=_DYNAMIC)
            ]
        out["dynamic"] = sorted(recent, key=lambda e: str(e["updated_at"] or ""), reverse=True)[:_DYNAMIC]
        if (q or "").strip():
            out["results"] = self.search(q or "")["results"]
        return out

    def rules(self) -> dict:
        return {"rules": [], "local": True}

    def _sidecar(self, cid: str) -> Path:
        return self._dir(cid) / "documents.json"

    def _doc_meta(self, cid: str) -> dict:
        p = self._sidecar(cid)
        return json.loads(p.read_text()) if p.exists() else {}

    def _document_row(self, cid: str, d: Any, meta: dict) -> dict:
        m = meta.get(d.doc_id, {})
        return {
            "id": d.doc_id,
            "title": d.title,
            "kind": "document",
            "status": "learned",
            "container": cid,
            "custom_id": m.get("custom_id", ""),
            "metadata": m.get("metadata") or {},
            "created_at": d.created_at,
            "updated_at": getattr(d, "updated_at", d.created_at),
        }

    def documents(self, container: str | None = None) -> dict:
        targets = [self._resolve(container)] if container else self._ids()
        rows: list[dict] = []
        for cid in targets:
            engine, _ = self.engine(cid)
            meta = self._doc_meta(cid)
            page = 1
            while True:
                listing = engine.list_documents(page=page, page_size=100)
                rows += [self._document_row(cid, d, meta) for d in listing.documents]
                if page * 100 >= listing.total:
                    break
                page += 1
        rows.sort(key=lambda r: str(r["created_at"] or ""), reverse=True)
        return {"documents": rows}

    def _find_document(self, document_id: str) -> tuple[str, Any]:
        from membase_core import DocumentNotFoundError

        for cid in self._ids():
            engine, _ = self.engine(cid)
            try:
                return cid, engine.get_document(document_id)
            except DocumentNotFoundError:
                continue
        raise LocalError(404, "not_found", "document not found", {"document_id": document_id})

    def document(self, document_id: str) -> dict:
        cid, d = self._find_document(document_id)
        row = self._document_row(cid, d, self._doc_meta(cid))
        row.update({
            "learned": True,
            "summary": d.summary,
            "topics": [{"id": t.topic_id, "path": t.topic_path, "summary": t.summary} for t in d.topics],
        })
        return row


    def add_document(
        self,
        container: str | None,
        content: str | None = None,
        url: str | None = None,
        title: str = "",
        metadata: dict | None = None,
        custom_id: str = "",
    ) -> dict:
        if (content is None) == (url is None):
            raise LocalError(400, "bad_request", "give exactly one of content and url")
        from membase_core import DuplicateDocumentError

        cid = self._resolve(container, create=True)
        engine, lock = self.engine(cid)
        doc_id = "doc_" + hashlib.sha256(custom_id.encode()).hexdigest()[:16] if custom_id else None
        with lock:
            try:
                if url is not None:
                    res = self._ingest_url(engine, url, title=title, doc_id=doc_id)
                else:
                    res = engine.ingest_document(text=content, title=title or None, doc_id=doc_id)
            except DuplicateDocumentError:
                return {"document_id": doc_id, "container": cid, "status": "exists"}
            meta = self._doc_meta(cid)
            meta[res.doc_id] = {"custom_id": custom_id, "metadata": metadata or {}}
            self._sidecar(cid).write_text(json.dumps(meta))
            engine.save()
        return {
            "document_id": res.doc_id,
            "container": cid,
            "status": "learned",
            "topics": res.topic_count,
        }

    @staticmethod
    def _ingest_url(engine, url: str, *, title: str, doc_id: str | None):
        import httpx

        r = httpx.get(url, follow_redirects=True, timeout=60)
        r.raise_for_status()
        kind = r.headers.get("content-type", "").split(";")[0].strip()
        suffix = {
            "application/pdf": ".pdf", "text/html": ".html", "text/markdown": ".md",
            "text/plain": ".txt", "application/json": ".json",
        }.get(kind) or Path(url.split("?")[0]).suffix or ".html"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"page{suffix}"
            p.write_bytes(r.content)
            return engine.ingest_document(str(p), title=title or url, doc_id=doc_id,
                                          source_type="url")

    @staticmethod
    def _turns(content: str, title: str) -> list[dict]:
        """A transcript (``User: …`` / ``Assistant: …`` lines) keeps its turns; anything else is
        one thing the user said."""
        if len(_ROLE_LINE.findall(content)) >= 2:
            from membase_core.sources.markdown_chat import parse_markdown_chat

            sessions = parse_markdown_chat(content)
            turns = [t for s in sessions for t in s["turns"]]
            if turns:
                return turns
        text = f"{title}\n\n{content}" if title else content
        return [{"role": "user", "content": text}]

    def add_memory(
        self, content: str, container: str | None = None, static: bool = False, title: str = ""
    ) -> dict:
        if not (content or "").strip():
            raise LocalError(400, "bad_request", "content is required")
        if static:
            engine, lock = self.engine(_PROFILE)
            with lock:
                engine.add_profile_fact(content.strip())
            return {"static": True, "status": "recorded", "content": content}
        cid = self._resolve(container, create=True)
        engine, lock = self.engine(cid)
        sid = f"mem-{uuid.uuid4().hex[:12]}"
        with lock:
            result = engine.ingest_session(sid, _now(), self._turns(content, title))
            engine.save()
            ids = [e["id"] for e in engine.episodes(limit=1000) if e["session_id"] == sid]
        memory_ids = [f"{cid}:{i}" for i in sorted(ids)]
        return {
            "memory_id": memory_ids[0] if memory_ids else None,
            "memory_ids": memory_ids,
            "container": cid,
            "status": "learned",
            "static": False,
            "extracted": result.counts,
        }

    @staticmethod
    def _confirmation_required(verb: str, target: dict) -> dict:
        return {
            "status": "confirmation_required",
            "verb": verb,
            **target,
            "how": "Pass confirm=true to remove it.",
        }

    def delete_document(self, document_id: str, confirm: bool = False) -> dict:
        cid, d = self._find_document(document_id)
        target = {"document_id": document_id, "title": d.title}
        if not confirm:
            return self._confirmation_required("delete_document", target)
        engine, lock = self.engine(cid)
        with lock:
            res = engine.delete_document(document_id)
            meta = self._doc_meta(cid)
            if meta.pop(document_id, None) is not None:
                self._sidecar(cid).write_text(json.dumps(meta))
            engine.save()
        return {"status": "deleted", "document_id": document_id, "row_deleted": True,
                "removed_topics": getattr(res, "deleted_topics", None)}

    def _parse_memory_id(self, memory_id: str, container: str | None) -> tuple[str, int]:
        cid, _, raw = memory_id.rpartition(":")
        cid = cid or (self._resolve(container) if container else "")
        if not cid or not raw.isdigit() or not self._exists(cid):
            raise LocalError(404, "not_found", "memory not found", {"memory_id": memory_id})
        return cid, int(raw)

    def forget_memory(self, memory_id: str, container: str | None = None, confirm: bool = False) -> dict:
        cid, eid = self._parse_memory_id(memory_id, container)
        engine, lock = self.engine(cid)
        found = next((e for e in engine.episodes(limit=100000) if e["id"] == eid), None)
        if found is None:
            raise LocalError(404, "not_found", "memory not found", {"memory_id": memory_id})
        if not confirm:
            return self._confirmation_required(
                "forget_memory", {"memory_id": memory_id, "content": found["content"]}
            )
        with lock:
            engine.forget(eid)
            engine.save()
        return {"status": "forgotten", "memory_id": memory_id}

    def _best_container(self, message: str) -> str | None:
        ids = self._ids()
        if len(ids) <= 1:
            return ids[0] if ids else None
        best, best_score = None, -1.0
        for cid in ids:
            engine, _ = self.engine(cid)
            top = engine.search(message).observations_top[:1]
            score = float(top[0].score or top[0].rrf) if top else -1.0
            if score > best_score:
                best, best_score = cid, score
        return best

    def ask(self, message: str, model: str | None = None, container: str | None = None) -> dict:
        if not (message or "").strip():
            raise LocalError(400, "bad_request", "message is required")
        cid = self._resolve(container) if container else self._best_container(message)
        if cid is None or not self._exists(cid):
            return {"answer": "Not answerable from memory.", "citations": [], "container": cid}
        engine, lock = self.engine(cid)
        with lock:
            previous = engine.reader_model
            engine.reader_model = model or previous
            try:
                detail = engine.answer_detail(message)
            finally:
                engine.reader_model = previous
        return {
            "answer": detail.answer,
            "citations": [c.text for c in detail.retrieval.observations_top[:5]],
            "container": cid,
        }


    def agent_ingest(self, messages: list[dict], *, agent_id: str, session_id: str | None = None,
                     container: str | None = None) -> dict:
        """An agent trace into the agent's cases and skills (engine agent memory)."""
        cid = self._resolve(container, create=True)
        engine, lock = self.engine(cid)
        with lock:
            out = engine.ingest_agent_trace(messages, agent_id=agent_id, session_id=session_id)
            engine.save()
        return {"container": cid, **out}

    def agent_search(self, q: str, *, agent_id: str, kind: str = "both", limit: int = 10,
                     container: str | None = None) -> dict:
        cid = self._resolve(container)
        engine, _ = self.engine(cid)
        return {"container": cid, **engine.search_agent(q, agent_id=agent_id, kind=kind, top_k=limit)}

    def agent_skills(self, agent_id: str, container: str | None = None) -> dict:
        cid = self._resolve(container)
        engine, _ = self.engine(cid)
        return {"container": cid, "skills": engine.list_skills(agent_id)}

    def import_sessions(self, sessions: list[dict], container: str | None = None) -> dict:
        """Chat exports (``membase_core.sources``) straight into conversation memory."""
        cid = self._resolve(container, create=True)
        engine, lock = self.engine(cid)
        with lock:
            results = engine.ingest_sessions_parallel(sessions)
            engine.save()
        return {
            "container": cid,
            "sessions": len(results),
            "episodes": sum(int(r.counts.get("episodes", 0)) for r in results),
        }
