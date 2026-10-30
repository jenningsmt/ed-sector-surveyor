"""Extraction behavior added in the Zurara hardening pass: input validation,
rejected-record reporting, staged publication, ring columns, and consistent
limit/commit semantics between the single- and multi-sector extractors.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import extract_multi_sector_to_sqlite as multi
from scripts import extract_sector_systems_to_sqlite as extractor


def _system(name="Audit AA-A a0", address=1, coords=None, bodies=None):
    return {
        "name": name, "id64": address,
        "coords": coords if coords is not None else {"x": 0, "y": 0, "z": 0},
        "bodies": bodies or [],
    }


def _ringed_system():
    ring = {"name": "Audit AA-A a0 A Ring", "type": "Icy", "mass": 1.5e10,
            "innerRadius": 70000.0, "outerRadius": 90000.0}
    body = {"name": "Audit AA-A a0 A", "bodyId": 1, "type": "Planet", "rings": [ring]}
    return _system(bodies=[body])


def _quiet(fn, *args, **kwargs):
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        rc = fn(*args, **kwargs)
    return rc, log.getvalue()


def _rows(db: Path, sql: str):
    with contextlib.closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql).fetchall()


class ExtractionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dump = self.tmp / "dump.jsonl"
        self.db = self.tmp / "sector_Audit.sqlite"

    def tearDown(self):
        self._tmp.cleanup()

    def write_dump(self, *lines: str) -> None:
        self.dump.write_text("\n".join(lines) + "\n", encoding="utf-8")


class InputValidationTests(ExtractionTestCase):
    def test_clean_input_returns_ok(self):
        self.write_dump(json.dumps(_system()))
        rc, log = _quiet(extractor.run, self.dump, "Audit", output_db=self.db)
        self.assertEqual(rc, extractor.EXIT_OK)
        self.assertNotIn("Rejected records", log)

    def test_strict_mode_stops_at_first_unreadable_record(self):
        self.write_dump(json.dumps(_system()), "{invalid json}", json.dumps(_system("Audit AA-A a1", 2)))
        rc, log = _quiet(extractor.run, self.dump, "Audit", output_db=self.db, strict=True)
        self.assertEqual(rc, extractor.EXIT_FAILED)
        self.assertIn("line 2", log)
        self.assertFalse(self.db.exists())

    def test_non_object_line_is_rejected(self):
        self.write_dump(json.dumps(_system()), "[1, 2, 3]")
        rc, log = _quiet(extractor.run, self.dump, "Audit", output_db=self.db)
        self.assertEqual(rc, extractor.EXIT_REJECTED_RECORDS)
        self.assertIn("not a JSON object", log)

    def test_truncated_json_array_fails_without_publishing(self):
        dump = self.tmp / "dump.json.gz"
        text = "[" + json.dumps(_system()) + ",\n" + json.dumps(_system("Audit AA-A a1", 2))[:25]
        with gzip.open(dump, "wt", encoding="utf-8") as handle:
            handle.write(text)
        rc, log = _quiet(extractor.run, dump, "Audit", output_db=self.db)
        self.assertEqual(rc, extractor.EXIT_FAILED)
        self.assertIn("Extraction failed", log)
        self.assertFalse(self.db.exists())
        self.assertFalse(extractor.staging_path(self.db).exists())

    def test_invalid_coordinates_are_stored_empty_and_counted(self):
        self.write_dump(
            json.dumps(_system("Audit AA-A a0", 1, {"x": 0, "y": None, "z": 0})),
            json.dumps(_system("Audit AA-A a1", 2, {"x": "nan", "y": 1, "z": 2})),
            json.dumps(_system("Audit AA-A a2", 3, {"x": -3.5, "y": 0, "z": 0})),
        )
        rc, log = _quiet(extractor.run, self.dump, "Audit", output_db=self.db)
        self.assertEqual(rc, extractor.EXIT_OK)
        rows = dict((n, (x, y, z)) for n, x, y, z in _rows(self.db, "select name, x, y, z from systems"))
        self.assertEqual(rows["Audit AA-A a0"], (None, None, None))
        self.assertEqual(rows["Audit AA-A a1"], (None, None, None))
        self.assertEqual(rows["Audit AA-A a2"], (-3.5, 0.0, 0.0))
        self.assertIn("Systems without valid coordinates: 2", log)

    def test_spansh_ring_fields_fill_the_ring_columns(self):
        self.write_dump(json.dumps(_ringed_system()))
        _quiet(extractor.run, self.dump, "Audit", output_db=self.db)
        (ring,) = _rows(self.db, "select ring_class, mass_mt, inner_rad, outer_rad from rings")
        self.assertEqual(ring, ("Icy", 1.5e10, 70000.0, 90000.0))


class PublicationTests(ExtractionTestCase):
    def test_success_publishes_and_leaves_no_partial_file(self):
        self.write_dump(json.dumps(_system()))
        _quiet(extractor.run, self.dump, "Audit", output_db=self.db)
        self.assertTrue(self.db.exists())
        self.assertFalse(extractor.staging_path(self.db).exists())

    def test_cancelled_fresh_extraction_writes_nothing(self):
        self.write_dump(json.dumps(_system()))
        cancel = threading.Event()
        cancel.set()
        rc, _ = _quiet(extractor.run, self.dump, "Audit", output_db=self.db, cancel_event=cancel)
        self.assertEqual(rc, extractor.EXIT_CANCELLED)
        self.assertFalse(self.db.exists())
        self.assertFalse(extractor.staging_path(self.db).exists())

    def test_leftover_partial_file_is_replaced(self):
        extractor.staging_path(self.db).write_bytes(b"not a database")
        self.write_dump(json.dumps(_system()))
        rc, _ = _quiet(extractor.run, self.dump, "Audit", output_db=self.db)
        self.assertEqual(rc, extractor.EXIT_OK)
        self.assertEqual(_rows(self.db, "select count(*) from systems"), [(1,)])

    def test_refresh_into_existing_database_still_merges(self):
        self.write_dump(json.dumps(_system()), json.dumps(_system("Audit AA-A a1", 2)))
        _quiet(extractor.run, self.dump, "Audit", output_db=self.db)
        self.write_dump(json.dumps(_system("Audit AA-A a2", 3)))
        rc, _ = _quiet(extractor.run, self.dump, "Audit", output_db=self.db)
        self.assertEqual(rc, extractor.EXIT_OK)
        self.assertEqual(_rows(self.db, "select count(*) from systems"), [(3,)])


class MultiSectorTests(ExtractionTestCase):
    def test_limit_means_records_scanned_in_both_extractors(self):
        self.write_dump(*(json.dumps(_system(f"Audit AA-A a{i}", i + 1)) for i in range(5)))
        _quiet(extractor.run, self.dump, "Audit", output_db=self.db, limit=2)
        _quiet(multi.run, self.dump, ["Audit"], self.tmp / "multi", limit=2)
        self.assertEqual(_rows(self.db, "select count(*) from systems"), [(2,)])
        self.assertEqual(_rows(self.tmp / "multi" / "sector_Audit.sqlite", "select count(*) from systems"), [(2,)])

    def test_body_threshold_triggers_a_flush(self):
        self.write_dump(*(json.dumps(_ringed_system() | {"name": f"Audit AA-A a{i}", "id64": i + 1}) for i in range(3)))
        with mock.patch.object(multi.SectorSink, "flush", autospec=True, side_effect=multi.SectorSink.flush) as flush:
            _quiet(multi.run, self.dump, ["Audit"], self.tmp / "multi",
                   commit_every_systems=1000, commit_every_bodies=1, commit_every_rings=1000)
        # One flush per system from the body threshold, plus the final flush on close.
        self.assertGreaterEqual(flush.call_count, 3)

    def test_one_failed_sector_does_not_block_the_others(self):
        self.write_dump(json.dumps(_system("Audit AA-A a0", 1)), json.dumps(_system("Other AA-A a0", 2)))
        real_flush = multi.SectorSink.flush

        def flush(sink):
            if sink.prefix == "Audit":
                raise sqlite3.OperationalError("injected disk failure")
            real_flush(sink)

        with mock.patch.object(multi.SectorSink, "flush", autospec=True, side_effect=flush):
            rc, log = _quiet(multi.run, self.dump, ["Audit", "Other"], self.tmp / "multi")
        self.assertEqual(rc, multi.EXIT_FAILED)
        self.assertIn("could not finish writing 'Audit'", log)
        self.assertFalse((self.tmp / "multi" / "sector_Audit.sqlite").exists())
        self.assertTrue((self.tmp / "multi" / "sector_Other.sqlite").exists())

    def test_rejected_records_reported_by_multi_sector_run(self):
        self.write_dump(json.dumps(_system()), "{invalid json}")
        rc, log = _quiet(multi.run, self.dump, ["Audit"], self.tmp / "multi")
        self.assertEqual(rc, multi.EXIT_REJECTED_RECORDS)
        self.assertIn("Rejected records: 1", log)


if __name__ == "__main__":
    unittest.main()
