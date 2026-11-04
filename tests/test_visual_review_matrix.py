import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.build_visual_review_matrix import parse_candidate_ids, select_eligible_candidates


def candidate(candidate_id, *, priority="medium", last_status=None, scored=False, loaded=True,
              hero_media_status=None):
    row = {
        "candidate_id": candidate_id,
        "site_check_status": "loaded" if loaded else "failed",
        "automated_priority": priority,
    }
    if hero_media_status:
        row["hero_media"] = {"status": hero_media_status}
    if last_status:
        row["duckai_visual_review_last_attempt"] = {"status": last_status}
    if scored:
        row["duckai_visual_review"] = {
            "status": "passed",
            "candidate_id": candidate_id,
            "visual_opportunity_score": 52,
            "visual_verdict": "medium",
            "confidence": "high",
            "major_issues": ["Weak hierarchy"],
        }
    return row


class VisualReviewMatrixTests(unittest.TestCase):
    def test_blank_ids_enable_auto_selection_and_select_first_twenty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            rows = [candidate(f"candidate-{index:02d}") for index in range(25)]
            (run_dir / "CANDIDATE_LEDGER.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            self.assertEqual(parse_candidate_ids(""), [])
            selected, eligible_count = select_eligible_candidates(run_dir)

            self.assertEqual(len(selected), 20)
            self.assertEqual(selected[0], "candidate-00")
            self.assertEqual(selected[-1], "candidate-19")
            self.assertEqual(eligible_count, 25)

    def test_auto_selection_skips_scored_pass_and_existing_failure_states(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            rows = [
                candidate("eligible-medium"),
                candidate("eligible-high", priority="high"),
                candidate("already-scored", scored=True),
                candidate("technical-pass", priority="pass"),
                candidate("failed-to-load", loaded=False),
                candidate("retry-queued", last_status="retry-needed"),
                candidate("permanent-failure", last_status="failed"),
            ]
            (run_dir / "CANDIDATE_LEDGER.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            selected, eligible_count = select_eligible_candidates(run_dir)

            self.assertEqual(selected, ["eligible-medium", "eligible-high"])
            self.assertEqual(eligible_count, 2)

    def test_auto_selection_routes_passing_sites_with_absent_or_uncertain_hero_media(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            rows = [
                candidate("hero-absent", priority="pass", hero_media_status="absent"),
                candidate("hero-uncertain", priority="pass", hero_media_status="uncertain"),
                candidate("hero-present-pass", priority="pass", hero_media_status="present"),
            ]
            (run_dir / "CANDIDATE_LEDGER.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            selected, eligible_count = select_eligible_candidates(run_dir)

            self.assertEqual(selected, ["hero-absent", "hero-uncertain"])
            self.assertEqual(eligible_count, 2)

    def test_empty_auto_selection_is_successful_and_emits_empty_matrix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "CANDIDATE_LEDGER.jsonl").write_text(
                json.dumps(candidate("already-scored", scored=True)) + "\n", encoding="utf-8")
            output = root / "github-output.txt"

            result = subprocess.run(
                [sys.executable, "tools/build_visual_review_matrix.py",
                 "--candidate-ids", "", "--run-dir", str(run_dir),
                 "--github-output", str(output)],
                capture_output=True, text=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["status"], "no_eligible_candidates")
            self.assertEqual(plan["message"], "No eligible candidates; batch complete.")
            self.assertEqual(plan["matrix"], {"include": []})
            outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(json.loads(outputs["candidate_ids"]), [])
            self.assertEqual(outputs["selected_count"], "0")
            self.assertEqual(outputs["eligible_count"], "0")
            self.assertEqual(outputs["selection_mode"], "automatic")

    def test_shared_workflows_queue_pending_runs_without_canceling_active_run(self):
        workflows = [
            Path(".github/workflows/lead-hunt.yml"),
            Path(".github/workflows/chatgpt-visual-review-batch.yml"),
            Path(".github/workflows/chatgpt-visual-review-retry-scheduler.yml"),
        ]
        for workflow in workflows:
            with self.subTest(workflow=workflow):
                text = workflow.read_text(encoding="utf-8")
                self.assertTrue(
                    "group: website-lead-hunt-private-operations-${{ github.repository }}" in text
                    or "format('website-lead-hunt-private-operations-{0}', github.repository)" in text
                )
                self.assertIn("queue: max", text)
                self.assertIn("cancel-in-progress: false", text)


if __name__ == "__main__":
    unittest.main()

