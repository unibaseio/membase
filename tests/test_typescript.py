"""The npm packages against `membase serve`: the client's src/local.test.ts, and the built
`membase` command of @unibaseio/membase-cli."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

pytest.importorskip("membase_core", reason="needs the local extra")

ROOT = Path(__file__).resolve().parent.parent
TS = ROOT / "typescript"
CLI = ROOT / "cli" / "dist" / "cli.js"


# npm workspaces: `npm ci` at the repository root installs both packages.
@pytest.mark.skipif(shutil.which("npx") is None or not (ROOT / "node_modules" / ".bin" / "vitest").exists(),
                    reason="needs node and `npm ci` at the repository root")
def test_typescript_client_against_local_server(tmp_path):
    from membase.local.server import make_server

    httpd = make_server(port=0, root=str(tmp_path / "store"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        env = {**os.environ, "MEMBASE_LOCAL_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}"}
        r = subprocess.run(["npx", "vitest", "run", "src/local.test.ts"], cwd=TS, env=env,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "2 passed" in r.stdout, r.stdout
    finally:
        httpd.shutdown()
        httpd.backend.close()


@pytest.mark.skipif(shutil.which("node") is None or not CLI.exists() or not (TS / "dist" / "index.js").exists(),
                    reason="needs `npm ci && npm run build -w typescript && npm run build -w cli`")
def test_npm_cli_against_local_server(tmp_path):
    import json

    from membase.local.server import make_server

    httpd = make_server(port=0, root=str(tmp_path / "store"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = ["node", str(CLI), "--base-url", f"http://127.0.0.1:{httpd.server_address[1]}", "--api-key", "local"]

    def membase(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*base, *args], capture_output=True, text=True, timeout=300)

    try:
        r = membase("memories", "list", "--limit", "5")
        assert r.returncode == 0 and json.loads(r.stdout) == {"items": [], "has_more": False, "partial": []}, r.stderr
        r = membase("documents", "list", "--kind", "note")
        assert r.returncode == 0 and json.loads(r.stdout)["items"] == [], r.stderr
        assert "memories.add" in json.loads(membase("capabilities").stdout)["actions"]
        r = membase("documents", "get", "doc_missing")
        assert r.returncode == 1 and r.stderr.startswith("error: "), r.stdout + r.stderr
        r = membase("search", "x", "--container", "default")
        assert r.returncode == 2, r.stdout + r.stderr
        r = membase("serve")
        assert r.returncode == 2 and "unibaseio-membase[local]" in r.stderr
    finally:
        httpd.shutdown()
        httpd.backend.close()
