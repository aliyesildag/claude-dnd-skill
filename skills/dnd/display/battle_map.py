#!/usr/bin/env python3
"""The tactical grid, drawn from its own spec.

A fight on a grid needs two things to agree: what the rules engine thinks the
board is, and what the players see. The usual way to get that is a map image
with a grid painted over it, and then a long argument about whether the
image's edges really are the grid's edges. This module refuses the argument:
there is no image. The spec that `grid.py` does its arithmetic on — columns,
rows, terrain — is also the only thing that gets drawn. Whatever the players
see, the pathfinder saw first.

Positions are not stored here. The display keeps one `state` dict (handle,
spec, round, tokens) and hands it in; this module turns it into SVG and a
caption, twice when it has to — once with the hidden tokens left out, for the
seats, and once with them in, for the DM screen. The two views come out of
the same function with one flag, so they cannot drift.
"""

from __future__ import annotations

import html

from grid import expand_tiles, parse_tile, tile_name, validate_spec

# Sizes in tile units — the viewBox is the grid, so 1.0 is one 5-ft square.
TOKEN_R = 0.38
MARGIN = 0.9

# What a kind of terrain looks like. Kinds are free text on the spec, written
# by the DM in whatever language the table speaks, so both are listed; anything
# unrecognised falls back to its flags (difficult → hatched, impassable → solid).
KIND_FILL = {
    "water": "#1c3542", "su": "#1c3542", "river": "#1c3542", "nehir": "#1c3542",
    "stream": "#1c3542", "dere": "#1c3542", "pool": "#1c3542", "gölet": "#1c3542",
    "rubble": "#3a3128", "moloz": "#3a3128", "debris": "#3a3128", "enkaz": "#3a3128",
    "mud": "#33291c", "çamur": "#33291c", "bog": "#2c2f1e", "bataklık": "#2c2f1e",
    "wall": "#2b2420", "duvar": "#2b2420", "pillar": "#2b2420", "sütun": "#2b2420",
    "rock": "#2e2924", "kaya": "#2e2924", "boulder": "#2e2924",
    "tree": "#22301f", "ağaç": "#22301f", "forest": "#22301f", "orman": "#22301f",
    "bush": "#26331f", "çalı": "#26331f", "undergrowth": "#26331f",
    "fire": "#4a1f12", "ateş": "#4a1f12", "lava": "#4a1f12", "ember": "#4a1f12",
    "pit": "#080706", "çukur": "#080706", "chasm": "#080706", "uçurum": "#080706",
    "door": "#4a3a22", "kapı": "#4a3a22", "gate": "#4a3a22",
    "stairs": "#3d3327", "merdiven": "#3d3327", "steps": "#3d3327",
    "altar": "#3b2e3c", "sunak": "#3b2e3c",
    "table": "#3d2f1f", "masa": "#3d2f1f", "bed": "#3d2f1f",
}
DIFFICULT_FILL = "#2f2820"
IMPASSABLE_FILL = "#2b2420"

PC_FILL = "#d4b24c"
NPC_FILL = "#b0432f"


def _fold(s: str) -> str:
    return str(s or "").strip().lower()


def _terrain_tiles(spec: dict):
    """Yield (col, row, fill, difficult, impassable) for every terrain tile."""
    for t in spec.get("terrain", []):
        kind = _fold(t.get("kind"))
        difficult = bool(t.get("difficult"))
        impassable = bool(t.get("impassable"))
        fill = KIND_FILL.get(kind) or (
            IMPASSABLE_FILL if impassable else DIFFICULT_FILL if difficult else None)
        if fill is None:
            continue          # decorative terrain with no look and no rule: skip
        for (c, r) in expand_tiles(t["tiles"]):
            yield c, r, fill, difficult, impassable


def _initial(name: str) -> str:
    name = str(name or "").strip()
    return html.escape(name[:1].upper()) if name else "?"


def render_svg(state: dict, active: str = "", show_hidden: bool = False) -> str:
    """The board as one SVG string.

    `active` is the name whose turn it is, drawn with a bright ring — read off
    the display's turn order by the caller, never stored on the map.
    `show_hidden` is the DM screen: hidden tokens are drawn dashed and dim so
    the DM can tell at a glance what the table cannot see. For the seats they
    are not drawn, and — this matters — not present in the string at all.
    """
    spec = state["spec"]
    cols, rows = int(spec["cols"]), int(spec["rows"])
    parts = []

    parts.append(f'<rect x="0" y="0" width="{cols}" height="{rows}" fill="#14100c"/>')

    for c, r, fill, difficult, impassable in _terrain_tiles(spec):
        parts.append(f'<rect x="{c}" y="{r}" width="1" height="1" fill="{fill}"/>')
        if difficult and not impassable:
            parts.append(f'<rect x="{c}" y="{r}" width="1" height="1" fill="url(#hatch)"/>')
        if impassable:
            parts.append(f'<rect x="{c + 0.08}" y="{r + 0.08}" width="0.84" height="0.84" '
                         f'class="block"/>')

    for c in range(cols + 1):
        parts.append(f'<line x1="{c}" y1="0" x2="{c}" y2="{rows}"/>')
    for r in range(rows + 1):
        parts.append(f'<line x1="0" y1="{r}" x2="{cols}" y2="{r}"/>')

    for c in range(cols):
        parts.append(f'<text x="{c + 0.5}" y="-0.28" class="lbl">{chr(ord("A") + c)}</text>')
    for r in range(rows):
        parts.append(f'<text x="-0.45" y="{r + 0.68}" class="lbl">{r + 1}</text>')

    want_active = _fold(active)
    drawn = []
    for tok in state.get("tokens", []):
        if tok.get("hidden") and not show_hidden:
            continue
        pos = tok.get("pos")
        if not pos:
            continue
        try:
            col, row = parse_tile(pos)
        except ValueError:
            continue
        if not (0 <= col < cols and 0 <= row < rows):
            continue
        drawn.append((col, row, tok))
    occupied = {(c, r) for c, r, _ in drawn}
    for col, row, tok in drawn:
        cx, cy = col + 0.5, row + 0.5
        # Names alternate under / over along a run of adjacent tokens, so a
        # line of four fighters reads as four names instead of one smear.
        # Position in the run decides, not just "is my left neighbour taken":
        # that rule puts the 2nd and 3rd both on top, back into one smear.
        run = 0
        while (col - 1 - run, row) in occupied:
            run += 1
        name_y = cy - 0.5 if run % 2 else cy + 0.72
        fill = PC_FILL if tok.get("type") == "pc" else NPC_FILL
        cls = ["token"]
        if tok.get("hidden"):
            cls.append("hidden")
        if want_active and _fold(tok.get("name")) == want_active:
            cls.append("active")
            parts.append(f'<circle class="halo" cx="{cx}" cy="{cy}" r="{TOKEN_R + 0.18}"/>')
        parts.append(f'<circle class="{" ".join(cls)}" cx="{cx}" cy="{cy}" '
                     f'r="{TOKEN_R}" fill="{fill}"/>')
        parts.append(f'<text x="{cx}" y="{cy + 0.13}" class="ini">{_initial(tok.get("name"))}</text>')
        parts.append(f'<text x="{cx}" y="{name_y}" class="name">'
                     f'{html.escape(str(tok.get("name", ""))[:14])}</text>')

    # Legend — one swatch per kind on this board, with the rule it carries.
    # A colour nobody can read is decoration; the rule is what the DM will
    # adjudicate by, so it is written next to the swatch, not left to memory.
    legend = []
    seen = set()
    for t in spec.get("terrain", []):
        kind = _fold(t.get("kind")) or ("geçilmez" if t.get("impassable")
                                        else "zor arazi" if t.get("difficult") else "")
        if not kind or kind in seen:
            continue
        seen.add(kind)
        fill = KIND_FILL.get(kind) or (IMPASSABLE_FILL if t.get("impassable")
                                       else DIFFICULT_FILL if t.get("difficult") else None)
        if fill is None:
            continue
        rule = ("geçilmez" if t.get("impassable") else "zor arazi" if t.get("difficult") else "")
        legend.append((kind, fill, rule, bool(t.get("difficult")), bool(t.get("impassable"))))
    # Laid out left to right and wrapped at the board's edge, because a
    # legend that runs off the canvas explains exactly nothing.
    ly = rows + 0.55
    lx = 0.0
    legend_rows = 1 if legend else 0
    for kind, fill, rule, difficult, impassable in legend:
        text = kind + (f" — {rule}" if rule else "")
        width = 0.9 + 0.17 * len(text)
        if lx > 0 and lx + width > cols:
            lx, ly = 0.0, ly + 0.75
            legend_rows += 1
        parts.append(f'<rect x="{lx}" y="{ly - 0.28}" width="0.5" height="0.5" fill="{fill}"/>')
        if difficult and not impassable:
            parts.append(f'<rect x="{lx}" y="{ly - 0.28}" width="0.5" height="0.5" fill="url(#hatch)"/>')
        if impassable:
            parts.append(f'<rect x="{lx + 0.05}" y="{ly - 0.23}" width="0.4" height="0.4" class="block"/>')
        parts.append(f'<text x="{lx + 0.65}" y="{ly + 0.1}" class="leg">{html.escape(text)}</text>')
        lx += width
    bottom_pad = MARGIN + 0.75 * legend_rows

    style = (
        'line{stroke:rgba(232,220,200,.26);stroke-width:.03}'
        '.lbl{fill:#c9a86a;font-size:.32px;text-anchor:middle;font-family:system-ui,sans-serif}'
        '.name{fill:#e8dcc8;font-size:.21px;text-anchor:middle;font-family:system-ui,sans-serif;'
        'paint-order:stroke;stroke:#0b0908;stroke-width:.06px}'
        '.ini{fill:#0b0908;font-size:.3px;font-weight:700;text-anchor:middle;'
        'font-family:system-ui,sans-serif}'
        '.token{stroke:#0b0908;stroke-width:.06}'
        '.token.active{stroke:#ffe4a8;stroke-width:.09}'
        '.token.hidden{stroke-dasharray:.12 .08;opacity:.6}'
        '.halo{fill:none;stroke:#ffe4a8;stroke-width:.05;opacity:.45}'
        '.block{fill:none;stroke:rgba(0,0,0,.55);stroke-width:.06}'
        '.leg{fill:#b7a488;font-size:.3px;font-family:system-ui,sans-serif}'
    )
    defs = (
        '<defs><pattern id="hatch" width=".25" height=".25" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2=".25" '
        'stroke="rgba(232,220,200,.13)" stroke-width=".06"/></pattern></defs>'
    )
    return (f'<svg viewBox="{-MARGIN} {-MARGIN} {cols + 2 * MARGIN} {rows + MARGIN + bottom_pad}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img">'
            f'<style>{style}</style>{defs}{"".join(parts)}</svg>')


def caption(state: dict, show_hidden: bool = False) -> str:
    """The line under the board: which map, which round, who is off it."""
    bits = [str(state.get("handle") or state["spec"].get("handle") or "harita")]
    rnd = state.get("round")
    if rnd:
        bits.append(f"tur {rnd}")
    unplaced = [str(t.get("name", "")) for t in state.get("tokens", [])
                if not t.get("pos") and (show_hidden or not t.get("hidden"))]
    if unplaced:
        bits.append("harita dışı: " + ", ".join(unplaced))
    return " · ".join(bits)


def has_hidden(state: dict) -> bool:
    return any(t.get("hidden") for t in state.get("tokens", []))


def views(state: dict, active: str = "") -> "tuple[dict, dict | None]":
    """(what every seat gets, what only the DM screen gets or None).

    The second is None whenever nothing is hidden, so the common case is one
    payload and the DM screen never has to reconcile two.
    """
    base = {"handle": state.get("handle") or state["spec"].get("handle", ""),
            "cols": int(state["spec"]["cols"]), "rows": int(state["spec"]["rows"]),
            "round": state.get("round") or 0}
    public = dict(base, svg=render_svg(state, active, show_hidden=False),
                  label=caption(state, show_hidden=False))
    if not has_hidden(state):
        return public, None
    full = dict(base, svg=render_svg(state, active, show_hidden=True),
                label=caption(state, show_hidden=True))
    return public, full


def check_spec(spec) -> "list[str]":
    """grid.py's validation, re-exported so the endpoint has one import."""
    return validate_spec(spec)


__all__ = ["render_svg", "caption", "views", "has_hidden", "check_spec",
           "parse_tile", "tile_name"]
