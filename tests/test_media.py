"""Media memory over the local routes, offline: the engine's real store, indexes and ffmpeg steps,
with stand-ins for the model calls and CLIP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("membase_media", reason="needs the media extra (unibaseio-membase-core[media])")

from membase import (  # noqa: E402
    BadRequestError,
    Membase,
    NotFoundError,
    UnprocessableEntityError,
    cli,
    requirements,
)
from membase.local.server import make_server  # noqa: E402
from membase.requirements import INSTALL_MEDIA, media_unavailable  # noqa: E402
from membase_media import MediaConfig  # noqa: E402

COLORS = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255)}


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


class FakeLLM:
    def __init__(self) -> None:
        self.config = MediaConfig()
        self.segments: list[dict] = []
        self.client = SimpleNamespace()

    def caption(self, image) -> str:
        mean = np.asarray(image.convert("RGB"), dtype=np.float32).mean(axis=(0, 1))
        color = min(COLORS, key=lambda c: np.linalg.norm(mean - np.array(COLORS[c])))
        return f"a plain {color} square"

    def embed(self, texts):
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in _words(t):
                out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
        return out / (np.linalg.norm(out, axis=1, keepdims=True) + 1e-8)

    def transcribe(self, audio_path):
        return [SimpleNamespace(**s) for s in self.segments]

    def chat_text(self, model, system, user, max_tokens=1024) -> str:
        return "translated" if system.startswith("Translate") else "summary of the media"

    def vision_json(self, model, system, parts) -> dict:
        return {"speakers": {}}

    def chat_json(self, model, system, user, max_tokens=4096) -> dict:
        if "Items:" in user:
            query, items = user.split("\n\nItems:\n")
            words = _words(query) - {"query"}
            return {"scores": {line[1:line.index("]")]: 3 if words & _words(line) else 0
                               for line in items.splitlines()}}
        return {"relevant": [1], "answer": "from media memory"}


class FakeClip:
    def _vec(self, color: str) -> np.ndarray:
        v = np.array(COLORS[color] + (1,) * 5, dtype=np.float32)
        return v / np.linalg.norm(v)

    def encode_images(self, images):
        return np.stack([self._vec(FakeLLM().caption(im).split()[2]) for im in images])

    def encode_text(self, text):
        return self._vec(next((c for c in COLORS if c in text.lower()), "green"))


@pytest.fixture
def media_ready(monkeypatch):
    # The engine's media modules are installed; CLIP itself is stood in for below.
    monkeypatch.setattr(requirements, "media_unavailable", lambda: None)


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def local(tmp_path, media_ready, llm):
    m = Membase(local=tmp_path / "store")
    m._http._transport.backend.media._shared = (MediaConfig(), llm, FakeClip())
    yield m
    m.close()


def _png(tmp_path, color: str, name: str):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (48, 48), color).save(path)
    return path


def _has_ffmpeg() -> bool:
    try:
        from membase_media.video import ffmpeg_exe

        ffmpeg_exe()
        return True
    except Exception:
        return False


def test_an_empty_store_has_no_media(local):
    assert local.media.list() == {"media": []}
    assert local.media.search("anything") == {"query": "anything", "results": [], "containers": []}
    assert local.media.ask("what did I film?")["sources"] == []


def test_add_list_get_search_ask(local, tmp_path):
    out = local.media.add(_png(tmp_path, "red", "r.png"), title="Red card")
    assert out["status"] == "stored" and out["kind"] == "image" and out["container"] == "default"
    assert out["summary"] == "a plain red square" and out["filename"] == "Red card"
    local.media.add(_png(tmp_path, "blue", "b.png"))

    rows = local.media.list()["media"]
    assert [r["filename"] for r in rows] == ["b.png", "Red card"]
    item = local.media.get(out["media_id"])
    assert item["path"].endswith(".png") and item["moments"] == []
    assert open(item["path"], "rb").read(4) == b"\x89PNG"

    hits = local.media.search("red square")
    assert hits["results"][0]["id"] == out["media_id"]
    assert set(hits["results"][0]["channels"]) == {"semantic", "keyword", "visual"}
    assert hits["containers"] == [{"id": "default", "name": "default"}]
    assert local.media.search("red", kind="video")["results"] == []

    answer = local.media.ask("which card was red?")
    assert answer["answer"] == "from media memory" and answer["container"] == "default"
    assert len(answer["sources"]) == 1


def test_media_is_apart_from_documents_and_memories(local, tmp_path):
    local.media.add(_png(tmp_path, "red", "r.png"))
    assert local.documents.list() == {"documents": []}
    assert local.search("red square")["results"] == []


def test_custom_ids_and_delete_with_confirmation(local, tmp_path):
    first = local.media.add(_png(tmp_path, "red", "r.png"), custom_id="card-1")
    again = local.media.add(_png(tmp_path, "red", "r.png"), custom_id="card-1")
    assert again == {"media_id": first["media_id"], "container": "default", "status": "exists"}
    assert local.media.list()["media"][0]["custom_id"] == "card-1"

    dry = local.media.delete(first["media_id"])
    assert dry["status"] == "confirmation_required" and dry["verb"] == "delete_media"
    assert len(local.media.list()["media"]) == 1
    path = local.media.get(first["media_id"])["path"]
    assert local.media.delete(first["media_id"], confirm=True) == {
        "status": "deleted", "media_id": first["media_id"],
    }
    assert local.media.list()["media"] == []
    assert not __import__("os").path.exists(path)
    with pytest.raises(NotFoundError):
        local.media.get(first["media_id"])
    # The id is free again once its item is gone.
    assert local.media.add(_png(tmp_path, "red", "r.png"), custom_id="card-1")["status"] == "stored"


def test_refusals(local, tmp_path):
    with pytest.raises(BadRequestError):
        local._request("POST", "/v1/media", json={"path": "/x.png", "url": "https://x/y.png"})
    with pytest.raises(BadRequestError, match="no such file"):
        local.media.add(tmp_path / "missing.png")
    (tmp_path / "data.bin").write_bytes(b"\x00")
    with pytest.raises(BadRequestError, match="unsupported"):
        local.media.add(tmp_path / "data.bin")
    with pytest.raises(BadRequestError):
        local.media.search("")
    with pytest.raises(BadRequestError):
        local.media.search("x", kind="pdf")
    with pytest.raises(BadRequestError):
        local.media.search("x", limit=99)
    with pytest.raises(NotFoundError):
        local.media.delete("img_missing", confirm=True)
    r = local._http.post("/v1/media/search", json={"q": "x", "threshold": 1})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


def test_containers_keep_their_own_media(local, tmp_path):
    local.media.add(_png(tmp_path, "red", "r.png"), container="Trips")
    local.media.add(_png(tmp_path, "blue", "b.png"), container="Work")
    assert [r["container"] for r in local.media.list(container="trips")["media"]] == ["trips"]
    hits = local.media.search("square")
    assert {r["container"] for r in hits["results"]} == {"trips", "work"}
    with pytest.raises(BadRequestError, match="several containers"):
        local.media.ask("what colour?")
    assert local.media.ask("what colour?", container="work")["container"] == "work"


@pytest.mark.skipif(not _has_ffmpeg(), reason="needs ffmpeg or imageio-ffmpeg")
def test_a_video_has_moments_with_thumbnails(local, llm, tmp_path):
    from membase_media.video import ffmpeg

    video = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=160x120:d=3:r=10",
           "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=3:r=10",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video))
    llm.segments = [{"speaker": "A", "start": 3.5, "end": 4.5, "text": "the blue part starts"}]
    out = local.media.add(video)
    assert out["kind"] == "video" and out["moments"] == 3

    item = local.media.get(out["media_id"])
    assert [(m["kind"], m["t"]) for m in item["moments"]] == [("scene", 0.0), ("scene", 3.0), ("speech", 3.5)]
    assert all(open(m["thumb"], "rb").read(2) == b"\xff\xd8" for m in item["moments"])
    hit = local.media.search("part starts")["results"][0]
    assert hit["id"] == out["media_id"] and hit["moments"][0]["t"] == 3.5


def test_media_needs_its_extra(tmp_path, monkeypatch):
    reason = media_unavailable((3, 13), find=lambda name: None if name == "open_clip" else object())
    assert INSTALL_MEDIA in reason
    assert "Python 3.12" in media_unavailable((3, 11), find=lambda name: object())
    assert media_unavailable((3, 13), find=lambda name: object()) is None

    monkeypatch.setattr(requirements, "media_unavailable", lambda: f"media memory needs ...: {INSTALL_MEDIA}")
    with Membase(local=tmp_path / "s") as m:
        assert m.media.list() == {"media": []}  # nothing stored, nothing to load
        with pytest.raises(UnprocessableEntityError, match=re.escape(INSTALL_MEDIA)):
            m.media.add(_png(tmp_path, "red", "r.png"))


def test_cli_media_commands(tmp_path, capsys, media_ready, monkeypatch, llm):
    from membase.local.media import LocalMedia

    real_engine = LocalMedia.engine

    def engine(self, cid):
        self._shared = self._shared or (MediaConfig(), llm, FakeClip())
        return real_engine(self, cid)

    monkeypatch.setattr(LocalMedia, "engine", engine)
    store = ["--store", str(tmp_path / "s")]
    assert cli.main([*store, "media", "add", str(_png(tmp_path, "red", "r.png"))]) == 0
    media_id = json.loads(capsys.readouterr().out)["media_id"]
    assert cli.main([*store, "media", "search", "red", "--kind", "image"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["id"] == media_id
    assert cli.main([*store, "media", "delete", media_id]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "confirmation_required"


def test_cli_media_is_local_only(monkeypatch):
    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_test")
    monkeypatch.delenv("MEMBASE_LOCAL", raising=False)
    with pytest.raises(SystemExit, match="media memory is local"):
        cli.main(["media", "list"])


def test_the_http_server_answers_the_media_routes(tmp_path, media_ready, llm):
    httpd = make_server(port=0, root=str(tmp_path / "srv"))
    httpd.backend.media._shared = (MediaConfig(), llm, FakeClip())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        remote = Membase("local", base_url=f"http://127.0.0.1:{httpd.server_address[1]}", max_retries=0)
        out = remote.media.add(_png(tmp_path, "red", "r.png"))
        assert remote.media.search("red")["results"][0]["id"] == out["media_id"]
        remote.close()
    finally:
        httpd.shutdown()
        httpd.backend.close()


def test_mcp_offers_media_tools_only_locally(tmp_path, media_ready, monkeypatch):
    pytest.importorskip("mcp", reason="needs the mcp library")
    from membase.mcp import build_server

    def names(client) -> set[str]:
        return {t.name for t in asyncio.run(build_server(client).list_tools())}

    media_tools = {"add_media", "list_media", "get_media", "search_media", "ask_media", "delete_media"}
    with Membase(local=tmp_path / "s") as m:
        assert media_tools <= names(m)
    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_test")
    with Membase() as hosted:
        assert not media_tools & names(hosted)
    monkeypatch.setattr(requirements, "media_unavailable", lambda: "no")
    with Membase(local=tmp_path / "s") as m:
        assert not media_tools & names(m)
