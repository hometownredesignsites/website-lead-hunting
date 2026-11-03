import json
import tempfile
import unittest
from pathlib import Path

from tools.build_visual_review_matrix import parse_candidate_ids, select_eligible_candidates


def candidate(candidate_id, *, priority="medium", last_status=None, scored=False, loaded=True):
    row = {
        "candidate_id": candidate_id,
        "site_check_status": "loaded" if loaded else "failed",
        "automated_priority": priority,
    }
    if last_status:
        row["chatgpt_visual_review_last_attempt"] = {"status": last_status}
    if scored:
        row["chatgpt_visual_review"] = {
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


if __name__ == "__main__":
    unittest.main()

