import json
import tempfile
import unittest
from pathlib import Path

from tools.merge_duckai_visual_reviews import merge
from tools.prepare_duckai_visual_review import make_review_sheet, validate_source_viewport
from tools.visual_review_status import classify_attempt
from tools.validate_duckai_visual_review import parse_labeled_text, validate_image_uploads
from PIL import Image


class VisualReviewRecoveryTests(unittest.TestCase):
    def test_labeled_contact_sheet_covers_top_and_mid_source_views(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            views = []
            for view, color in (("top", "red"), ("mid", "blue")):
                path = root / f"desktop_{view}.jpg"
                Image.new("RGB", (960, 500), color).save(path)
                views.append({"view": view, "filename": path.name, "path": str(path)})
            sheet = make_review_sheet("desktop", views, root)
            self.assertEqual(sheet["filename"], "desktop_review_sheet.jpg")
            self.assertEqual(sheet["covers"], ["desktop_top.jpg", "desktop_mid.jpg"])
            with Image.open(sheet["path"]) as image:
                self.assertEqual(image.width, 960)
                self.assertGreater(image.height, 1000)

    def test_contact_sheet_upload_validation_checks_two_sheets_and_four_views(self):
        context = {
            "screenshots": [{"filename": f"{viewport}_{view}.jpg"}
                            for viewport in ("desktop", "mobile")
                            for view in ("top", "mid")],
            "upload_images": [
                {"filename": "desktop_review_sheet.jpg",
                 "covers": ["desktop_top.jpg", "desktop_mid.jpg"]},
                {"filename": "mobile_review_sheet.jpg",
                 "covers": ["mobile_top.jpg", "mobile_mid.jpg"]},
            ],
        }
        metadata = {"image_attachments": [
            {"filename": "desktop_review_sheet.jpg", "status": "uploaded"},
            {"filename": "mobile_review_sheet.jpg", "status": "uploaded"},
        ]}
        self.assertEqual(validate_image_uploads(metadata, context)["errors"], [])
        context["upload_images"][1]["covers"] = ["desktop_top.jpg", "desktop_mid.jpg"]
        self.assertIn("review sheets do not cover all four source top/mid screenshots exactly",
                      validate_image_uploads(metadata, context)["errors"])

    def test_scorecard_parser_splits_adjacent_labels_and_ignores_chat_footer(self):
        context = {"candidate_id": "candidate-1", "website_url": "https://example.com"}
        response = """Visual opportunity score: 78/100
Verdict: high
Confidence: medium
Desktop top observations:
- Hero image is dark. Desktop mid observations:
- Text is dense. Mobile top observations:
- The title is small. Mobile mid observations:
- Large dark gap appears. Desktop observations:
- Sections are clear. Mobile observations:
- Spacing is uneven. Major issues:
- Mobile hero readability. Positive signals:
- Strong project photos. Recommended redesign focus:
- Improve hero legibility. Limitations:
- Only screenshot views assessed.
Jump to latest response
Tools
"""
        review = parse_labeled_text(response, context)
        self.assertEqual(review["visual_opportunity_score"], 78)
        self.assertEqual(review["visual_verdict"], "high")
        self.assertEqual(review["desktop_top_observations"], ["Hero image is dark."])
        self.assertEqual(review["desktop_mid_observations"], ["Text is dense."])
        self.assertEqual(review["mobile_top_observations"], ["The title is small."])
        self.assertEqual(review["mobile_mid_observations"], ["Large dark gap appears."])
        self.assertEqual(review["major_issues"], ["Mobile hero readability."])

    def test_batch_workflow_uses_proven_in_session_retry_behavior(self):
        workflow = Path(".github/workflows/chatgpt-visual-review-batch.yml").read_text(encoding="utf-8")
        review_jobs = workflow.split("\n  notify_city_campaign:", 1)[0]
        self.assertIn("xvfb-run --auto-servernum", workflow)
        self.assertIn("tmp/duckai-visual-review-profile-$REVIEW_CANDIDATE_ID\"", workflow)
        self.assertIn("--allow-retry", workflow)
        self.assertIn("--refresh-before-retry", workflow)
        self.assertNotIn("for attempt in 1 2 3", review_jobs)
        self.assertNotIn("duckai-visual-review-profile-$REVIEW_CANDIDATE_ID-$attempt", review_jobs)

    def test_missing_source_artifact_is_a_permanent_candidate_failure(self):
        decision = classify_attempt(
            source_artifact_available=False,
            preparation_ok=False,
            process_exit_code=None,
            result=None,
        )
        self.assertEqual(decision["status"], "failed")
        self.assertFalse(decision["retryable"])

    def test_missing_source_screenshot_is_not_retried_as_a_duckai_failure(self):
        decision = classify_attempt(
            source_artifact_available=True,
            preparation_ok=False,
            process_exit_code=None,
            result=None,
            preparation_error="Saved mobile screenshot is unavailable in the downloaded artifact.",
        )
        self.assertEqual(decision["status"], "failed")
        self.assertEqual(decision["failure_class"], "screenshots_unavailable_or_invalid")

    def test_screenshot_viewport_mismatch_is_rejected_before_duckai(self):
        with self.assertRaisesRegex(ValueError, "Desktop screenshot appears mismatched"):
            validate_source_viewport("desktop", 390, 844, Path("mobile.png"))
        with self.assertRaisesRegex(ValueError, "Mobile screenshot appears mismatched"):
            validate_source_viewport("mobile", 1440, 1000, Path("desktop.png"))

    def test_mismatched_uploaded_screenshots_are_retried_in_a_fresh_session(self):
        decision = classify_attempt(
            source_artifact_available=True,
            preparation_ok=True,
            process_exit_code=1,
            result={"status": "failed", "errors": [
                "both expected desktop/mobile review sheets were not confirmed uploaded"
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
            runner_log_text="Error: Duck.ai did not confirm all uploaded images in its composer",
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
        self.assertEqual(row["duckai_visual_review_last_attempt"]["status"], "retry-needed")
        self.assertEqual(row["duckai_visual_review_last_attempt"]["failure_class"],
                         "review_artifact_missing_or_run_interrupted")


if __name__ == "__main__":
    unittest.main()
