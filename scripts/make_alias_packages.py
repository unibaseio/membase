"""Write the alias and placeholder packages that hold the Membase names on PyPI and npm.

Real packages: unibaseio-membase, unibaseio-membase-core (PyPI); @unibaseio/membase,
@unibaseio/membase-cli (npm). Each other name installs the real package (alias) or, for a
project not on the registry (membase-bench), installs nothing and links to it (placeholder).
PyPI membase-ai / membase-sdk / membase-core get no release: an alias would share their files,
and pip's upgrade would delete them.

    python scripts/make_alias_packages.py OUT_DIR

writes OUT_DIR/pypi/<name>/ (build each with `uv build`) and OUT_DIR/npm/<name>/ (publish each
with `npm publish ./OUT_DIR/npm/<name>`).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

VERSION = "0.3.0"  # aliases follow the real packages
PLACEHOLDER = "0.0.1"
HOME = "https://membase.ai"
SDK, CORE = "unibaseio-membase", "unibaseio-membase-core"
NPM_SDK, NPM_CLI = "@unibaseio/membase", "@unibaseio/membase-cli"
CORE_EXTRAS = ("multimodal", "anthropic", "ollama", "all")


def pypi(name: str) -> str:
    return f"https://pypi.org/project/{name}/"


def npm(name: str) -> str:
    return f"https://www.npmjs.com/package/{name}"


# name: (what it installs, extras passed through, requires-python)
PYPI_ALIASES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "unibaseio": (SDK, ("local",), ">=3.10"),
    "unibaseio-membase-sdk": (SDK, ("local",), ">=3.10"),
    "unibaseio-membase-cli": (SDK, ("local",), ">=3.10"),
    "unibase-membase": (SDK, ("local",), ">=3.10"),
    "unibase-membase-cli": (SDK, ("local",), ">=3.10"),
    "membase-cli": (SDK, ("local",), ">=3.10"),
    "unibaseio-membase-mcp": (f"{SDK}[local]", (), ">=3.10"),
    "unibase-membase-mcp": (f"{SDK}[local]", (), ">=3.10"),
    "membase-mcp": (f"{SDK}[local]", (), ">=3.10"),
    "unibase-membase-core": (CORE, CORE_EXTRAS, ">=3.12"),
    "membase-engine": (CORE, CORE_EXTRAS, ">=3.12"),
    "membase-algo": (CORE, CORE_EXTRAS, ">=3.12"),
}

# name: (link text, link)
PYPI_PLACEHOLDERS: dict[str, tuple[str, str]] = {
    "membase-bench": ("membase-bench", "https://github.com/unibaseio/membase-bench"),
    "unibaseio-membase-bench": ("membase-bench", "https://github.com/unibaseio/membase-bench"),
}

NPM_ALIASES = ("membase-ai", "membase-sdk", "unibaseio-membase", "unibase-membase", "unibaseio")
NPM_CLI_ALIASES = ("membase-cli",)
# name: (link text, link)
NPM_PLACEHOLDERS: dict[str, tuple[str, str]] = {
    "membase-mcp": (f"{SDK}[local]", pypi(SDK)),
    "membase-core": (CORE, pypi(CORE)),
    "membase-engine": (CORE, pypi(CORE)),
}


def _readme(name: str, text: str, link: str, install: str | None) -> str:
    cmd = f"\n```bash\n{install}\n```\n" if install else ""
    return f"# {name}\n\nUse [{text}]({link}).\n{cmd}"


def _pyproject(name: str, version: str, summary: str, requires_python: str,
               deps: list[str], extras: dict[str, list[str]]) -> str:
    def toml_list(items: list[str]) -> str:
        return "[" + ", ".join(json.dumps(i) for i in items) + "]"

    extra_lines = "".join(f"{k} = {toml_list(v)}\n" for k, v in extras.items())
    return f'''[project]
name = "{name}"
version = "{version}"
description = {json.dumps(summary)}
readme = "README.md"
requires-python = "{requires_python}"
license = {{ text = "Proprietary" }}
authors = [{{ name = "Unibase" }}]
dependencies = {toml_list(deps)}
{"[project.optional-dependencies]" + chr(10) + extra_lines if extras else ""}
[project.urls]
Homepage = "{HOME}"

[build-system]
requires = ["hatchling<1.28"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
bypass-selection = true
'''


def write_pypi(out: Path) -> None:
    for name, (target, extras, py) in PYPI_ALIASES.items():
        base = target.split("[")[0]
        d = out / "pypi" / name
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text(_pyproject(
            name, VERSION, f"Use {target}.", py, [f"{target}>={VERSION}"],
            {e: [f"{base}[{e}]>={VERSION}"] for e in extras}))
        (d / "README.md").write_text(_readme(name, target, pypi(base), f"pip install '{target}'"))
    for name, (text, link) in PYPI_PLACEHOLDERS.items():
        d = out / "pypi" / name
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text(_pyproject(name, PLACEHOLDER, f"Use {text}: {link}", ">=3.10", [], {}))
        (d / "README.md").write_text(_readme(name, text, link, None))


def _package_json(name: str, version: str, description: str, **extra: object) -> str:
    pkg = {"name": name, "version": version, "description": description,
           "license": "UNLICENSED", "homepage": HOME, "type": "module", **extra}
    return json.dumps(pkg, indent=2) + "\n"


def write_npm(out: Path) -> None:
    for name in NPM_ALIASES:
        d = out / "npm" / name
        d.mkdir(parents=True)
        (d / "package.json").write_text(_package_json(
            name, VERSION, f"Use {NPM_SDK}.", main="./index.js", types="./index.d.ts",
            files=["index.js", "index.d.ts", "README.md"], dependencies={NPM_SDK: f"^{VERSION}"}))
        (d / "index.js").write_text(f'export * from "{NPM_SDK}";\n')
        (d / "index.d.ts").write_text(f'export * from "{NPM_SDK}";\n')
        (d / "README.md").write_text(_readme(name, NPM_SDK, npm(NPM_SDK), f"npm install {NPM_SDK}"))
    for name in NPM_CLI_ALIASES:
        d = out / "npm" / name
        d.mkdir(parents=True)
        (d / "package.json").write_text(_package_json(
            name, VERSION, f"Use {NPM_CLI}.", bin={"membase": "./cli.js"}, files=["cli.js", "README.md"],
            dependencies={NPM_CLI: f"^{VERSION}"}))
        (d / "cli.js").write_text(f'#!/usr/bin/env node\nimport "{NPM_CLI}/dist/cli.js";\n')
        (d / "README.md").write_text(_readme(name, NPM_CLI, npm(NPM_CLI), f"npm install -g {NPM_CLI}"))
    for name, (text, link) in NPM_PLACEHOLDERS.items():
        d = out / "npm" / name
        d.mkdir(parents=True)
        (d / "package.json").write_text(_package_json(
            name, PLACEHOLDER, f"Use {text}: {link}", main="./index.js", files=["index.js", "README.md"]))
        (d / "index.js").write_text(f"throw new Error({json.dumps(f'Use {text}: {link}')});\n")
        (d / "README.md").write_text(_readme(name, text, link, None))


def main(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    write_pypi(out)
    write_npm(out)
    print(f"{len(PYPI_ALIASES) + len(PYPI_PLACEHOLDERS)} PyPI and "
          f"{len(NPM_ALIASES) + len(NPM_CLI_ALIASES) + len(NPM_PLACEHOLDERS)} npm packages in {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
