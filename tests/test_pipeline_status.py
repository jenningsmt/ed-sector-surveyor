"""Pipeline return codes, retired settings, and status wording (v1.7.0)."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gui import config as config_module
from gui import pipeline
from gui.worker import describe_exit
from scripts import extract_sector_systems_to_sqlite as extractor


class PipelineReturnCodeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "project"
        db = self.project / "data" / "sector_library" / "sector_Audit.sqlite"
        db.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(db)) as conn:
            extractor.init_db(conn)
        self.dump = Path(self._tmp.name) / "dump.jsonl"
        self.dump.write_text("{}\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, **stages):
        base = {"extract": False, "bracketed_gaps": False, "backward_extrap": False,
                "bio_opportunity": False, "aggregate": False, "cluster": False}
        return {"project_dir": str(self.project), "galaxy_dump_path": str(self.dump),
                "sectors": ["Audit"], "dry_run": True, "stages": {**base, **stages}}

    def run_pipeline(self, config):
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            rc = pipeline.run_pipeline(config)
        return rc, log.getvalue()

    def test_nothing_to_do_but_no_errors_is_complete(self):
        rc, log = self.run_pipeline(self.config())
        self.assertEqual(rc, pipeline.EXIT_OK)
        self.assertIn("Pipeline complete", log)

    def test_failed_stage_returns_stages_failed_code(self):
        with mock.patch.object(pipeline.gap_full_export, "run", side_effect=RuntimeError("boom")):
            rc, log = self.run_pipeline(self.config(bracketed_gaps=True))
        self.assertEqual(rc, pipeline.EXIT_STAGES_FAILED)
        self.assertIn("bracketed gaps (Audit)", log)
        self.assertNotIn("Pipeline complete", log)

    def test_extraction_with_rejected_records_continues_and_reports(self):
        with mock.patch.object(pipeline.extract_multi_sector_to_sqlite, "run",
                               return_value=pipeline.EXIT_REJECTED_RECORDS), \
                mock.patch.object(pipeline.sector_summary_export, "analyze_sector", return_value=None):
            rc, log = self.run_pipeline(self.config(extract=True))
        self.assertEqual(rc, pipeline.EXIT_REJECTED_RECORDS)
        self.assertIn("Pipeline complete", log)

    def test_extraction_failure_stops_the_run(self):
        with mock.patch.object(pipeline.extract_multi_sector_to_sqlite, "run", return_value=1):
            rc, log = self.run_pipeline(self.config(extract=True, bracketed_gaps=True))
        self.assertEqual(rc, 1)
        self.assertIn("Extraction failed", log)

    def test_missing_sector_database_counts_as_failed(self):
        config = self.config(bracketed_gaps=True)
        config["sectors"] = ["Nowhere"]
        rc, log = self.run_pipeline(config)
        self.assertEqual(rc, pipeline.EXIT_STAGES_FAILED)
        self.assertIn("no extracted sector database", log)


class RetiredSettingsTests(unittest.TestCase):
    def test_forward_extrapolation_settings_are_dropped_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "run_forward": True, "max_forward_step": 3, "extend_depth": 7,
                "stages": {"forward_extrap": True, "backward_extrap": False},
            }), encoding="utf-8")
            with mock.patch.object(config_module, "config_path", return_value=path):
                loaded = config_module.load_config()
        self.assertNotIn("run_forward", loaded)
        self.assertNotIn("max_forward_step", loaded)
        self.assertNotIn("forward_extrap", loaded["stages"])
        self.assertEqual(loaded["extend_depth"], 7)
        self.assertFalse(loaded["stages"]["backward_extrap"])


class StatusWordingTests(unittest.TestCase):
    def test_known_codes_are_described(self):
        self.assertEqual(describe_exit("0"), "completed, exit 0")
        self.assertEqual(describe_exit("2"), "finished with failed stages, exit 2")
        self.assertIn("cancelled", describe_exit("130"))

    def test_unknown_code_is_shown_as_is(self):
        self.assertEqual(describe_exit("7"), "exit 7")


if __name__ == "__main__":
    unittest.main()
