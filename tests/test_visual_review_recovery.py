import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tools.merge_chatgpt_visual_reviews import merge
from tools.prepare_chatgpt_visual_review import main as prepare_visual_review, validate_source_viewport
from tools.visual_review_status import classify_attempt


class VisualReviewRecoveryTests(unittest.TestCase):
    def test_batch_workflow_uses_proven_in_session_retry_behavior(self):
        workflow = Path(".github/workflows/chatgpt-visual-review-batch.yml").read_text(encoding="utf-8")
        review_jobs = workflow.split("\n  notify_city_campaign:", 1)[0]
        self.assertIn("xvfb-run --auto-servernum", workflow)
        self.assertIn("tmp/chatgpt-visual-review-profile-$REVIEW_CANDIDATE_ID-$replicate\"", workflow)
        self.assertIn("--allow-retry", workflow)
        self.assertIn("--refresh-before-retry", workflow)
        self.assertNotIn("for attempt in 1 2 3", review_jobs)
        self.assertNotIn("chatgpt-visual-review-profile-$REVIEW_CANDIDATE_ID-$attempt", review_jobs)

    def test_missing_source_artifact_is_a_permanent_candidate_failure(self):
        decision = classify_attempt(
            source_artifact_available=False,
            preparation_ok=False,
            process_exit_code=None,
            result=None,
        )
        self.assertEqual(decision["status"], "failed")
        self.assertFalse(decision["retryable"])

    def test_missing_source_screenshot_is_not_retried_as_a_chatgpt_failure(self):
        decision = classify_attempt(
            source_artifact_available=True,
            preparation_ok=False,
            process_exit_code=None,
            result=None,
            preparation_error="Saved mobile screenshot is unavailable in the downloaded artifact.",
        )
        self.assertEqual(decision["status"], "failed")
        self.assertEqual(decision["failure_class"], "screenshots_unavailable_or_invalid")

    def test_screenshot_viewport_mismatch_is_rejected_before_chatgpt(self):
        with self.assertRaisesRegex(ValueError, "Desktop screenshot appears mismatched"):
            validate_source_viewport("desktop", 390, 844, Path("mobile.png"))
        with self.assertRaisesRegex(ValueError, "Mobile screenshot appears mismatched"):
            validate_source_viewport("mobile", 1440, 1000, Path("desktop.png"))

    def test_targeted_screenshot_preparation_uses_candidate_page_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-1"
            (run_dir / "screenshots").mkdir(parents=True)
            for viewport, size in (("desktop", (1000, 3200)), ("mobile", (390, 3200))):
                Image.new("RGB", size, "white").save(run_dir / "screenshots" / f"{viewport}.png")
            candidate = {
                "candidate_id": "candidate-1",
                "screenshots": {
                    viewport: {"status": "saved", "path": f"screenshots/{viewport}.png"}
                    for viewport in ("desktop", "mobile")
                },
                "visual_page_flow": {
                    "desktop": {"first_content_image": {"document_y": 1200},
                                "first_contact_form": {"document_y": 1500}},
                    "mobile": {"first_content_image": {"document_y": 1300},
                               "first_contact_form": {"document_y": 1600}},
                },
            }
            (run_dir / "CANDIDATE_LEDGER.jsonl").write_text(
                json.dumps(candidate) + "\n", encoding="utf-8"
            )
            output_dir = root / "prepared"
            with patch.object(sys, "argv", ["prepare", "--run-dir", str(run_dir),
                                             "--candidate-id", "candidate-1",
                                             "--output-dir", str(output_dir)]):
                self.assertEqual(prepare_visual_review(), 0)
            manifest = json.loads((output_dir / "input-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual({item["filename"] for item in manifest["images"]}, {
                "desktop_top.jpg", "desktop_mid.jpg", "mobile_top.jpg", "mobile_mid.jpg"
            })
            desktop_mid = next(item for item in manifest["images"]
                               if item["viewport"] == "desktop" and item["view"] == "mid")
            mobile_mid = next(item for item in manifest["images"]
                              if item["viewport"] == "mobile" and item["view"] == "mid")
        self.assertEqual(desktop_mid["source_top_px"], 575)
        self.assertEqual(mobile_mid["source_top_px"], 1131)

    def test_mismatched_uploaded_screenshots_are_retried_in_a_fresh_session(self):
        decision = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=1,
            result={"status": "failed", "errors": [
                "all expected desktop/mobile top and mid screenshots were not confirmed uploaded"
            ]},
        )
        self.assertEqual(decision["status"], "retry-needed")
        self.assertTrue(decision["retryable"])
        self.assertEqual(decision["failure_class"], "screenshot_upload_unconfirmed")

    def test_blank_response_is_retryable(self):
        decision = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=1,
            result={"status": "failed", "errors": [
                "response has no parseable JSON object or labeled assessment scorecard"
            ]},
        )
        self.assertEqual(decision["status"], "retry-needed")
        self.assertEqual(decision["failure_class"], "blank_or_unparseable_response")

    def test_invalid_scorecard_is_retryable_but_identity_mismatch_is_not(self):
        invalid = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=1,
            result={"status": "failed", "errors": [
                "visual_opportunity_score must be an integer from 0 to 100"
            ]},
        )
        identity = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=1,
            result={"status": "failed", "errors": [
                "candidate_id does not match the source candidate"
            ]},
        )
        self.assertEqual(invalid["status"], "retry-needed")
        self.assertEqual(identity["status"], "failed")

    def test_runner_retries_known_transient_errors_but_not_setup_or_challenge_errors(self):
        transient = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=1,
            result=None,
            runner_log_text="Error: ChatGPT did not confirm all uploaded images in its composer",
        )
        setup_error = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=1,
            result=None,
            runner_log_text="ModuleNotFoundError: No module named 'playwright'",
        )
        challenge = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=1,
            result=None,
            runner_log_text="Human verification challenge detected",
        )
        timeout = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=124,
            result=None,
        )
        self.assertEqual(transient["status"], "retry-needed")
        self.assertEqual(setup_error["status"], "failed")
        self.assertEqual(challenge["status"], "failed")
        self.assertEqual(timeout["status"], "retry-needed")

    def test_interrupted_candidate_run_is_recorded_as_retry_needed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "CANDIDATE_LEDGER.jsonl"
            ledger.write_text(json.dumps({"candidate_id": "candidate-1"}) + "\n", encoding="utf-8")
            result = merge(
                ledger_path=ledger,
                results_dir=root / "artifacts-that-were-not-uploaded",
                candidate_ids=["candidate-1"],
                source_run_id="100",
                review_run_id="200",
            )
            row = json.loads(ledger.read_text(encoding="utf-8").strip())
        self.assertEqual(result["statuses"], {"retry-needed": 1})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["retry_needed"], 1)
        self.assertEqual(result["candidate_ids_by_status"]["retry-needed"], ["candidate-1"])
        self.assertEqual(row["chatgpt_visual_review_last_attempt"]["status"], "retry-needed")
        self.assertEqual(row["chatgpt_visual_review_last_attempt"]["failure_class"],
                         "review_artifact_missing_or_run_interrupted")


if __name__ == "__main__":
    unittest.main()
