#!/usr/bin/env python3
"""
grid.py — combat-grid math for mapped tactical scenes.

Stateless like combat.py: positions come in as arguments, verdicts go out on
stdout. The grid spec for a map lives at <campaign>/maps/<handle>.grid.json
(dims + terrain); positions themselves live in the combat STATE_JSON, never here.

Coordinates are chess-style tile labels: column letter (A-Z, left to right)
plus row number (1-99, top to bottom). Tiles are 5 ft. Diagonals cost 5 ft
(2014 PHB default), so distance is Chebyshev.

Usage:
    python3 grid.py validate <spec.json>
        Schema/bounds-check a grid spec. Prints VALID, or INVALID + errors (exit 1).

    python3 grid.py dist <tile> <tile>
        Distance in feet, e.g. `grid.py dist C4 F7` -> 15ft

    python3 grid.py move --from C4 --to F6 --speed 30 --spec <spec.json>
        Cheapest-path movement verdict (difficult terrain x2, impassable blocked):
        `OK cost=25ft`, or `ILLEGAL cost=40ft -- furthest reachable toward F6: E5 (30ft)`,
        or `UNREACHABLE F6`.

    python3 grid.py range --from C4 --to F7 --ft 60
        `IN RANGE (dist=15ft)` / `OUT OF RANGE (dist=70ft)`

    python3 grid.py aoe --shape sphere|cube|cone|line --origin D4 --size 20
                        [--dir N|NE|E|SE|S|SW|W|NW] --spec <spec.json>
        Affected tile list, clipped to the grid. Sphere/cube are exact on the
        5-ft-diagonal metric; cone/line are grid approximations (documented v1
        simplification). Cube supports cardinal --dir only.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import re
import sys
from pathlib import Path

TILE_FT = 5
MAX_COLS = 26
MAX_ROWS = 99

DIRS = {"N": (0, -1), "NE": (1, -1), "E": (1, 0), "SE": (1, 1),
        "S": (0, 1), "SW": (-1, 1), "W": (-1, 0), "NW": (-1, -1)}

_TILE = re.compile(r"^([A-Za-z])([1-9]\d?)$")


def _force_utf8_stdio() -> None:
    # Same guard as combat.py: Windows consoles default to cp1252.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


_force_utf8_stdio()


def parse_tile(label: str) -> tuple[int, int]:
    """'C4' -> (col, row) zero-based: (2, 3)."""
    m = _TILE.match(label.strip())
    if not m:
        raise ValueError(f"Bad tile label: {label!r}")
    return ord(m.group(1).upper()) - ord("A"), int(m.group(2)) - 1


def tile_name(col: int, row: int) -> str:
    return f"{chr(ord('A') + col)}{row + 1}"


def dist_ft(a: str, b: str) -> int:
    (c1, r1), (c2, r2) = parse_tile(a), parse_tile(b)
    return max(abs(c1 - c2), abs(r1 - r2)) * TILE_FT


def expand_tiles(expr: str) -> list[tuple[int, int]]:
    """'F1' -> [(5, 0)]; 'C3-D5' -> inclusive rectangle between the corners."""
    parts = expr.strip().split("-")
    if len(parts) == 1:
        return [parse_tile(parts[0])]
    if len(parts) != 2:
        raise ValueError(f"Bad tile range: {expr!r}")
    (c1, r1), (c2, r2) = parse_tile(parts[0]), parse_tile(parts[1])
    return [(c, r)
            for c in range(min(c1, c2), max(c1, c2) + 1)
            for r in range(min(r1, r2), max(r1, r2) + 1)]


def validate_spec(spec: dict) -> list[str]:
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    errors = []
    if not spec.get("handle"):
        errors.append("handle is required")
    cols, rows = spec.get("cols"), spec.get("rows")
    if isinstance(cols, bool) or not isinstance(cols, int) or not 1 <= cols <= MAX_COLS:
        errors.append(f"cols must be an int 1-{MAX_COLS}")
    if isinstance(rows, bool) or not isinstance(rows, int) or not 1 <= rows <= MAX_ROWS:
        errors.append(f"rows must be an int 1-{MAX_ROWS}")
    for i, t in enumerate(spec.get("terrain", [])):
        if not isinstance(t, dict) or "tiles" not in t:
            errors.append(f"terrain[{i}]: missing 'tiles'")
            continue
        try:
            tiles = expand_tiles(t["tiles"])
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            errors.append(f"terrain[{i}]: {e}")
            continue
        if isinstance(cols, int) and isinstance(rows, int):
            for (c, r) in tiles:
                if not (0 <= c < cols and 0 <= r < rows):
                    errors.append(
                        f"terrain[{i}]: {tile_name(c, r)} is outside the "
                        f"{cols}x{rows} grid")
                    break
    return errors


def load_spec(path) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_spec(spec)
    if errors:
        raise ValueError("; ".join(errors))
    return spec


def terrain_sets(spec: dict) -> tuple[set, set]:
    difficult, impassable = set(), set()
    for t in spec.get("terrain", []):
        for tile in expand_tiles(t["tiles"]):
            if t.get("difficult"):
                difficult.add(tile)
            if t.get("impassable"):
                impassable.add(tile)
    return difficult, impassable


def _step_cost(tile: tuple[int, int], difficult: set) -> int:
    return TILE_FT * 2 if tile in difficult else TILE_FT


def cheapest_path(spec: dict, from_label: str, to_label: str):
    """Dijkstra over the 8-connected grid. Cost = cost of the tile entered
    (difficult x2). Returns (cost_ft, path as [(col,row), ...] incl. both
    endpoints), or (None, []) if the target is unreachable/impassable."""
    cols, rows = spec["cols"], spec["rows"]
    difficult, impassable = terrain_sets(spec)
    start, end = parse_tile(from_label), parse_tile(to_label)
    for label, tile in ((from_label, start), (to_label, end)):
        if not (0 <= tile[0] < cols and 0 <= tile[1] < rows):
            raise ValueError(f"{label} is outside the {cols}x{rows} grid")
    if end in impassable:
        return None, []

    best = {start: 0}
    prev = {}
    pq = [(0, start)]
    while pq:
        d, cur = heapq.heappop(pq)
        if cur == end:
            break
        if d > best.get(cur, d):
            continue
        for dc in (-1, 0, 1):
            for dr in (-1, 0, 1):
                if dc == 0 and dr == 0:
                    continue
                nxt = (cur[0] + dc, cur[1] + dr)
                if not (0 <= nxt[0] < cols and 0 <= nxt[1] < rows):
                    continue
                if nxt in impassable:
                    continue
                nd = d + _step_cost(nxt, difficult)
                if nd < best.get(nxt, nd + 1):
                    best[nxt] = nd
                    prev[nxt] = cur
                    heapq.heappush(pq, (nd, nxt))
    if end not in best:
        return None, []
    path = [end]
    while path[-1] != start:
        path.append(prev[path[-1]])
    return best[end], list(reversed(path))


def move_verdict(spec: dict, from_label: str, to_label: str, speed: int) -> str:
    cost, path = cheapest_path(spec, from_label, to_label)
    if cost is None:
        return f"UNREACHABLE {to_label.strip().upper()}"
    if cost <= speed:
        return f"OK cost={cost}ft"
    difficult, _ = terrain_sets(spec)
    spent, reach = 0, path[0]
    for tile in path[1:]:
        step = _step_cost(tile, difficult)
        if spent + step > speed:
            break
        spent += step
        reach = tile
    return (f"ILLEGAL cost={cost}ft -- furthest reachable toward "
            f"{to_label.strip().upper()}: {tile_name(*reach)} ({spent}ft)")


def range_verdict(from_label: str, to_label: str, ft: int) -> str:
    d = dist_ft(from_label, to_label)
    return (f"IN RANGE (dist={d}ft)" if d <= ft
            else f"OUT OF RANGE (dist={d}ft)")


def aoe_tiles(spec: dict, shape: str, origin_label: str, size_ft: int,
              direction: str = "N") -> list[str]:
    cols, rows = spec["cols"], spec["rows"]
    o = parse_tile(origin_label)
    s = max(1, size_ft // TILE_FT)
    if direction not in DIRS:
        raise ValueError(f"Bad direction: {direction!r}")
    dc, dr = DIRS[direction]
    tiles = set()

    if shape == "sphere":
        for c in range(max(0, o[0] - s), min(cols, o[0] + s + 1)):
            for r in range(max(0, o[1] - s), min(rows, o[1] + s + 1)):
                tiles.add((c, r))
    elif shape == "cube":
        if dc and dr:
            raise ValueError("cube supports cardinal --dir only (N/E/S/W)")
        side = s
        off = -(side // 2)
        for f in range(side):
            for w in range(off, off + side):
                if dc:                        # E/W: forward on cols, width on rows
                    tiles.add((o[0] + dc * f, o[1] + w))
                else:                         # N/S: forward on rows, width on cols
                    tiles.add((o[0] + w, o[1] + dr * f))
    elif shape == "cone":
        ang0 = math.atan2(dr, dc)
        for c in range(cols):
            for r in range(rows):
                if (c, r) == o:
                    continue
                if max(abs(c - o[0]), abs(r - o[1])) > s:
                    continue
                ang = math.atan2(r - o[1], c - o[0])
                diff = abs((ang - ang0 + math.pi) % (2 * math.pi) - math.pi)
                if diff <= math.pi / 4 + 1e-9:
                    tiles.add((c, r))
    elif shape == "line":
        for i in range(1, s + 1):
            tiles.add((o[0] + dc * i, o[1] + dr * i))
    else:
        raise ValueError(f"Bad shape: {shape!r}")

    inb = [(c, r) for (c, r) in tiles if 0 <= c < cols and 0 <= r < rows]
    return [tile_name(c, r) for (c, r) in sorted(inb, key=lambda t: (t[1], t[0]))]


def main(argv=None):
    p = argparse.ArgumentParser(description="Combat-grid math.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dist")
    d.add_argument("a")
    d.add_argument("b")

    v = sub.add_parser("validate")
    v.add_argument("spec")

    m = sub.add_parser("move")
    m.add_argument("--from", dest="frm", required=True)
    m.add_argument("--to", required=True)
    m.add_argument("--speed", type=int, required=True)
    m.add_argument("--spec", required=True)

    r = sub.add_parser("range")
    r.add_argument("--from", dest="frm", required=True)
    r.add_argument("--to", required=True)
    r.add_argument("--ft", type=int, required=True)

    a = sub.add_parser("aoe")
    a.add_argument("--shape", required=True,
                   choices=["sphere", "cube", "cone", "line"])
    a.add_argument("--origin", required=True)
    a.add_argument("--size", type=int, required=True)
    a.add_argument("--dir", dest="direction", default="N", choices=sorted(DIRS))
    a.add_argument("--spec", required=True)

    args = p.parse_args(argv)
    if args.cmd == "dist":
        print(f"{dist_ft(args.a, args.b)}ft")
    if args.cmd == "validate":
        try:
            spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"INVALID\n- cannot read spec: {e}")
            sys.exit(1)
        errors = validate_spec(spec)
        if errors:
            print("INVALID")
            for e in errors:
                print(f"- {e}")
            sys.exit(1)
        print("VALID")
    if args.cmd == "move":
        print(move_verdict(load_spec(args.spec), args.frm, args.to, args.speed))
    if args.cmd == "range":
        print(range_verdict(args.frm, args.to, args.ft))
    if args.cmd == "aoe":
        tiles = aoe_tiles(load_spec(args.spec), args.shape, args.origin,
                          args.size, args.direction)
        print(f"{len(tiles)} tiles: {' '.join(tiles)}")


if __name__ == "__main__":
    main()
