import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from visual_factor_scoring import (ASSESSMENT_VERSION, FACTOR_SOURCES, FACTORS, enforce_script_audit_evidence, has_complete_response_scorecard,
                                   normalize_and_score, parse_factor_assessments)
from build_human_review_benchmark import assessment_fields, assign_stratified_split, summarize
from validate_chatgpt_visual_review import main as validate_chatgpt_review


class VisualFactorScoringTests(unittest.TestCase):
    def factors(self, severity="none"):
        return {key: {"severity": severity, "evidence": f"Evidence for {key}", "source": source}
                for key, source in FACTOR_SOURCES.items()}

    def test_fixed_factor_weights_and_verdict_bands(self):
        factors = self.factors()
        factors["hero_imagery"]["severity"] = "moderate"
        factors["readability"]["severity"] = "moderate"
        result = normalize_and_score(factors)
        self.assertEqual((result["score"], result["verdict"]), (50, "medium"))
        factors["layout_mobile"]["severity"] = "moderate"
        result = normalize_and_score(factors)
        self.assertEqual((result["score"], result["verdict"]), (75, "high"))

    def test_absent_hero_floor_adds_independent_factor_points_without_double_counting(self):
        factors = self.factors()
        factors["hero_imagery"]["severity"] = "minor"
        factors["readability"]["severity"] = "minor"
        result = normalize_and_score(factors, {"hero_media": {"status": "absent"}})
        self.assertEqual(result["factor_points"]["hero_imagery"], 45)
        self.assertEqual(result["score"], 50)
        self.assertEqual(result["verdict"], "medium")
        factors["hero_imagery"]["severity"] = "severe"
        result = normalize_and_score(factors, {"hero_media": {"status": "absent"}})
        self.assertEqual(result["factor_points"]["hero_imagery"], 50)

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

    def test_visual_factors_accept_page_flow_as_supplementary_evidence(self):
        parsed = parse_factor_assessments(
            "Hero imagery: assessed (moderate) — The mid image follows the page scroll. "
            "Source=screenshots and page-flow measurements.",
            {},
        )
        self.assertEqual(parsed["hero_imagery"]["source"], "screenshots")
        self.assertIn("page scroll", parsed["hero_imagery"]["evidence"])
        parsed = parse_factor_assessments(
            "Layout and mobile use: assessed (minor) — Layout remains readable. "
            "Source=screenshots (all four views); supporting page-flow evidence=script_audit.",
            {},
        )
        self.assertEqual(parsed["layout_mobile"]["source"], "screenshots")

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
        self._assert_validator_uses_factor_score(validate_chatgpt_review)

    def _assert_validator_uses_factor_score(self, validator):
        screenshots = [{"filename": name} for name in (
            "desktop_top.jpg", "desktop_mid.jpg", "mobile_top.jpg", "mobile_mid.jpg")]
        factors = self.factors()
        factors["hero_imagery"]["severity"] = "minor"
        review = {
            "candidate_id": "candidate-1", "website_url": "https://example.com",
            "visual_verdict": "pass", "visual_opportunity_score": 49,
            "confidence": "high", "factor_assessments": factors,
            "desktop_top_observations": ["Desktop top checked"],
            "desktop_mid_observations": ["Desktop mid checked"],
            "mobile_top_observations": ["Mobile top checked"],
            "mobile_mid_observations": ["Mobile mid checked"],
            "desktop_observations": ["Desktop checked"],
            "mobile_observations": ["Mobile checked"],
            "major_issues": [], "moderate_issue_clusters": [], "severe_issue_clusters": [],
            "positive_signals": [], "recommended_redesign_focus": [], "limitations": [],
        }
        context = {
            "candidate_id": "candidate-1", "website_url": "https://example.com",
            "screenshots": screenshots,
            "script_audit": {
                "hero_media": {"status": "present"},
                "broken_images": {"status": "clear"},
                "broken_links_and_interactions": {"status": "clear",
                    "interactions": {"status": "clear", "tested_count": 1, "findings": []}},
            },
        }
        uploaded = [{"filename": item["filename"], "status": "uploaded"} for item in screenshots]
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

    def test_score_only_response_is_incomplete_until_all_factor_lines_exist(self):
        score_only = "Visual opportunity score: 62/100\nVerdict: medium"
        self.assertFalse(has_complete_response_scorecard(score_only))
        complete = score_only + "\n" + "\n".join((
            "Hero imagery: minor — text-only hero; source=screenshots",
            "Image quality: none — photos are sharp; source=screenshots",
            "Design freshness: none — visual language is cohesive; source=screenshots",
            "Readability: none — heading is legible; source=screenshots",
            "Layout and mobile use: none — controls fit the viewport; source=screenshots",
            "Broken images: none — no failed images; source=script_audit",
            "Interactions: none — controls responded; source=script_audit",
        ))
        self.assertTrue(has_complete_response_scorecard(complete))

    def test_flattened_factor_assessments_are_parsed_from_one_paragraph(self):
        flattened = (
            "Limitations: no script-audit evidence. Factor assessments: "
            "Hero imagery: minor — text-only hero; source=screenshots "
            "Image quality: none — photos are sharp; source=screenshots "
            "Design freshness: moderate — dated typography and controls; source=screenshots "
            "Readability: moderate — small dense text; source=screenshots "
            "Layout and mobile use: moderate — tightly packed mobile layout; source=screenshots "
            "Broken images: not_assessable — audit not provided; source=script_audit "
            "Interactions: not_assessable — audit not provided; source=script_audit"
        )
        factors = parse_factor_assessments(flattened, {})
        self.assertEqual(set(factors), set(FACTORS))
        self.assertEqual(factors["readability"]["severity"], "moderate")
        self.assertTrue(has_complete_response_scorecard(
            "Visual opportunity score: 55/100\n" + flattened
        ))
        partial_json = {"visual_opportunity_score": 55, "factor_assessments": {
            "hero_imagery": {"severity": "none", "evidence": "JSON value", "source": "screenshots"}
        }}
        merged = parse_factor_assessments(flattened, partial_json)
        self.assertEqual(len(merged), len(FACTORS))
        self.assertEqual(merged["hero_imagery"]["evidence"], "JSON value")
        self.assertTrue(has_complete_response_scorecard(flattened, partial_json))

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

    def test_benchmark_excludes_legacy_scores_and_marks_factor_uncertainty(self):
        legacy = {"chatgpt_visual_review": {"status": "passed", "visual_opportunity_score": 30}}
        self.assertEqual(assessment_fields(legacy, "chatgpt")["chatgpt_status"], "not_assessed")
        self.assertEqual(assessment_fields(legacy, "chatgpt")["chatgpt_assessment_version"], "legacy_excluded")
        factor = {"chatgpt_visual_review": {
            "status": "passed", "visual_opportunity_score": None, "visual_verdict": "uncertain",
            "score_calibration": {"version": ASSESSMENT_VERSION}, "factor_assessments": {"hero_imagery": {"severity": "minor"}},
        }}
        fields = assessment_fields(factor, "chatgpt")
        self.assertEqual(fields["chatgpt_status"], "uncertain")
        self.assertEqual(fields["chatgpt_assessment_version"], ASSESSMENT_VERSION)
        self.assertEqual(fields["chatgpt_score"], None)


if __name__ == "__main__":
    unittest.main()
