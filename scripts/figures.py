"""Render the README diagrams in assets/."""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "assets"

BLUE, DEEP, LIGHT, PALE = "#3E61FF", "#2A3FBF", "#9DB0FF", "#E3E8FF"
NIGHT, SUB, LINE = "#12162B", "#5B6280", "#A3ACD1"
FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif'
MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
W, PAD = 840, 24


def _svg(height: int, body: str, label: str, eyebrow: str = "", width: int = W, panel: bool = True) -> str:
    bg = ""
    if panel:
        bg = f'<rect width="{width}" height="{height}" rx="20" fill="url(#p)"/>'
    if eyebrow:
        bg += (f'<text x="{PAD}" y="38" style="font-family:{MONO};font-size:12px;letter-spacing:.14em;'
               f'fill:{BLUE}">{eyebrow}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{label}"><defs>'
            f'<marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{LINE}"/></marker>'
            '<linearGradient id="p" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#E9EDFF"/>'
            '<stop offset=".55" stop-color="#F6F7FD"/><stop offset="1" stop-color="#E4E1FB"/></linearGradient>'
            f'</defs><style>text{{font-family:{FONT}}}</style>{bg}{body}</svg>\n')


def _card(x, y, w, h, inner="", fill="#FFFFFF", rx=14):
    return (f'<rect x="{x + 2}" y="{y + 6}" width="{w}" height="{h}" rx="{rx}" fill="{DEEP}" opacity=".07"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"/>{inner}')


def _node(x, y, w, h, title, sub="", solid=False, mono=False):
    color, sub_color = ("#FFFFFF", "#DCE3FF") if solid else (NIGHT, SUB)
    font = f"font-family:{MONO};font-size:14px;font-weight:600" if mono else "font-size:15px;font-weight:700"
    ty = y + h / 2 + (-3 if sub else 5)
    inner = f'<text x="{x + w / 2}" y="{ty}" text-anchor="middle" style="{font};fill:{color}">{title}</text>'
    if sub:
        inner += (f'<text x="{x + w / 2}" y="{ty + 20}" text-anchor="middle" '
                  f'style="font-family:{MONO};font-size:12px;fill:{sub_color}">{sub}</text>')
    return _card(x, y, w, h, inner, BLUE if solid else "#FFFFFF")


def _arrow(x1, y1, x2, y2):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{LINE}" stroke-width="1.8" '
            f'marker-end="url(#a)"/>')


def _line(x1, y1, x2, y2, color=LINE, dash=""):
    extra = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.8"{extra}/>'


def _logo(cx, cy, r):
    return (f'<circle cx="{cx}" cy="{cy}" r="{r + 9}" fill="#FFFFFF" stroke="{LIGHT}" stroke-width="2" opacity=".9"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{BLUE}"/>'
            f'<path d="M{cx - r * .32} {cy - r * .38} v{r * .32} M{cx + r * .12} {cy - r * .38} v{r * .32}" '
            f'stroke="#FFFFFF" stroke-width="{r * .1:.1f}" stroke-linecap="round"/>'
            f'<path d="M{cx - r * .55} {cy + r * .08} q{r * .55} {r * .62} {r * 1.1} {-r * .1}" fill="none" '
            f'stroke="#FFFFFF" stroke-width="{r * .1:.1f}" stroke-linecap="round"/>')


def hero() -> str:
    sources = ["Documents and pages", "Facts you state", "Chat exports", "Uploads and connected apps"]
    readers = ["Your app", "MCP clients", "Your assistant", "The command line"]
    cy, body = 177, []
    for x, word in ((PAD + 118, "ADD"), (420, "MANAGE"), (W - PAD - 118, "USE")):
        body.append(f'<text x="{x}" y="38" text-anchor="middle" style="font-family:{MONO};font-size:12px;'
                    f'letter-spacing:.14em;fill:{BLUE}">{word}</text>')
    for i, (src, reader) in enumerate(zip(sources, readers)):
        y = 56 + i * 62
        body.append(_node(PAD, y, 236, 48, src))
        body.append(_node(W - PAD - 236, y, 236, 48, reader, solid=True))
        body.append(_arrow(PAD + 240, y + 24, 356, cy + (i - 1.5) * 16))
        body.append(_arrow(484, cy + (i - 1.5) * 16, W - PAD - 240, y + 24))
    body.append(_logo(420, cy - 10, 40))
    body.append(f'<text x="420" y="{cy + 66}" text-anchor="middle" style="font-size:17px;font-weight:700;'
                f'fill:{BLUE}">One memory</text>')
    return _svg(328, "".join(body), "Add sources, manage one memory, use it from every AI and app")


def architecture() -> str:
    cw, gap = 236, (W - 2 * PAD - 3 * 236) / 2
    xs = [PAD + i * (cw + gap) for i in range(3)]
    ys, h = [60, 138, 216], 60
    body = [_node(xs[0], ys[0], cw, h, "Membase()", mono=True),
            _node(xs[1], ys[0], cw, h, "Hosted API", "api.app.membase.com"),
            _node(xs[2], ys[0], cw, h, "Your Membase account", solid=True),
            _node(xs[0], ys[1], cw, h, "Membase(local=True)", mono=True),
            _node(xs[1], ys[1], cw, h, "unibaseio-membase-core", "in your process"),
            _node(xs[0], ys[2], cw, h, "Other languages"),
            _node(xs[1], ys[2], cw, h, "membase serve", "127.0.0.1:8787/v1"),
            _node(xs[2], ys[1], cw, ys[2] + h - ys[1], "~/.membase", "on your machine", solid=True)]
    for y in ys:
        body.append(_arrow(xs[0] + cw + 4, y + h / 2, xs[1] - 6, y + h / 2))
        body.append(_arrow(xs[1] + cw + 4, y + h / 2, xs[2] - 6, y + h / 2))
    return _svg(300, "".join(body), "Hosted, local in process, or local over HTTP", "WHERE THE MEMORY LIVES")


def _episodes(cx, top):
    out = ""
    for j, w in enumerate([96, 72, 108]):
        y = top + j * 22
        out += (f'<rect x="{cx - 80}" y="{y}" width="160" height="16" rx="8" fill="{PALE}"/>'
                f'<circle cx="{cx - 68}" cy="{y + 8}" r="4" fill="{BLUE}"/>'
                f'<rect x="{cx - 56}" y="{y + 6}" width="{w}" height="4" rx="2" fill="{LIGHT}"/>')
    return out


def _tree(cx, top):
    nodes = [(cx, top, 52, BLUE), (cx - 46, top + 26, 44, LIGHT), (cx + 46, top + 26, 44, LIGHT),
             (cx - 70, top + 52, 36, PALE), (cx - 22, top + 52, 36, PALE), (cx + 46, top + 52, 36, PALE)]
    edges = [(0, 1), (0, 2), (1, 3), (1, 4), (2, 5)]
    out = "".join(_line(nodes[a][0], nodes[a][1] + 14, nodes[b][0], nodes[b][1], LIGHT) for a, b in edges)
    return out + "".join(f'<rect x="{x - w / 2}" y="{y}" width="{w}" height="14" rx="7" fill="{c}"/>'
                         for x, y, w, c in nodes)


def _cases(cx, top):
    out = ""
    for k, dx in enumerate([-84, -72]):
        out += f'<rect x="{cx + dx}" y="{top + k * 8}" width="72" height="54" rx="8" fill="{PALE}" stroke="#FFFFFF" stroke-width="2"/>'
    for j in range(3):
        out += f'<rect x="{cx - 62}" y="{top + 20 + j * 10}" width="{48 - j * 10}" height="4" rx="2" fill="{LIGHT}"/>'
    out += _arrow(cx + 4, top + 34, cx + 22, top + 34)
    out += (f'<rect x="{cx + 26}" y="{top + 14}" width="58" height="40" rx="8" fill="{BLUE}"/>'
            f'<path d="M{cx + 44} {top + 34} l7 7 l13 -14" stroke="#FFFFFF" stroke-width="3" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round"/>')
    return out


def memory_types() -> str:
    cw, gap = 248, 24
    items = [("A conversation", "dated episodes", _episodes), ("A document", "a topic tree", _tree),
             ("An agent trace", "cases and skills", _cases)]
    body = []
    for i, (src, dst, draw) in enumerate(items):
        x = PAD + i * (cw + gap)
        cx = x + cw / 2
        inner = (f'<text x="{cx}" y="96" text-anchor="middle" style="font-size:15px;font-weight:700;fill:{NIGHT}">{src}</text>'
                 + _arrow(cx, 108, cx, 130) + draw(cx, 142)
                 + f'<text x="{cx}" y="236" text-anchor="middle" style="font-size:15px;font-weight:700;fill:{BLUE}">{dst}</text>')
        body.append(_card(x, 60, cw, 200, inner))
    return _svg(284, "".join(body), "Conversations, documents and agent traces", "WHAT EACH INPUT BECOMES")


def mcp() -> str:
    cw = (W - 2 * PAD - 3 * 16) / 4
    body = []
    for i, c in enumerate(["Claude Code", "Cursor", "Codex", "any MCP client"]):
        x = PAD + i * (cw + 16)
        body.append(_node(x, 56, cw, 44, c, solid=i < 3))
        body.append(_line(x + cw / 2, 102, x + cw / 2, 120))
    body.append(_line(PAD + cw / 2, 120, W - PAD - cw / 2, 120))
    sw = (W - 2 * PAD - 24) / 2
    for i, (title, sub) in enumerate([("Hosted", "api.app.membase.com/mcp-http"), ("Local", "membase --local mcp")]):
        x = PAD + i * (sw + 24)
        body.append(_arrow(x + sw / 2, 120, x + sw / 2, 138))
        body.append(_node(x, 142, sw, 60, title, sub))
        body.append(_arrow(x + sw / 2, 206, x + sw / 2, 230))
    tools = ["search_memories", "get_profile", "memory_rules", "list_containers", "list_documents",
             "add_memory", "add_document", "forget_memory", "delete_document", "ask_agent"]
    tw = (W - 2 * PAD - 4 * 12) / 5
    for i, tool in enumerate(tools):
        x, y = PAD + (i % 5) * (tw + 12), 236 + (i // 5) * 42
        body.append(_card(x, y, tw, 34, f'<text x="{x + tw / 2}" y="{y + 22}" text-anchor="middle" '
                          f'style="font-family:{MONO};font-size:12.5px;fill:{DEEP}">{tool}</text>', fill=PALE, rx=9))
    return _svg(336, "".join(body), "MCP clients, hosted or local server, same tools", "ANY MCP CLIENT, SAME TOOLS")


def _window(x, y, w, h, title, lines):
    inner = (f'<circle cx="{x + 20}" cy="{y + 20}" r="5" fill="#FF5F57"/><circle cx="{x + 36}" cy="{y + 20}" r="5" fill="#FEBC2E"/>'
             f'<circle cx="{x + 52}" cy="{y + 20}" r="5" fill="#28C840"/>'
             f'<text x="{x + w / 2}" y="{y + 24}" text-anchor="middle" style="font-family:{MONO};font-size:12px;fill:#7C86B2">{title}</text>')
    for i, (color, text) in enumerate(lines):
        if text:
            inner += (f'<text x="{x + 22}" y="{y + 62 + i * 21}" style="font-family:{MONO};font-size:12.5px;fill:{color}" '
                      f'xml:space="preserve">{text}</text>')
    return _card(x, y, w, h, inner, NIGHT)


def in_action() -> str:
    code, out = "#E6E9F5", LIGHT
    left = [(code, "from membase import Membase"), (code, "m = Membase()"), ("", ""),
            (code, "m.memories.add("), (code, '    "We picked Postgres for the ledger",'),
            (code, '    container="Engineering")')]
    right = [(code, '$ claude -p "Which database is'), (code, '  the ledger on?"'), ("", ""),
             (out, "Postgres: you picked it for the"), (out, "ledger service, according to"),
             (out, "your Engineering memory.")]
    ww, y, h = 340, 52, 196
    label = ('<text x="{}" y="36" text-anchor="middle" style="font-size:14px;font-weight:700;'
             f'fill:{NIGHT}">{{}}</text>')
    body = [label.format(PAD + ww / 2, "Your app writes"), label.format(W - PAD - ww / 2, "Any AI reads"),
            _window(PAD, y, ww, h, "app.py", left), _window(W - PAD - ww, y, ww, h, "Claude Code", right),
            _line(PAD + ww + 2, y + h / 2, 381, y + h / 2, LIGHT, "5 5"),
            _line(459, y + h / 2, W - PAD - ww - 2, y + h / 2, LIGHT, "5 5"),
            _logo(420, y + h / 2, 28),
            (f'<text x="420" y="{y + h / 2 + 62}" text-anchor="middle" style="font-size:13px;font-weight:600;'
             f'fill:{SUB}">Engineering</text>')]
    return _svg(y + h + PAD, "".join(body), "Your app writes a memory; any AI reads it, here Claude Code")


FIGURES = {"hero": hero, "architecture": architecture, "memory-types": memory_types, "mcp": mcp, "in-action": in_action}


if __name__ == "__main__":
    for name, fn in FIGURES.items():
        (ASSETS / f"{name}.svg").write_text(fn())
        print(f"assets/{name}.svg")
