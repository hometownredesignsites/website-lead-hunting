from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from tools.lead_hunt_campaign import (
    apply_child_completion,
    create_state,
    dispatch_payload,
    ensure_inputs_unchanged,
    event_campaign,
    fetch_workflow_run,
    save_state,
)


def event_for(batch: int = 1, attempt: int = 1, run_id: int = 11,
              conclusion: str = "success") -> dict:
    return {"workflow_run": {
        "name": "ChatGPT website lead hunt",
        "event": "workflow_dispatch",
        "display_title": f"ChatGPT website lead hunt campaign-x campaign=campaign-x batch={batch} attempt={attempt}",
        "id": run_id,
        "conclusion": conclusion,
    }}


class LeadHuntCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.categories = self.root / "config/business_categories.csv"
        self.cities = self.root / "config/cities.csv"
        self.categories.write_text("category\nKitchen remodelers\nRoofers\n", encoding="utf-8")
        self.cities.write_text('city\n"Westminster, CO"\n"Denver, CO"\n', encoding="utf-8")
        self.state = create_state(self.root, "campaign-x", "Colorado", 2, 3, False,
                                  self.categories, self.cities)
        self.path = self.root / "runs/campaign-x/CAMPAIGN_STATE.json"
        save_state(self.path, self.state)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_start_persists_csv_fingerprint_and_query_total(self) -> None:
        self.assertEqual(self.state["total_queries"], 4)
        self.assertEqual(self.state["status"], "active")
        self.assertEqual(self.state["pending_batch_number"], 1)
        ensure_inputs_unchanged(self.state, self.categories, self.cities)

    def test_resume_refuses_changed_csvs(self) -> None:
        self.cities.write_text('city\n"Boulder, CO"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "CSV inputs changed"):
            ensure_inputs_unchanged(self.state, self.categories, self.cities)

    def test_child_event_is_identified_only_for_campaign_runs(self) -> None:
        event = event_for()
        title = event["workflow_run"].pop("display_title")
        self.assertEqual(event_campaign(event, {"display_title": title}), ("campaign-x", 1, 1))
        ordinary = event_for()
        ordinary["workflow_run"]["display_title"] = "ChatGPT website lead hunt campaign-x"
        self.assertIsNone(event_campaign(ordinary))

    def test_workflow_run_api_record_supplies_campaign_display_title(self) -> None:
        from io import BytesIO
        response = BytesIO(json.dumps({"name": "ChatGPT website lead hunt",
            "event": "workflow_dispatch", "display_title":
            "ChatGPT website lead hunt campaign-x campaign=campaign-x batch=1 attempt=1"}).encode())
        response.status = 200
        with patch("tools.lead_hunt_campaign.urllib.request.urlopen") as open_url:
            open_url.return_value.__enter__.return_value = response
            details = fetch_workflow_run("owner/repo", "11", "test-token")
        self.assertEqual(event_campaign({"workflow_run": {"id": 11}}, details),
                         ("campaign-x", 1, 1))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://api.github.com/repos/owner/repo/actions/runs/11")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

    def test_success_advances_to_next_checkpointed_batch(self) -> None:
        progress = {"status": "IN_PROGRESS", "pending": 2, "errors": 0}
        self.assertTrue(apply_child_completion(self.state, event_for(), progress))
        self.assertEqual(self.state["pending_batch_number"], 2)
        self.assertEqual(self.state["pending_attempt"], 1)
        self.assertEqual(self.state["completed_batches"], 1)
        self.assertFalse(apply_child_completion(self.state, event_for(), progress))

    def test_complete_progress_stops_campaign(self) -> None:
        progress = {"status": "COMPLETE", "pending": 0, "errors": 0}
        self.assertFalse(apply_child_completion(self.state, event_for(), progress))
        self.assertEqual(self.state["status"], "complete")

    def test_transient_batch_failure_retries_same_batch_then_pauses(self) -> None:
        failed_progress = {"status": "IN_PROGRESS", "pending": 4, "errors": 1}
        self.assertTrue(apply_child_completion(self.state, event_for(conclusion="failure"), failed_progress))
        self.assertEqual(self.state["pending_batch_number"], 1)
        self.assertEqual(self.state["pending_attempt"], 2)
        self.assertTrue(apply_child_completion(self.state, event_for(attempt=2, run_id=12,
                                                                      conclusion="failure"), failed_progress))
        self.assertEqual(self.state["pending_attempt"], 3)
        self.assertFalse(apply_child_completion(self.state, event_for(attempt=3, run_id=13,
                                                                       conclusion="failure"), failed_progress))
        self.assertEqual(self.state["status"], "paused")

    def test_challenge_pauses_without_repeating_the_blocked_batch(self) -> None:
        blocked = {"status": "BLOCKED", "pending": 4, "errors": 0}
        self.assertFalse(apply_child_completion(self.state, event_for(conclusion="failure"), blocked))
        self.assertEqual(self.state["status"], "paused")

    def test_cancelled_batch_pauses_and_can_be_resumed_manually(self) -> None:
        progress = {"status": "IN_PROGRESS", "pending": 4, "errors": 0}
        self.assertFalse(apply_child_completion(self.state, event_for(conclusion="cancelled"), progress))
        self.assertEqual(self.state["status"], "paused")

    def test_dispatch_uses_checkpoint_and_bounded_inputs(self) -> None:
        payload = dispatch_payload(self.state, "git-chatgpt")
        self.assertEqual(payload["ref"], "git-chatgpt")
        self.assertEqual(payload["inputs"]["run_key"], "campaign-x")
        self.assertEqual(payload["inputs"]["max_queries"], "2")
        self.assertEqual(payload["inputs"]["campaign_batch"], "1")
        self.assertEqual(payload["inputs"]["campaign_attempt"], "1")
        self.assertEqual(payload["inputs"]["capture_google_evidence"], "false")

    def test_campaign_workflow_dispatches_and_handles_completion(self) -> None:
        workflow = (Path(__file__).parents[1] / ".github/workflows/lead-hunt-campaign.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("workflow_run:", workflow)
        self.assertIn("workflows:\n      - ChatGPT website lead hunt", workflow)
        self.assertIn("--dispatch-to-github", workflow)


if __name__ == "__main__":
    unittest.main()
