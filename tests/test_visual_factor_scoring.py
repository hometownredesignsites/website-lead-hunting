import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from visual_factor_scoring import enforce_script_audit_evidence, normalize_and_score, parse_factor_assessments
from build_human_review_benchmark import assign_stratified_split, summarize
from validate_chatgpt_visual_review import main as validate_chatgpt_review


class VisualFactorScoringTests(unittest.TestCase):
    def factors(self, severity="none"):
        sources = {
            "hero_imagery": "screenshots",
            "readability": "screenshots",
            "layout_mobile": "screenshots",
            "broken_images": "script_audit",
            "interactions": "script_audit",
        }
        return {key: {"severity": severity, "evidence": f"Evidence for {key}", "source": source}
                for key, source in sources.items()}

    def test_fixed_factor_weights_and_verdict_bands(self):
        factors = self.factors()
        factors["hero_imagery"]["severity"] = "moderate"
        factors["readability"]["severity"] = "moderate"
        result = normalize_and_score(factors)
        self.assertEqual((result["score"], result["verdict"]), (50, "medium"))
        factors["layout_mobile"]["severity"] = "moderate"
        result = normalize_and_score(factors)
        self.assertEqual((result["score"], result["verdict"]), (75, "high"))

    def test_missing_factor_or_evidence_is_invalid(self):
        factors = self.factors()
        factors.pop("interactions")
        self.assertTrue(normalize_and_score(factors)["errors"])
        factors = self.factors()
        factors["broken_images"]["evidence"] = "  "
        self.assertTrue(normalize_and_score(factors)["errors"])

    def test_not_assessable_factor_returns_uncertain_without_score(self):
        factors = self.factors()
        factors["broken_images"]["severity"] = "not_assessable"
        result = normalize_and_score(factors)
        self.assertEqual((result["score"], result["verdict"]), (None, "uncertain"))

    def test_factor_source_must_match_its_evidence_type(self):
        factors = self.factors()
        factors["interactions"]["source"] = "screenshots"
        self.assertTrue(normalize_and_score(factors)["errors"])

    def test_incomplete_script_checks_cannot_be_scored_as_clean(self):
        factors = self.factors()
        audit = {
            "broken_images": {"status": "partial"},
            "broken_links_and_interactions": {"status": "not_run"},
        }
        result = normalize_and_score(enforce_script_audit_evidence(factors, audit))
        self.assertEqual(result["verdict"], "uncertain")
        self.assertEqual(set(result["uncertain_factors"]), {"broken_images", "interactions"})

    def test_chatgpt_validator_returns_the_fixed_factor_score(self):
        self._assert_validator_uses_factor_score(validate_chatgpt_review, duckai=False)

    def _assert_validator_uses_factor_score(self, validator, duckai):
        screenshots = [
            {"filename": name}
            for name in ("desktop_top.jpg", "desktop_mid.jpg", "mobile_top.jpg", "mobile_mid.jpg")
        ]
        factors = self.factors()
        factors["hero_imagery"]["severity"] = "minor"
        review = {
            "candidate_id": "candidate-1", "website_url": "https://example.com",
            "visual_verdict": "pass", "visual_opportunity_score": 49,
            "confidence": "high", "factor_assessments": factors,
            "desktop_top_observations": ["Desktop top checked"],
            "desktop_mid_observations": ["Desktop middle checked"],
            "mobile_top_observations": ["Mobile top checked"],
            "mobile_mid_observations": ["Mobile middle checked"],
            "desktop_observations": ["Desktop checked"],
            "mobile_observations": ["Mobile checked"],
            "major_issues": [], "moderate_issue_clusters": [], "severe_issue_clusters": [],
            "positive_signals": [], "recommended_redesign_focus": [], "limitations": [],
        }
        context = {
            "candidate_id": "candidate-1", "website_url": "https://example.com",
            "screenshots": screenshots,
            "script_audit": {
                "broken_images": {"status": "clear"},
                "broken_links_and_interactions": {"status": "clear"},
            },
        }
        uploaded = [{"filename": item["filename"], "status": "uploaded"} for item in screenshots]
        if duckai:
            context["upload_images"] = [
                {"filename": "desktop_top.jpg", "covers": ["desktop_top.jpg", "desktop_mid.jpg"]},
                {"filename": "mobile_top.jpg", "covers": ["mobile_top.jpg", "mobile_mid.jpg"]},
                {"filename": "desktop_mid.jpg", "covers": []},
            ]
            # Duck.ai uploads three composites, so its attachment names and coverage match that contract.
            context["upload_images"] = context["upload_images"][:2] + [
                {"filename": "desktop_mid.jpg", "covers": ["mobile_mid.jpg"]}
            ]
            uploaded = [{"filename": item["filename"], "status": "uploaded"} for item in context["upload_images"]]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            response_path = root / "response.json"
            metadata_path = root / "metadata.json"
            context_path = root / "context.json"
            output_path = root / "result.json"
            response_path.write_text(json.dumps(review), encoding="utf-8")
            metadata_path.write_text(json.dumps({"image_attachments": uploaded}), encoding="utf-8")
            context_path.write_text(json.dumps(context), encoding="utf-8")
            args = ["validator", "--response", str(response_path), "--metadata", str(metadata_path),
                    "--context", str(context_path), "--output", str(output_path)]
            with patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()):
                result_code = validator()
            result = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(result_code, 0)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["review"]["visual_opportunity_score"], 5)
        self.assertEqual(result["review"]["visual_verdict"], "pass")

    def test_parse_fixed_labeled_factor_lines(self):
        text = "Hero imagery: moderate — no meaningful hero image; source=screenshots\n" \
               "Broken images: none — script audit reported zero failed images; source=script_audit"
        parsed = parse_factor_assessments(text, {})
        self.assertEqual(parsed["hero_imagery"]["severity"], "moderate")
        self.assertEqual(parsed["broken_images"]["source"], "script_audit")
        self.assertNotIn("source=", parsed["broken_images"]["evidence"])

    def test_benchmark_splits_by_label_and_reports_misclassification(self):
        rows = [
            {"website": f"site-{i}.example", "decision": "High", "chatgpt_status": "passed",
             "chatgpt_verdict": "Pass", "chatgpt_score": 30,
             "duckai_status": "passed", "duckai_verdict": "High", "duckai_score": 80}
            for i in range(10)
        ]
        assign_stratified_split(rows)
        self.assertEqual(sum(row["benchmark_split"] == "holdout" for row in rows), 2)
        report = summarize(rows)
        metrics = report["providers"]["chatgpt"]["label_metrics"]["High"]
        self.assertEqual(metrics["false_negative"], 10)
        self.assertEqual(report["provider_score_agreement"]["within_10_points"], 0)


if __name__ == "__main__":
    unittest.main()
