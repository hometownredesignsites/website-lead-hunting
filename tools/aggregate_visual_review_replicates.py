#!/usr/bin/env python3
"""Aggregate independent visual assessments of one validated capture set."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


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
                 and isinstance(review.get("visual_opportunity_score"), int)
                 and review.get("visual_verdict") in {"pass", "medium", "high"})
        attempts.append({"replicate": index, "status": status, "result": result,
                         "review": review, "valid": valid})

    valid_reviews = [item["review"] for item in attempts if item["valid"]]
    scores = [review["visual_opportunity_score"] for review in valid_reviews]
    complete = len(valid_reviews) == REPLICATE_COUNT
    retryable = any(item["status"].get("retryable", False) for item in attempts if not item["valid"])
    if complete:
        exact_mean = statistics.mean(scores)
        # Scorecards are integer-valued; round half up deterministically.
        score = int(exact_mean + 0.5)
        verdict = "pass" if score < 50 else "medium" if score < 75 else "high"
        review = dict(valid_reviews[0])
        replicate_calibrations = [item.get("score_calibration") or {} for item in valid_reviews]
        factor_keys = sorted({key for calibration in replicate_calibrations
                              for key in (calibration.get("factor_points") or {})})
        factor_point_means = {}
        for key in factor_keys:
            points = [(calibration.get("factor_points") or {}).get(key, {}).get("points", 0)
                      for calibration in replicate_calibrations]
            factor_point_means[key] = statistics.mean(points)
        review.update({
            "visual_opportunity_score": score,
            "visual_verdict": verdict,
            "site_outcome": verdict,
            "replicate_count": REPLICATE_COUNT,
            "replicate_scores": scores,
            "replicate_score_mean": exact_mean,
            "replicate_score_min": min(scores),
            "replicate_score_max": max(scores),
            "replicate_score_range": max(scores) - min(scores),
            "replicate_aggregation": "arithmetic_mean_rounded_half_up",
            "replicate_reviews": valid_reviews,
            "score_calibration": {
                "version": (replicate_calibrations[0].get("version") if replicate_calibrations else None),
                "method": "Arithmetic mean of three independently Python-scored assessments using the same validated four screenshots; rounded half up to an integer; published verdict bands applied after averaging.",
                "factor_points_mean": factor_point_means,
                "stale_copyright_points_mean": statistics.mean(
                    calibration.get("stale_copyright_points", 0) for calibration in replicate_calibrations
                ),
                "replicate_scores": scores,
                "replicate_score_mean": exact_mean,
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
