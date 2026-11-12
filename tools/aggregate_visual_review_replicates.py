#!/usr/bin/env python3
"""Aggregate independent visual assessments of one validated capture set."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from visual_replicate_aggregation import choose_aggregate_score, factor_points_for


REPLICATE_COUNT = 3


def aggregate(attempt_root: Path, output: Path) -> dict:
    attempts = []
    for index in range(1, REPLICATE_COUNT + 1):
        folder = attempt_root / str(index)
        status_path = folder / "attempt-status.json"
        result_path = folder / "review-result.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            status = {"status": "retry-needed", "retryable": True,
                      "failure_class": "replicate_result_missing",
                      "error": f"Replicate {index} did not produce an attempt status."}
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = None
        review = result.get("review") if isinstance(result, dict) else None
        valid = (status.get("status") == "passed"
                 and isinstance(result, dict) and result.get("status") == "passed"
                 and isinstance(review, dict)
                 and review.get("scorecard_validation_status") == "valid"
                 and isinstance(review.get("visual_opportunity_score"), int)
                 and review.get("visual_verdict") in {"pass", "medium", "high"})
        attempts.append({"replicate": index, "status": status, "result": result,
                         "review": review, "valid": valid})

    valid_reviews = [item["review"] for item in attempts if item["valid"]]
    scores = [review["visual_opportunity_score"] for review in valid_reviews]
    complete = len(valid_reviews) == REPLICATE_COUNT
    retryable = any(item["status"].get("retryable", False) for item in attempts if not item["valid"])
    if complete:
        aggregation = choose_aggregate_score(valid_reviews)
        exact_mean = aggregation["mean"]
        score = aggregation["score"]
        verdict = "pass" if score < 50 else "medium" if score < 76 else "high"
        selected_index = aggregation["selected_replicate"]
        review = dict(valid_reviews[selected_index - 1] if selected_index else valid_reviews[0])
        replicate_calibrations = [item.get("score_calibration") or {} for item in valid_reviews]
        factor_keys = sorted({key for calibration in replicate_calibrations
                              for key in (calibration.get("factor_points") or {})})
        factor_point_means = {}
        for key in factor_keys:
            points = [(calibration.get("factor_points") or {}).get(key, {}).get("points", 0)
                      for calibration in replicate_calibrations]
            factor_point_means[key] = statistics.mean(points)
        applied_factor_points = (
            factor_points_for(review) if aggregation["override_applied"] else factor_point_means
        )
        applied_stale_copyright_points = (
            (replicate_calibrations[selected_index - 1].get("stale_copyright_points", 0)
             if selected_index else statistics.mean(
                 calibration.get("stale_copyright_points", 0)
                 for calibration in replicate_calibrations
             ))
        )
        method = (
            f"Score range was {aggregation['range']} (trigger >= {aggregation['threshold_points']}); "
            f"{'used the highest valid, evidence-supported replicate ' + str(aggregation['selected_replicate']) if aggregation['override_applied'] else 'used the arithmetic mean because the maximum-score evidence did not qualify' if aggregation['disagreement_review_required'] else 'used the arithmetic mean'}; rounded half up for means; applied Pass/Medium/High bands afterward."
        )
        review.update({
            "visual_opportunity_score": score,
            "visual_verdict": verdict,
            "site_outcome": verdict,
            "replicate_count": REPLICATE_COUNT,
            "replicate_scores": scores,
            "replicate_score_mean": exact_mean,
            "replicate_score_min": min(scores),
            "replicate_score_max": max(scores),
            "replicate_score_range": aggregation["range"],
            "replicate_aggregation": aggregation["method"],
            "disagreement_triggered": aggregation["disagreement_triggered"],
            "disagreement_override_applied": aggregation["override_applied"],
            "disagreement_review_required": aggregation["disagreement_review_required"],
            "replicate_reviews": valid_reviews,
            "score_calibration": {
                "version": (replicate_calibrations[0].get("version") if replicate_calibrations else None),
                "method": method,
                "factor_points_mean": factor_point_means,
                "factor_points_applied": applied_factor_points,
                "stale_copyright_points_applied": applied_stale_copyright_points,
                "stale_copyright_points_mean": statistics.mean(
                    calibration.get("stale_copyright_points", 0) for calibration in replicate_calibrations
                ),
                "replicate_scores": scores,
                "replicate_score_mean": exact_mean,
                "disagreement": aggregation,
            },
        })
        result = {
            "status": "passed", "review_execution_status": "completed",
            "scorecard_validation_status": "valid", "site_outcome": verdict,
            "candidate_id": review.get("candidate_id"), "review": review,
            "replicate_count_required": REPLICATE_COUNT,
            "replicates_completed": REPLICATE_COUNT,
            "replicate_statuses": [item["status"] for item in attempts],
            "errors": [],
        }
    else:
        errors = [f"Only {len(valid_reviews)} of {REPLICATE_COUNT} independent visual scorecards were valid; no aggregate site rating was saved."]
        result = {
            "status": "failed", "review_execution_status": "completed",
            "scorecard_validation_status": "incomplete", "site_outcome": "uncertain",
            "replicate_count_required": REPLICATE_COUNT,
            "replicates_completed": len(attempts),
            "replicate_statuses": [item["status"] for item in attempts],
            "retryable": retryable, "errors": errors,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = aggregate(args.attempt_root, args.output)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
