"""Survey clustering: the sparse complete-linkage must match a brute-force
reference, the boxel-grid decoding must recover a sector's origin, and run()
must place every candidate exactly once.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import csv
import io
import itertools
import math
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import gap_cluster_export as gce


def brute_force_complete_linkage(points, cut):
    """Textbook O(n³) complete linkage: merge the closest pair of groups
    (max member-pair distance) until none is within cut."""
    groups = [[i] for i in range(len(points))]

    def dist(a, b):
        return max(math.dist(points[i], points[j]) for i in a for j in b)

    while True:
        best = None
        for x, y in itertools.combinations(range(len(groups)), 2):
            d = dist(groups[x], groups[y])
            if d <= cut and (best is None or d < best[0]):
                best = (d, x, y)
        if best is None:
            return sorted(sorted(g) for g in groups)
        _, x, y = best
        groups[x] += groups[y]
        del groups[y]


def random_points(rng, n, span=400.0):
    return [(rng.uniform(0, span), rng.uniform(0, span / 4), rng.uniform(0, span)) for _ in range(n)]


class CompleteLinkageTest(unittest.TestCase):
    def test_matches_brute_force(self):
        rng = random.Random(7)
        for _ in range(40):
            points = random_points(rng, rng.randint(1, 35))
            cut = rng.uniform(40, 200)
            got = sorted(gce.complete_linkage(len(points), gce._neighbor_pairs(points, cut)))
            self.assertEqual(got, brute_force_complete_linkage(points, cut))

    def test_nested_levels_match_brute_force_and_nest(self):
        rng = random.Random(11)
        for _ in range(25):
            points = random_points(rng, rng.randint(2, 35))
            clusters, regions = gce.nested_complete_linkage(points, 60.0, 180.0)
            self.assertEqual(sorted(clusters), brute_force_complete_linkage(points, 60.0))
            region_points = sorted(sorted(i for c in r for i in clusters[c]) for r in regions)
            self.assertEqual(region_points, brute_force_complete_linkage(points, 180.0))
            self.assertEqual(sorted(c for r in regions for c in r), list(range(len(clusters))))

    def test_visiting_order_is_a_permutation_from_start(self):
        rng = random.Random(3)
        points = random_points(rng, 30)
        order = gce.visiting_order(points, 5)
        self.assertEqual(order[0], 5)
        self.assertEqual(sorted(order), list(range(30)))


class BoxelGridTest(unittest.TestCase):
    ORIGIN = (-9025.0, -25.0, -6185.0)  # Hypiae Aim

    def position_in(self, subsector, mass_prefix, frac=(0.3, 0.6, 0.9)):
        grid = gce.boxel_grid_index(subsector, mass_prefix)
        size = gce.CUBE_SIZE[mass_prefix[0]]
        return tuple(self.ORIGIN[a] + (grid[a] + frac[a]) * size for a in range(3))

    def test_real_systems_lie_in_their_decoded_cube(self):
        # Coordinates as stored in sector_Hypiae_Aim.sqlite.
        real = {
            ("JS-T", "d3"): (-8930.0625, 299.5625, -5858.875),
            ("XO-Z", "d13"): (-7784.78125, 21.8125, -4957.28125),
            ("TC-T", "c17"): (-7932.96875, 95.4375, -5391.9375),
        }
        for (subsector, mass_prefix), pos in real.items():
            grid = gce.boxel_grid_index(subsector, mass_prefix)
            size = gce.CUBE_SIZE[mass_prefix[0]]
            for a in range(3):
                low = self.ORIGIN[a] + grid[a] * size
                self.assertTrue(low <= pos[a] < low + size, (subsector, mass_prefix, a))
        self.assertIsNone(gce.boxel_grid_index("AS", "c17"))

    def test_origin_recovered_from_consistent_systems(self):
        samples = [(s, m, self.position_in(s, m)) for s, m in
                   [("AS-S", "c17"), ("BB-M", "c21"), ("KC-V", "e2"), ("PX-W", "b56"), ("XO-Z", "d13")]]
        self.assertEqual(gce.infer_sector_origin(samples), self.ORIGIN)

    def test_inconsistent_systems_give_no_origin(self):
        pos = self.position_in("AS-S", "c17")
        shifted = (pos[0] + 500, pos[1], pos[2])
        samples = [("AS-S", "c17", pos), ("BB-M", "c21", self.position_in("BB-M", "c21")),
                   ("XO-Z", "d13", shifted)]
        self.assertIsNone(gce.infer_sector_origin(samples))


class RunTest(unittest.TestCase):
    SECTOR = "Testa Aim"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.out_dir = tmp / "out"
        self.db = tmp / "sector.sqlite"
        # Two boxel neighbourhoods ~1000 ly apart, plus a candidate boxel
        # with no known systems at all.
        known = {
            "AB-C c3": [(0, 0, 0), (10, 5, 3)],
            "AD-C c3": [(30, 0, 20)],
            "ZZ-Y d9": [(1000, 40, 1000), (1010, 40, 990)],
        }
        conn = sqlite3.connect(str(self.db))
        conn.execute("CREATE TABLE systems (system_key TEXT, system_address INTEGER, "
                     "name TEXT, x REAL, y REAL, z REAL, raw_json TEXT)")
        n = 0
        for boxel, positions in known.items():
            for serial, (x, y, z) in enumerate(positions):
                conn.execute("INSERT INTO systems VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (str(n), n, f"{self.SECTOR} {boxel}-{serial}", x, y, z, "{}"))
                n += 1
        conn.commit()
        conn.close()

        self.out_dir.mkdir()
        self.candidates = self.out_dir / "testa_aim_gap_full_validated.csv"
        fields = ["system_name", "edsm_status", "direction", "steps_from_edge", "spansh_edge_number",
                  "family", "subsector", "mass_prefix", "mass_code", "boxel", "number"]
        rows = [("AB-C", "c3", 5, "not_in_edsm"), ("AB-C", "c3", 6, "not_in_edsm"),
                ("AD-C", "c3", 2, "not_in_edsm"), ("ZZ-Y", "d9", 4, "not_in_edsm"),
                ("QQ-Q", "b1", 0, "not_in_edsm"), ("AB-C", "c3", 9, "in_edsm")]
        with self.candidates.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fields)
            writer.writeheader()
            for sub, mp, num, status in rows:
                writer.writerow({
                    "system_name": f"{self.SECTOR} {sub} {mp}-{num}", "edsm_status": status,
                    "direction": "bracketed_gap", "family": f"{self.SECTOR} {sub}", "subsector": sub,
                    "mass_prefix": mp, "mass_code": mp[0], "boxel": mp[1:], "number": num,
                })

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_candidate_placed_once_and_grouped_by_proximity(self):
        with contextlib.redirect_stdout(io.StringIO()):
            written = gce.run(self.db, self.SECTOR, [self.candidates], self.out_dir)
        self.assertEqual(sorted(p.name for p in written), [
            "testa_aim_candidates_clustered.csv", "testa_aim_survey_clusters.csv",
            "testa_aim_survey_clusters.md", "testa_aim_survey_clusters.svg",
        ])
        with (self.out_dir / "testa_aim_candidates_clustered.csv").open(newline="", encoding="utf-8") as f:
            rows = {r["system_name"]: r for r in csv.DictReader(f)}

        # in_edsm row dropped; the rest each appear once.
        self.assertEqual(len(rows), 5)
        name = f"{self.SECTOR} {{}}".format
        near = {rows[name(n)]["cluster"] for n in ("AB-C c3-5", "AB-C c3-6", "AD-C c3-2")}
        self.assertEqual(len(near), 1)
        self.assertNotEqual(rows[name("ZZ-Y d9-4")]["region"], rows[name("AB-C c3-5")]["region"])
        # Starting from Sol, the region at the origin comes first.
        self.assertEqual(rows[name("AB-C c3-5")]["cluster"], "R01-C01")
        # No known boxel-mates and no confirmed grid: listed but unplaced.
        self.assertEqual(rows[name("QQ-Q b1-0")]["cluster"], "")
        self.assertEqual(rows[name("AB-C c3-5")]["anchor_known_system"][:len(self.SECTOR)], self.SECTOR)

    def test_no_candidates_raises(self):
        empty = self.out_dir / "empty_gap_full_validated.csv"
        empty.write_text("system_name,edsm_status\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(gce.NoCandidatesError):
                gce.run(self.db, self.SECTOR, [empty], self.out_dir)


if __name__ == "__main__":
    unittest.main()
