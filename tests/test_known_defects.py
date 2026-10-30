"""Regression tests for the defects found by the October 2026 engineering audit.

Each test asserts the intended (fixed) behavior. Tests for defects that are not
fixed yet are marked @known_defect, so they count as expected failures and the
suite stays green; the Phase 1 fix for each one removes its marker. Set
SHOW_KNOWN_DEFECTS=1 to run them as ordinary tests and see the current failures.

Finding IDs (E01, E02, ...) refer to the Zurara Bridge engineering review.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gui import pipeline
from gui import worker as worker_module
from scripts import extract_multi_sector_to_sqlite as multi
from scripts import extract_sector_systems_to_sqlite as extractor
from scripts import gap_full_export

known_defect = (lambda f: f) if os.environ.get("SHOW_KNOWN_DEFECTS") else unittest.expectedFailure


def _system(name="Audit AA-A a0", address=1):
    return {"name": name, "id64": address, "coords": {"x": 0, "y": 0, "z": 0}, "bodies": []}


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _count_systems(db: Path) -> int:
    with contextlib.closing(sqlite3.connect(db)) as conn:
        return conn.execute("select count(*) from systems").fetchone()[0]


class WorkerStatusTests(unittest.TestCase):
    def test_exception_reports_failure_status(self):
        """E01: a job that raises must not finish with __job_done__:0."""
        w = worker_module.Worker()

        def broken(**_kwargs):
            raise RuntimeError("injected failure")

        w._run(broken, (), {})
        messages = []
        while not w.log_queue.empty():
            messages.append(w.log_queue.get_nowait())
        self.assertTrue(any("RuntimeError" in m for m in messages))
        self.assertNotEqual(messages[-1], f"{worker_module.DONE_SENTINEL}:0")

    def test_normal_completion_reports_its_return_code(self):
        w = worker_module.Worker()
        w._run(lambda **_kwargs: 0, (), {})
        messages = []
        while not w.log_queue.empty():
            messages.append(w.log_queue.get_nowait())
        self.assertEqual(messages[-1], f"{worker_module.DONE_SENTINEL}:0")


class ExtractionDefectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dump = self.tmp / "dump.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_final_flush_failure_returns_nonzero(self):
        """E01: a failed final flush in the multi-sector extractor must not return 0."""
        _write_jsonl(self.dump, [json.dumps(_system())])
        sinks = []
        build_sinks = multi.build_sinks

        def capture(*args, **kwargs):
            built = build_sinks(*args, **kwargs)
            sinks.extend(built)
            return built

        try:
            with mock.patch.object(multi, "build_sinks", side_effect=capture), \
                    mock.patch.object(multi.SectorSink, "flush", side_effect=sqlite3.OperationalError("injected disk failure")), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = multi.run(self.dump, ["Audit"], self.tmp / "out")
            self.assertNotEqual(rc, 0)
        finally:
            # The failed flush currently skips closing the connection (also part of E01);
            # close it here so Windows can delete the temporary folder.
            for sink in sinks:
                sink.conn.close()

    def test_corrupt_jsonl_line_is_reported(self):
        """E02: a malformed line must be counted and reported, not skipped silently."""
        _write_jsonl(self.dump, [json.dumps(_system()), "{invalid json}", json.dumps(_system("Audit AA-A a1", 2))])
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            rc = extractor.run(self.dump, "Audit", output_db=self.tmp / "corrupt.sqlite")
        self.assertEqual(_count_systems(self.tmp / "corrupt.sqlite"), 2)
        self.assertNotEqual(rc, 0)
        self.assertIn("rejected", log.getvalue().lower())

    def test_limit_one_extracts_one_system(self):
        """P2: limit=1 must extract one record, not zero."""
        _write_jsonl(self.dump, [json.dumps(_system())])
        with contextlib.redirect_stdout(io.StringIO()):
            rc = extractor.run(self.dump, "Audit", output_db=self.tmp / "limit.sqlite", limit=1)
        self.assertEqual(rc, 0)
        self.assertEqual(_count_systems(self.tmp / "limit.sqlite"), 1)


class PipelineStatusTests(unittest.TestCase):
    def test_failed_required_stage_is_not_complete_success(self):
        """E01: a required stage that raises must make the run return nonzero."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            db = project / "data" / "sector_library" / "sector_Audit.sqlite"
            db.parent.mkdir(parents=True)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                extractor.init_db(conn)
            dump = Path(tmp) / "dump.jsonl"
            _write_jsonl(dump, [json.dumps(_system())])
            config = {
                "project_dir": str(project), "galaxy_dump_path": str(dump), "sectors": ["Audit"], "dry_run": True,
                "stages": {"extract": False, "bracketed_gaps": True, "backward_extrap": False,
                           "bio_opportunity": False, "aggregate": False, "cluster": False},
            }
            log = io.StringIO()
            with mock.patch.object(pipeline.gap_full_export, "run", side_effect=RuntimeError("injected stage failure")), \
                    contextlib.redirect_stdout(log):
                rc = pipeline.run_pipeline(config)
        self.assertIn("injected stage failure", log.getvalue())
        self.assertNotEqual(rc, 0)


class EdsmFailureTests(unittest.TestCase):
    def test_transport_failure_is_not_cached_as_absence(self):
        """Already correct: an EDSM transport error leaves the candidate unknown and uncached."""
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "edsm-cache.sqlite"
            with mock.patch.object(gap_full_export._EDSMClient, "exists_system", side_effect=RuntimeError("injected transport error")), \
                    contextlib.redirect_stdout(io.StringIO()):
                kept = gap_full_export.validate_candidates(["Audit AA-A b1"], cache, False)
            with contextlib.closing(sqlite3.connect(cache)) as conn:
                cached = conn.execute("select count(*) from edsm_cache").fetchone()[0]
        self.assertEqual(kept, [])
        self.assertEqual(cached, 0)


if __name__ == "__main__":
    unittest.main()
