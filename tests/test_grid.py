"""test_grid.py — grid.py combat-grid math: tile labels, distance, spec
validation, movement pathing, range, AoE. Pure-function tests, no filesystem
except tmp spec files. Spec:
docs/superpowers/specs/2026-07-16-combat-grid-map-cue-design.md
"""
import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "dnd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import grid  # noqa: E402


class TileTests(unittest.TestCase):
    def test_parse_tile(self):
        self.assertEqual(grid.parse_tile("A1"), (0, 0))
        self.assertEqual(grid.parse_tile("C4"), (2, 3))
        self.assertEqual(grid.parse_tile("z99"), (25, 98))

    def test_tile_name_roundtrip(self):
        self.assertEqual(grid.tile_name(2, 3), "C4")
        self.assertEqual(grid.tile_name(*grid.parse_tile("R24")), "R24")

    def test_bad_labels_raise(self):
        for bad in ("", "4C", "AA1", "C0", "C", "C100"):
            with self.assertRaises(ValueError, msg=bad):
                grid.parse_tile(bad)


class DistTests(unittest.TestCase):
    def test_orthogonal(self):
        self.assertEqual(grid.dist_ft("A1", "A4"), 15)

    def test_diagonal_costs_5(self):
        self.assertEqual(grid.dist_ft("A1", "D4"), 15)   # pure diagonal, 3 tiles

    def test_mixed_is_chebyshev(self):
        self.assertEqual(grid.dist_ft("C4", "F5"), 15)   # dx=3, dy=1

    def test_same_tile_is_zero(self):
        self.assertEqual(grid.dist_ft("B2", "B2"), 0)


SPEC = {
    "handle": "cavern",
    "cols": 8, "rows": 6,
    "terrain": [
        {"tiles": "C3-D5", "kind": "rubble", "difficult": True},
        {"tiles": "F1", "kind": "pillar", "impassable": True, "blocks_los": True},
    ],
    "notes": "test map",
}


class ExpandTilesTests(unittest.TestCase):
    def test_single_tile(self):
        self.assertEqual(grid.expand_tiles("F1"), [(5, 0)])

    def test_rectangle_inclusive(self):
        tiles = grid.expand_tiles("C3-D5")
        self.assertEqual(len(tiles), 6)          # 2 cols x 3 rows
        self.assertIn((2, 2), tiles)             # C3
        self.assertIn((3, 4), tiles)             # D5

    def test_corners_any_order(self):
        self.assertEqual(set(grid.expand_tiles("D5-C3")),
                         set(grid.expand_tiles("C3-D5")))


class ValidateTests(unittest.TestCase):
    def test_valid_spec_no_errors(self):
        self.assertEqual(grid.validate_spec(SPEC), [])

    def test_missing_dims(self):
        errs = grid.validate_spec({"handle": "x", "terrain": []})
        self.assertTrue(any("cols" in e for e in errs))
        self.assertTrue(any("rows" in e for e in errs))

    def test_cols_over_26_rejected(self):
        errs = grid.validate_spec({"handle": "x", "cols": 30, "rows": 5})
        self.assertTrue(any("cols" in e for e in errs))

    def test_terrain_out_of_bounds_rejected(self):
        bad = dict(SPEC, terrain=[{"tiles": "J9", "difficult": True}])  # 8x6 grid
        self.assertTrue(grid.validate_spec(bad))

    def test_bad_tile_expr_rejected(self):
        bad = dict(SPEC, terrain=[{"tiles": "C3-D5-E6"}])
        self.assertTrue(grid.validate_spec(bad))

    def test_missing_handle_rejected(self):
        errs = grid.validate_spec({"cols": 8, "rows": 6})
        self.assertTrue(any("handle" in e for e in errs))

    def test_non_string_tiles_reported_not_crash(self):
        bad = dict(SPEC, terrain=[{"tiles": 123}])
        errs = grid.validate_spec(bad)
        self.assertTrue(any("terrain[0]" in e for e in errs))

    def test_non_dict_spec_reported_not_crash(self):
        errs = grid.validate_spec([1, 2])
        self.assertTrue(any("JSON object" in e for e in errs))

    def test_bool_dims_rejected(self):
        errs = grid.validate_spec({"handle": "x", "cols": True, "rows": 6})
        self.assertTrue(any("cols" in e for e in errs))

    def test_missing_tiles_key_descriptive(self):
        bad = dict(SPEC, terrain=[{"kind": "rubble"}])
        errs = grid.validate_spec(bad)
        self.assertTrue(any("missing 'tiles'" in e for e in errs))


class TerrainSetsTests(unittest.TestCase):
    def test_split_by_flag(self):
        difficult, impassable = grid.terrain_sets(SPEC)
        self.assertIn((2, 2), difficult)
        self.assertIn((5, 0), impassable)
        self.assertNotIn((5, 0), difficult)


class MoveTests(unittest.TestCase):
    # SPEC is 8x6; difficult C3-D5; impassable pillar F1.
    # SWAMP forces crossings: difficult band spans the full grid height, so no
    # cheap detour exists (SPEC's C3-D5 band leaves rows 1-2 open — Dijkstra
    # correctly routes around it, which is its own test below).
    SWAMP = {"handle": "swamp", "cols": 5, "rows": 3,
             "terrain": [{"tiles": "B1-C3", "kind": "bog", "difficult": True}]}

    def test_clear_path_ok(self):
        self.assertEqual(grid.move_verdict(SPEC, "A1", "A4", 30), "OK cost=15ft")

    def test_diagonal_costs_5(self):
        # E1 -> H4: pure diagonal, clear of the difficult band and the F1 pillar.
        self.assertEqual(grid.move_verdict(SPEC, "E1", "H4", 30), "OK cost=15ft")

    def test_difficult_terrain_doubles(self):
        # A2 -> D2 must cross the full-height bog at B and C: 10 + 10 + 5.
        self.assertEqual(grid.move_verdict(self.SWAMP, "A2", "D2", 30),
                         "OK cost=25ft")

    def test_path_routes_around_difficult_when_cheaper(self):
        # B2 -> E2 stays on row 2 (all normal): 15ft, never touches C3-D5.
        self.assertEqual(grid.move_verdict(SPEC, "B2", "E2", 30), "OK cost=15ft")

    def test_illegal_reports_cost_and_furthest(self):
        verdict = grid.move_verdict(self.SWAMP, "A2", "D2", 15)   # needs 25ft
        self.assertTrue(verdict.startswith("ILLEGAL cost=25ft"))
        self.assertIn("furthest reachable", verdict)

    def test_impassable_blocks_target(self):
        self.assertEqual(grid.move_verdict(SPEC, "E1", "F1", 30), "UNREACHABLE F1")

    def test_walled_off_target_unreachable(self):
        walled = {"handle": "w", "cols": 3, "rows": 1,
                  "terrain": [{"tiles": "B1", "impassable": True}]}
        self.assertEqual(grid.move_verdict(walled, "A1", "C1", 30), "UNREACHABLE C1")

    def test_off_grid_raises(self):
        with self.assertRaises(ValueError):
            grid.move_verdict(SPEC, "A1", "Z9", 30)


class RangeTests(unittest.TestCase):
    def test_in_range(self):
        self.assertEqual(grid.range_verdict("C4", "F5", 60), "IN RANGE (dist=15ft)")

    def test_out_of_range(self):
        self.assertEqual(grid.range_verdict("A1", "H6", 30),
                         "OUT OF RANGE (dist=35ft)")


class AoeTests(unittest.TestCase):
    BIG = {"handle": "big", "cols": 12, "rows": 12, "terrain": []}

    def test_sphere_20ft_is_chebyshev_radius_4(self):
        tiles = grid.aoe_tiles(self.BIG, "sphere", "F6", 20)
        self.assertIn("F6", tiles)            # origin included
        self.assertIn("B2", tiles)            # 4 tiles diagonal
        self.assertNotIn("A1", tiles)         # 5 tiles away
        self.assertEqual(len(tiles), 81)      # 9x9 square

    def test_sphere_clips_to_grid(self):
        tiles = grid.aoe_tiles(self.BIG, "sphere", "A1", 20)
        self.assertEqual(len(tiles), 25)      # 5x5 corner clip

    def test_cube_cardinal(self):
        tiles = grid.aoe_tiles(self.BIG, "cube", "F6", 15, direction="E")
        self.assertEqual(len(tiles), 9)       # 3x3
        self.assertIn("F6", tiles)            # near face at origin
        self.assertIn("H7", tiles)            # extends east, centered sideways

    def test_cube_diagonal_dir_raises(self):
        with self.assertRaises(ValueError):
            grid.aoe_tiles(self.BIG, "cube", "F6", 15, direction="NE")

    def test_line_30ft_east(self):
        tiles = grid.aoe_tiles(self.BIG, "line", "C6", 30, direction="E")
        self.assertEqual(tiles, ["D6", "E6", "F6", "G6", "H6", "I6"])

    def test_cone_15ft_east_widens(self):
        tiles = grid.aoe_tiles(self.BIG, "cone", "C6", 15, direction="E")
        self.assertNotIn("C6", tiles)         # origin excluded
        self.assertIn("D6", tiles)
        self.assertIn("F4", tiles)            # 45-degree edge at distance 3
        self.assertIn("F8", tiles)
        self.assertNotIn("D3", tiles)         # outside the 45-degree wedge

    def test_unknown_shape_raises(self):
        with self.assertRaises(ValueError):
            grid.aoe_tiles(self.BIG, "donut", "F6", 20)


if __name__ == "__main__":
    unittest.main()
