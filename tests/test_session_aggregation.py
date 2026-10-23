"""The GUI's master candidate list must cover only the files written in that
session -- not every candidate CSV that has accumulated in out/.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import csv
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from gui import pipeline
from scripts import gap_extrapolate_export, gap_full_export, gap_spatial_export

CSV_FIELDS = [
    "system_name", "edsm_status", "direction", "steps_from_edge", "spansh_edge_number",
    "family", "subsector", "mass_prefix", "mass_code", "boxel", "number",
]


def write_candidate_csv(path: Path, sector: str, names: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, CSV_FIELDS)
        writer.writeheader()
        for name in names:
            serial = int(name.rsplit("-", 1)[1])
            writer.writerow({
                "system_name": f"{sector} {name}", "edsm_status": "not_in_edsm",
                "direction": "bracketed_gap", "family": f"{sector} {name.split()[0]}",
                "subsector": name.split()[0], "mass_prefix": "c3", "mass_code": "c",
                "boxel": 3, "number": serial,
            })


def make_sector_db(path: Path, sector: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE systems (system_key TEXT, system_address INTEGER, "
                 "name TEXT, x REAL, y REAL, z REAL, raw_json TEXT)")
    for i, serial in enumerate((0, 1, 4, 7)):  # gaps at 2, 3, 5, 6
        conn.execute("INSERT INTO systems VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (str(i), i, f"{sector} AB-C c3-{serial}", float(i), 0.0, 0.0, "{}"))
    conn.commit()
    conn.close()


class SessionAggregationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def aggregate(self, session_files, started):
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline._run_aggregation(self.out_dir, session_files, started, scope="Sectors: Alpha")

    def test_master_contains_only_this_sessions_files(self):
        # Left over from earlier sessions: another sector entirely, and a
        # forward-extrapolation file for the same sector from a run that had
        # forward extrapolation enabled.
        write_candidate_csv(self.out_dir / "beta_gap_full_validated.csv", "Beta", ["AB-C c3-9"])
        write_candidate_csv(self.out_dir / "alpha_extrap_forward_step1_validated.csv", "Alpha", ["AB-C c3-8"])
        # This session's output.
        session_csv = self.out_dir / "alpha_gap_full_validated.csv"
        write_candidate_csv(session_csv, "Alpha", ["AB-C c3-2", "AB-C c3-3"])
        session_md = self.out_dir / "alpha_gap_full_validated.md"
        session_md.write_text("# report\n", encoding="utf-8")

        started = datetime(2026, 10, 21, 19, 49, 13, tzinfo=timezone.utc)
        self.aggregate([session_csv, session_md], started)

        master_csv = self.out_dir / "master_gap_candidates_20261021-194913.csv"
        master_md = self.out_dir / "master_gap_candidates_20261021-194913.md"
        self.assertTrue(master_csv.is_file())
        with master_csv.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["system_name"] for r in rows], ["Alpha AB-C c3-2", "Alpha AB-C c3-3"])
        self.assertEqual({r["source_file"] for r in rows}, {"alpha_gap_full_validated.csv"})

        md = master_md.read_text(encoding="utf-8")
        self.assertIn("Session started (UTC): 2026-10-21T19:49:13+00:00", md)
        self.assertIn("Sectors: Alpha", md)
        self.assertNotIn("beta", md)
        self.assertFalse((self.out_dir / "master_gap_candidates.csv").exists())

    def test_no_session_candidates_writes_no_master(self):
        write_candidate_csv(self.out_dir / "beta_gap_full_validated.csv", "Beta", ["AB-C c3-9"])
        self.aggregate([], datetime.now(timezone.utc))
        self.assertEqual(list(self.out_dir.glob("master_gap_candidates*")), [])


class StageRunReturnsWrittenFilesTest(unittest.TestCase):
    """The pipeline relies on each stage's run() returning what it wrote."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.out_dir = tmp / "out"
        self.db = tmp / "sector_Alpha.sqlite"
        make_sector_db(self.db, "Alpha")

    def tearDown(self):
        self._tmp.cleanup()

    def assert_written(self, written, expected_names):
        self.assertEqual(sorted(p.name for p in written), sorted(expected_names))
        for path in written:
            self.assertTrue(path.is_file(), path)

    def test_gap_full_export(self):
        with contextlib.redirect_stdout(io.StringIO()):
            written = gap_full_export.run(
                db_path=self.db, sector="Alpha", out_dir=self.out_dir,
                dry_run=True, cache_db_path=None,
            )
        self.assert_written(written, ["alpha_gap_full_dry_run.csv", "alpha_gap_full_dry_run.md"])

    def test_gap_extrapolate_export(self):
        with contextlib.redirect_stdout(io.StringIO()):
            written = gap_extrapolate_export.run(
                db_path=self.db, sector="Alpha", out_dir=self.out_dir, extend_depth=2,
                direction="both", max_forward_step=3, dry_run=True, cache_db_path=None,
            )
        self.assert_written(written, [
            "alpha_extrap_backward_dry_run.csv", "alpha_extrap_backward_dry_run.md",
            "alpha_extrap_forward_step1_dry_run.csv", "alpha_extrap_forward_step1_dry_run.md",
        ])

    def test_gap_spatial_export(self):
        with contextlib.redirect_stdout(io.StringIO()):
            written = gap_spatial_export.run(
                db_path=self.db, sector="Alpha", center_system="Alpha AB-C c3-0",
                radius_ly=10, out_dir=self.out_dir, dry_run=True, cache_db_path=None,
            )
        stem = "Alpha_spatial_Alpha_AB-C_c3-0"
        self.assert_written(written, [
            f"{stem}_spatial_gap_dry_run.csv", f"{stem}_spatial_gap_dry_run.md",
            f"{stem}_spatial_backward_dry_run.csv", f"{stem}_spatial_backward_dry_run.md",
        ])


if __name__ == "__main__":
    unittest.main()
