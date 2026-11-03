from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.private_results import copy_in, copy_run_out


class PrivateResultsTests(unittest.TestCase):
    def test_copy_in_replaces_public_data_with_private_source_of_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            workspace = root / "workspace"
            (private / ".git").mkdir(parents=True)
            (private / "config").mkdir()
            (private / "runs" / "pilot").mkdir(parents=True)
            (workspace / "config").mkdir(parents=True)
            (workspace / "runs" / "stale-public-run").mkdir(parents=True)
            (private / "config" / "cities.csv").write_text("city\nDenver\n", encoding="utf-8")
            (private / "runs" / "pilot" / "CANDIDATE_LEDGER.jsonl").write_text("{}\n", encoding="utf-8")
            (workspace / "runs" / "stale-public-run" / "old.json").write_text("{}", encoding="utf-8")

            copy_in(private, workspace)

            self.assertTrue((workspace / "config" / "cities.csv").is_file())
            self.assertTrue((workspace / "runs" / "pilot" / "CANDIDATE_LEDGER.jsonl").is_file())
            self.assertFalse((workspace / "runs" / "stale-public-run").exists())

    def test_copy_run_out_omits_screenshots_from_durable_private_git_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            workspace = root / "workspace"
            run = workspace / "runs" / "pilot"
            (run / "screenshots").mkdir(parents=True)
            (run / "CANDIDATE_LEDGER.jsonl").write_text("{}\n", encoding="utf-8")
            (run / "screenshots" / "desktop.png").write_bytes(b"image")

            copy_run_out(private, workspace, "pilot")

            self.assertTrue((private / "runs" / "pilot" / "CANDIDATE_LEDGER.jsonl").is_file())
            self.assertFalse((private / "runs" / "pilot" / "screenshots" / "desktop.png").exists())


if __name__ == "__main__":
    unittest.main()
