"""Media memory on this machine: photos, videos and audio per container, over the engine's
``membase_core.media`` (the ``[media]`` extra). Separate from documents and memories, with its own
search and ask."""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .backend import LocalError

if TYPE_CHECKING:
    from .backend import LocalBackend

KINDS = ("image", "video", "audio", "text")
_LIMIT_MAX = 50
_LIMIT_DEFAULT = 12
_SUFFIX = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mp4": ".m4a",
    "audio/ogg": ".ogg", "text/plain": ".txt", "text/markdown": ".md",
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LocalMedia:
    def __init__(self, backend: LocalBackend) -> None:
        self.b = backend
        self._engines: dict[str, Any] = {}
        self._guard = threading.Lock()
        # One config, model client and CLIP (~1.2GB) for every container's engine.
        self._shared: tuple[Any, Any, Any] | None = None

    def _dir(self, cid: str) -> Path:
        return self.b._dir(cid) / "media"

    def _ids(self) -> list[str]:
        """The containers that hold media."""
        return [cid for cid in self.b._ids() if (self._dir(cid) / "media.db").exists()]

    @staticmethod
    def _require() -> None:
        from ..requirements import media_unavailable

        reason = media_unavailable()
        if reason:
            raise LocalError(422, "capability_unavailable", reason)

    def engine(self, cid: str):
        with self._guard:
            if cid not in self._engines:
                try:
                    from membase_core.media import MediaConfig, MediaMemoryEngine
                    from membase_core.media.clip import Clip
                    from membase_core.media.llm import MediaLLM
                except ImportError as e:  # pragma: no cover - depends on the install
                    self._require()
                    raise LocalError(422, "capability_unavailable", str(e)) from e
                if self._shared is None:
                    cfg = MediaConfig.from_env()
                    self._shared = (cfg, MediaLLM(cfg), Clip(cfg.visual_model))
                cfg, llm, clip = self._shared
                self._engines[cid] = MediaMemoryEngine(self._dir(cid), config=cfg, llm=llm, clip=clip)
            return self._engines[cid]

    def close(self) -> None:
        with self._guard:
            for e in self._engines.values():
                e.close()
            self._engines.clear()

    # ------------------------------------------------------------------ shapes

    def _custom_ids_path(self, cid: str) -> Path:
        return self._dir(cid) / "custom_ids.json"

    def _custom_ids(self, cid: str) -> dict[str, str]:
        p = self._custom_ids_path(cid)
        return json.loads(p.read_text()) if p.exists() else {}

    @staticmethod
    def _moment(engine, m: dict) -> dict:
        out = {k: m[k] for k in ("t", "end", "kind", "text", "speaker", "speaker_confidence")}
        out["thumb"] = str(engine.media_dir / m["thumb"]) if m.get("thumb") else None
        if "channels" in m:
            out["channels"] = m["channels"]
        return out

    def _row(self, cid: str, engine, d: dict, *, moments: bool) -> dict:
        custom = {v: k for k, v in self._custom_ids(cid).items()}
        return {
            "id": d["id"],
            "kind": d["kind"],
            "filename": d["filename"],
            "container": cid,
            "custom_id": custom.get(d["id"], ""),
            "summary": d["summary"],
            "duration": d["duration"],
            "path": str(engine.media_dir / d["file"]) if d["file"] else None,
            "created_at": _iso(d["created"]),
            "moments": [self._moment(engine, m) for m in d["moments"]] if moments else len(d["moments"]),
        }

    def _hit(self, cid: str, engine, r: dict) -> dict:
        row = self._row(cid, engine, r["media"], moments=False)
        row.update({
            "container_name": self.b._meta(cid)["name"],
            "score": r["score"],
            "channels": r["channels"],
            "moments": [self._moment(engine, m) for m in r["moments"]],
        })
        return row

    def _find(self, media_id: str) -> tuple[str, Any, dict]:
        for cid in self._ids():
            engine = self.engine(cid)
            for d in engine.list_media():
                if d["id"] == media_id:
                    return cid, engine, d
        raise LocalError(404, "not_found", "media not found", {"media_id": media_id})

    # --------------------------------------------------------------- operations

    def add(self, container: str | None, path: str | None = None, url: str | None = None,
            title: str = "", custom_id: str = "") -> dict:
        if (path is None) == (url is None):
            raise LocalError(400, "bad_request", "give exactly one of path and url")
        from membase_core.media import kind_of

        self._require()
        with tempfile.TemporaryDirectory() as tmp:
            src = self._download(url, Path(tmp)) if url is not None else Path(path).expanduser()
            if not src.is_file():
                raise LocalError(400, "bad_request", f"no such file: {path}")
            kind = kind_of(src.name)
            if kind is None:
                raise LocalError(400, "bad_request", f"unsupported media type: {src.name}",
                                 {"supported": list(KINDS)})
            cid = self.b._resolve(container, create=True)
            engine = self.engine(cid)
            ids = self._custom_ids(cid)
            if custom_id and ids.get(custom_id) in {d["id"] for d in engine.list_media()}:
                return {"media_id": ids[custom_id], "container": cid, "status": "exists"}
            name = title or src.name
            if kind == "text":
                item = engine.add_text(src.read_text(encoding="utf-8"), title=name)
            else:
                add = {"image": engine.add_image, "video": engine.add_video, "audio": engine.add_audio}
                item = add[kind](src, filename=name)
        if custom_id:
            ids[custom_id] = item.id
            self._custom_ids_path(cid).write_text(json.dumps(ids))
        return {
            "media_id": item.id,
            "container": cid,
            "status": "stored",
            "kind": item.kind,
            "filename": item.filename,
            "summary": item.summary,
            "duration": item.duration,
            "moments": len(item.moments),
        }

    @staticmethod
    def _download(url: str, into: Path) -> Path:
        import httpx

        r = httpx.get(url, follow_redirects=True, timeout=120)
        r.raise_for_status()
        name = Path(urlparse(url).path).name or "download"
        if not Path(name).suffix:
            name += _SUFFIX.get(r.headers.get("content-type", "").split(";")[0].strip(), "")
        dest = into / name
        dest.write_bytes(r.content)
        return dest

    def list(self, container: str | None = None) -> dict:
        targets = [self.b._resolve(container)] if container else self._ids()
        rows: list[dict] = []
        for cid in targets:
            if (self._dir(cid) / "media.db").exists():
                engine = self.engine(cid)
                rows += [self._row(cid, engine, d, moments=False) for d in engine.list_media()]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {"media": rows}

    def get(self, media_id: str) -> dict:
        cid, engine, d = self._find(media_id)
        return self._row(cid, engine, d, moments=True)

    def delete(self, media_id: str, confirm: bool = False) -> dict:
        cid, engine, d = self._find(media_id)
        target = {"media_id": media_id, "kind": d["kind"], "filename": d["filename"]}
        if not confirm:
            return self.b._confirmation_required("delete_media", target)
        engine.delete(media_id)
        ids = self._custom_ids(cid)
        kept = {k: v for k, v in ids.items() if v != media_id}
        if kept != ids:
            self._custom_ids_path(cid).write_text(json.dumps(kept))
        return {"status": "deleted", "media_id": media_id}

    def search(self, q: str, container: str | None = None, kind: str | None = None,
               limit: int | None = None) -> dict:
        if not (q or "").strip():
            raise LocalError(400, "bad_request", "q is required")
        if kind not in (None, "", *KINDS):
            raise LocalError(400, "bad_request", f"kind must be one of {', '.join(KINDS)}")
        if limit is not None and not 1 <= int(limit) <= _LIMIT_MAX:
            raise LocalError(400, "bad_request", f"limit must be 1-{_LIMIT_MAX}")
        k = int(limit) if limit is not None else _LIMIT_DEFAULT
        targets = [self.b._resolve(container)] if container else self._ids()
        targets = [cid for cid in targets if (self._dir(cid) / "media.db").exists()]
        if targets:
            self._require()
        answers: list[tuple[str, list[dict], str | None]] = []
        for cid in targets:
            try:
                engine = self.engine(cid)
                hits = [self._hit(cid, engine, r) for r in engine.search(q, kind=kind or None, top_k=k)]
                answers.append((cid, hits, None))
            except LocalError:
                raise
            except Exception as e:  # noqa: BLE001 - one container failing is reported beside the rest
                answers.append((cid, [], f"{type(e).__name__}: {e}"))
        results: list[dict] = []
        for rank in range(max((len(h) for _, h, _ in answers), default=0)):
            results += [h[rank] for _, h, _ in answers if rank < len(h)]
        return {
            "query": q,
            "results": results[:k],
            "containers": [
                {"id": cid, "name": self.b._meta(cid)["name"], **({"error": err} if err else {})}
                for cid, _, err in answers
            ],
        }

    def ask(self, message: str, container: str | None = None) -> dict:
        if not (message or "").strip():
            raise LocalError(400, "bad_request", "message is required")
        if container:
            targets = [self.b._resolve(container)]
        else:
            targets = self._ids()
            if len(targets) > 1:
                raise LocalError(400, "bad_request", "several containers hold media; name one",
                                 {"containers": targets})
        if not targets or not (self._dir(targets[0]) / "media.db").exists():
            return {"answer": "Not answerable from media memory.", "sources": [],
                    "container": targets[0] if targets else None}
        self._require()
        cid = targets[0]
        engine = self.engine(cid)
        out = engine.ask(message)
        return {
            "answer": out["answer"],
            "sources": [self._hit(cid, engine, r) for r in out["sources"]],
            "container": cid,
        }
