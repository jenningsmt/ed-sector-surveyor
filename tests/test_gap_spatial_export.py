"""Spatial search must check candidates against the whole sector, not just the
systems inside the sphere (v1.5.1 fix), and must keep boxels that straddle
the sphere's edge.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import gap_spatial_export

SECTOR = "Alpha"
RADIUS = 10.0
# (boxel, serial) -> x position; y = z = 0, center at the origin.
SYSTEMS = {
    # Known sequence running out of the sphere: serial 2 is known but sits
    # 100 ly out -- before the fix it came back as a "candidate".
    ("AB-C c3", 0): 0.0, ("AB-C c3", 1): 5.0, ("AB-C c3", 4): 6.0,
    ("AB-C c3", 2): 100.0, ("AB-C c3", 6): 200.0,
    # Straddles the edge: one member inside, centroid far outside.
    ("DE-F c3", 1): 8.0, ("DE-F c3", 3): 300.0, ("DE-F c3", 4): 310.0,
    # Centroid at the center, but no member inside the sphere.
    ("JK-L c3", 1): -30.0, ("JK-L c3", 3): 30.0,
    # Entirely out of range.
    ("GH-I c3", 1): 500.0, ("GH-I c3", 3): 510.0,
}


class SpatialCandidatesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.db = tmp / "sector_Alpha.sqlite"
        self.out_dir = tmp / "out"
        conn = sqlite3.connect(str(self.db))
        conn.execute("CREATE TABLE systems (system_key TEXT, system_address INTEGER, "
                     "name TEXT, x REAL, y REAL, z REAL, raw_json TEXT)")
        for n, ((boxel, serial), x) in enumerate(SYSTEMS.items()):
            conn.execute("INSERT INTO systems VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (str(n), n, f"{SECTOR} {boxel}-{serial}", x, 0.0, 0.0, "{}"))
        conn.commit()
        conn.close()
        self.known = {f"{SECTOR} {boxel}-{serial}" for boxel, serial in SYSTEMS}

    def tearDown(self):
        self._tmp.cleanup()

    def test_candidates_checked_against_whole_sector(self):
        found = gap_spatial_export.find_spatial_candidates(self.db, (0.0, 0.0, 0.0), RADIUS)
        bracketed = set(found.bracketed)
        backward = {c.system_name for c in found.backward}

        self.assertEqual(bracketed, {
            "Alpha AB-C c3-3", "Alpha AB-C c3-5",  # not -2: that one is known, outside
            "Alpha DE-F c3-2",                     # straddling boxel kept
            "Alpha JK-L c3-2",                     # centroid in range
        })
        self.assertEqual(backward, {"Alpha DE-F c3-0", "Alpha JK-L c3-0"})
        self.assertFalse((bracketed | backward) & self.known)
        self.assertEqual(found.boxels_in_range, 3)
        self.assertAlmostEqual(found.distance_ly["Alpha DE-F c3-2"], 206.0)

    def test_run_writes_distance_column_and_no_known_systems(self):
        with contextlib.redirect_stdout(io.StringIO()):
            written = gap_spatial_export.run(
                db_path=self.db, sector=SECTOR, center_system="Alpha AB-C c3-0",
                radius_ly=RADIUS, out_dir=self.out_dir, dry_run=True, cache_db_path=None,
            )
        rows = []
        for path in written:
            if path.suffix == ".csv":
                with path.open(newline="", encoding="utf-8") as f:
                    rows += list(csv.DictReader(f))
        self.assertEqual(len(rows), 6)
        self.assertFalse({r["system_name"] for r in rows} & self.known)
        distances = {r["system_name"]: r["boxel_distance_ly"] for r in rows}
        self.assertEqual(distances["Alpha JK-L c3-2"], "0.0")


if __name__ == "__main__":
    unittest.main()
