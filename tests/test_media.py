"""Media memory over the local routes, offline: the engine's real store, indexes and ffmpeg steps,
with stand-ins for the model calls."""

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
    ConfirmationRequiredError,
    Membase,
    NotFoundError,
    UnprocessableEntityError,
    WriteTargetRequiredError,
    cli,
)
from membase.local import ids  # noqa: E402
from membase.local.server import make_server  # noqa: E402
from membase.media import requirements  # noqa: E402
from membase.media.requirements import INSTALL_MEDIA, media_unavailable  # noqa: E402
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


@pytest.fixture
def media_ready(monkeypatch):
    monkeypatch.setattr(requirements, "media_unavailable", lambda: None)


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def local(tmp_path, media_ready, llm, monkeypatch):
    monkeypatch.delenv("MEMBASE_WRITE_TARGET", raising=False)
    m = Membase(local=tmp_path / "store")
    m._http._transport.backend.media._shared = (MediaConfig(), llm)
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


EMPTY_LIST = {"items": [], "has_more": False, "partial": []}


def test_an_empty_store_has_no_media(local):
    assert local.media.list() == EMPTY_LIST
    assert local.media.search("anything") == {"items": [], "partial": []}
    assert local.media.ask("what did I film?")["sources"] == []


def test_add_list_get_search_ask(local, tmp_path):
    out = local.media.add(_png(tmp_path, "red", "r.png"), title="Red card")
    assert out["id"].startswith("med_") and out["kind"] == "image" and out["memory_name"] == "default"
    assert out["summary"] == "a plain red square" and out["filename"] == "Red card"
    assert "container" not in out and "path" not in out
    local.media.add(_png(tmp_path, "blue", "b.png"))

    rows = local.media.list()
    assert [r["filename"] for r in rows["items"]] == ["b.png", "Red card"] and rows["has_more"] is False
    assert local.media.list(limit=1)["has_more"] is True
    item = local.media.get(out["id"])
    assert item["moments"] == [] and item["file_url"] == f"/v1/media/{out['id']}/file"
    assert item["thumb_url"] == item["file_url"]
    r = local._http.get(item["file_url"])
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG" and r.headers["content-type"] == "image/png"
    part = local._http.get(item["file_url"], headers={"Range": "bytes=1-3"})
    assert part.status_code == 206 and part.content == b"PNG"
    assert part.headers["content-range"].startswith("bytes 1-3/")

    hits = local.media.search("red square")
    assert hits["items"][0]["id"] == out["id"] and hits["partial"] == []
    assert local.media.search("red", kind="video")["items"] == []

    answer = local.media.ask("which card was red?")
    assert answer["answer"] == "from media memory" and answer["memory_name"] == "default"
    assert [s["id"] for s in answer["sources"]] == [out["id"]]


def test_media_is_apart_from_documents_and_memories(local, tmp_path):
    local.media.add(_png(tmp_path, "red", "r.png"))
    assert local.documents.list() == EMPTY_LIST
    assert local.search("red square")["items"] == []


def test_custom_ids_and_delete_with_confirmation(local, tmp_path):
    first = local.media.add(_png(tmp_path, "red", "r.png"), custom_id="card-1")
    again = local.media.add(_png(tmp_path, "red", "r.png"), custom_id="card-1")
    assert again["id"] == first["id"]
    assert local.media.list()["items"][0]["custom_id"] == "card-1"

    with pytest.raises(ConfirmationRequiredError) as e:
        local.media.delete(first["id"])
    assert e.value.details["verb"] == "delete_media" and e.value.details["id"] == first["id"]
    assert len(local.media.list()["items"]) == 1
    file_url = local.media.get(first["id"])["file_url"]
    assert local.media.delete(first["id"], confirm=True) == {"id": first["id"], "status": "deleted"}
    assert local.media.list() == EMPTY_LIST
    assert local._http.get(file_url).status_code == 404
    with pytest.raises(NotFoundError):
        local.media.get(first["id"])
    assert local.media.add(_png(tmp_path, "red", "r.png"), custom_id="card-1")["id"] != first["id"]


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
    with pytest.raises(UnprocessableEntityError):
        local.media.search("x", kind="pdf")
    with pytest.raises(UnprocessableEntityError):
        local.media.search("x", limit=99)
    with pytest.raises(NotFoundError):
        local.media.delete("med_missing", confirm=True)
    with pytest.raises(NotFoundError):
        local.media.get(ids.encode("media", "default:img_missing"))
    r = local._http.post("/v1/media/search", json={"q": "x", "threshold": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation"


def test_thumbnails_stay_inside_the_media_directory(local, tmp_path):
    out = local.media.add(_png(tmp_path, "red", "r.png"))
    raw = ids.decode("media", out["id"])[1]
    for name in ("%2e%2e/media.db", "other_frames/0001.jpg", f"{raw}_frames/%2e%2e/%2e%2e/media.db"):
        assert local._http.get(f"/v1/media/{out['id']}/thumbs/{name}").status_code == 404


def test_containers_keep_their_own_media(local, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMBASE_WRITE_TARGET", "Trips")
    trip = local.media.add(_png(tmp_path, "red", "r.png"))
    monkeypatch.setenv("MEMBASE_WRITE_TARGET", "Work")
    local.media.add(_png(tmp_path, "blue", "b.png"))
    assert trip["memory_name"] == "Trips"
    hits = local.media.search("square")
    assert {r["memory_name"] for r in hits["items"]} == {"Trips", "Work"}
    monkeypatch.delenv("MEMBASE_WRITE_TARGET")
    with pytest.raises(WriteTargetRequiredError):
        local.media.add(_png(tmp_path, "green", "g.png"))
    assert local.media.ask("which one is red?")["memory_name"] in {"Trips", "Work"}


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
    assert out["kind"] == "video" and len(out["moments"]) == 3

    item = local.media.get(out["id"])
    assert [(m["kind"], m["t"]) for m in item["moments"]] == [("scene", 0.0), ("scene", 3.0), ("speech", 3.5)]
    for m in item["moments"]:
        assert m["thumb_url"].startswith(f"/v1/media/{out['id']}/thumbs/")
        assert local._http.get(m["thumb_url"]).content[:2] == b"\xff\xd8"
    hit = local.media.search("part starts")["items"][0]
    assert hit["id"] == out["id"] and hit["moments"][0]["t"] == 3.5


def test_media_needs_its_extra(tmp_path, monkeypatch):
    reason = media_unavailable((3, 13), find=lambda name: None if name == "membase_media" else object())
    assert INSTALL_MEDIA in reason
    assert "Python 3.12" in media_unavailable((3, 11), find=lambda name: object())
    assert media_unavailable((3, 13), find=lambda name: object()) is None

    monkeypatch.setattr(requirements, "media_unavailable", lambda: f"media memory needs ...: {INSTALL_MEDIA}")
    with Membase(local=tmp_path / "s") as m:
        assert m.media.list() == EMPTY_LIST
        with pytest.raises(UnprocessableEntityError, match=re.escape(INSTALL_MEDIA)):
            m.media.add(_png(tmp_path, "red", "r.png"))


def test_cli_media_commands(tmp_path, capsys, media_ready, monkeypatch, llm):
    from membase.media.local import LocalMedia

    real_engine = LocalMedia.engine

    def engine(self, cid):
        self._shared = self._shared or (MediaConfig(), llm)
        return real_engine(self, cid)

    monkeypatch.setattr(LocalMedia, "engine", engine)
    store = ["--store", str(tmp_path / "s")]
    assert cli.main([*store, "media", "add", str(_png(tmp_path, "red", "r.png"))]) == 0
    media_id = json.loads(capsys.readouterr().out)["id"]
    assert cli.main([*store, "media", "search", "red", "--kind", "image"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["id"] == media_id
    assert cli.main([*store, "media", "delete", media_id]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "confirmation_required"
    assert cli.main([*store, "media", "list"]) == 0
    assert [i["id"] for i in json.loads(capsys.readouterr().out)["items"]] == [media_id]


def test_cli_media_add_and_ask_are_local_only(monkeypatch):
    monkeypatch.setenv("MEMBASE_API_KEY", "mbk_test")
    monkeypatch.delenv("MEMBASE_LOCAL", raising=False)
    with pytest.raises(SystemExit, match="media add is local"):
        cli.main(["media", "add", "--url", "https://example.com/a.png"])
    with pytest.raises(SystemExit, match="media ask is local"):
        cli.main(["media", "ask", "what did I film?"])


def test_the_http_server_answers_the_media_routes(tmp_path, media_ready, llm):
    httpd = make_server(port=0, root=str(tmp_path / "srv"))
    httpd.backend.media._shared = (MediaConfig(), llm)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        remote = Membase("local", base_url=f"http://127.0.0.1:{httpd.server_address[1]}", max_retries=0)
        out = remote.media.add(_png(tmp_path, "red", "r.png"))
        assert remote.media.search("red")["items"][0]["id"] == out["id"]
        r = remote._http.get(out["file_url"], headers={"Range": "bytes=0-3"})
        assert r.status_code == 206 and r.content == b"\x89PNG"
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
