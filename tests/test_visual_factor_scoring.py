import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from visual_factor_scoring import enforce_script_audit_evidence, normalize_and_score, parse_factor_assessments
from build_human_review_benchmark import assign_stratified_split, summarize


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
