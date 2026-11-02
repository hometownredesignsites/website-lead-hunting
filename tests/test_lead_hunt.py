import unittest
import json
import tempfile
from pathlib import Path
from urllib.parse import quote

from tools.lead_hunt import (
    build_queries,
    classify_result,
    challenge_text_reason,
    BehaviorLogger,
    is_excluded_host,
    parse_search_results,
    resolve_ddg_result,
    score_signals,
    observed_url,
)


class LeadHuntTests(unittest.TestCase):
    def test_builds_category_location_pairs_in_stable_order(self):
        queries = build_queries("Kitchen remodelers\nPool builders", "Denver, CO\nAurora, CO", "Denver metro")
        self.assertEqual([item["query"] for item in queries], [
            "Kitchen remodelers Denver, CO", "Kitchen remodelers Aurora, CO",
            "Pool builders Denver, CO", "Pool builders Aurora, CO",
        ])
        self.assertEqual([item["query_index"] for item in queries], [1, 2, 3, 4])

    def test_unwraps_organic_duckduckgo_link_and_discards_tracking(self):
        target = "https://www.example.com/services?utm_source=ddg&keep=yes#section"
        wrapped = "https://duckduckgo.com/l/?uddg=" + quote(target, safe="") + "&rut=tracking"
        parsed = resolve_ddg_result(wrapped)
        self.assertEqual(parsed["domain"], "example.com")
        self.assertEqual(parsed["url"], "https://www.example.com/services?keep=yes")
        self.assertFalse(parsed["is_ad"])

    def test_marks_nested_bing_ad_wrapper_without_visiting_it(self):
        ad = "https://duckduckgo.com/y.js?ad_provider=bingv7aa&ad_type=txad&ad_domain=angi.com"
        wrapped = "https://duckduckgo.com/l/?uddg=" + quote(ad, safe="")
        parsed = resolve_ddg_result(wrapped)
        self.assertTrue(parsed["is_ad"])
        self.assertEqual(parsed["ad_domain"], "angi.com")
        self.assertIsNone(parsed["url"])
        disposition, _ = classify_result(parsed)
        self.assertEqual(disposition, "sponsored_ad")

    def test_excludes_directory_subdomains(self):
        self.assertTrue(is_excluded_host("www.homeadvisor.com"))
        self.assertTrue(is_excluded_host("reviews.angi.com"))
        self.assertFalse(is_excluded_host("local.example.com"))

    def test_unwraps_and_classifies_known_directory_result(self):
        wrapped = "https://duckduckgo.com/l/?uddg=" + quote("https://www.homeguide.com/co/denver", safe="")
        rows = parse_search_results([{"href": wrapped, "title": "Denver remodeling contractors", "snippet": ""}])
        self.assertEqual(rows[0]["domain"], "homeguide.com")
        self.assertEqual(classify_result(rows[0])[0], "directory_or_lead_generation")

    def test_technical_score_remains_uncertain_with_limited_evidence(self):
        result = score_signals({"desktop": {"viewportMeta": True}, "mobile": {}})
        self.assertEqual(result["automated_priority"], "uncertain")
        self.assertEqual(result["evidence_confidence"], "limited")

    def test_technical_score_does_not_replace_visual_human_review(self):
        metrics = {
            "https": True,
            "desktop": {"viewportMeta": True, "hasContactPath": True, "hasCta": True,
                        "titlePresent": True, "descriptionPresent": True, "imageCount": 0,
                        "copyrightYears": [2026]},
            "mobile": {"horizontalOverflowPx": 0},
        }
        result = score_signals(metrics)
        self.assertEqual(result["automated_priority"], "pass")
        self.assertIn("score_basis", result)
        self.assertNotIn("human_review", result)

    def test_technical_score_uses_measured_mobile_horizontal_overflow(self):
        base = {
            "https": True,
            "desktop": {"viewportMeta": True, "hasContactPath": True, "hasCta": True,
                        "titlePresent": True, "descriptionPresent": True, "imageCount": 0},
        }
        clear = score_signals({**base, "mobile": {"horizontalOverflowPx": 0}})
        overflow = score_signals({**base, "mobile": {"horizontalOverflowPx": 48}})
        self.assertEqual(clear["signals"]["mobile_overflow"], "clear")
        self.assertEqual(overflow["signals"]["mobile_overflow"], "issue")

    def test_detects_duckduckgo_human_verification_challenge(self):
        body = "Unfortunately, bots use DuckDuckGo too. Please complete the following challenge to confirm this search was made by a human. Select all squares containing a duck."
        self.assertEqual(challenge_text_reason("https://html.duckduckgo.com/html/", "DuckDuckGo", body),
                         "DuckDuckGo human-verification challenge text")

    def test_behavior_url_redacts_query_fragment_and_credentials(self):
        observed = observed_url("https://user:secret@html.duckduckgo.com/html/?q=private+query&token=abc#frag")
        self.assertEqual(observed, {"scheme": "https", "host": "html.duckduckgo.com", "path": "/html/"})

    def test_behavior_logger_appends_ordered_timestamped_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "BEHAVIOR_LOG.jsonl"
            logger = BehaviorLogger(log_path)
            logger.emit("sample", query_index=1, status=202)
            logger.close()
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["event"] for row in rows], ["batch_process_started", "sample", "batch_process_finished"])
        self.assertEqual([row["sequence"] for row in rows], [1, 2, 3])
        self.assertTrue(all("elapsed_ms" in row and "timestamp" in row for row in rows))


if __name__ == "__main__":
    unittest.main()
