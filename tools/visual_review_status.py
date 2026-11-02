#!/usr/bin/env python3
"""Classify one isolated ChatGPT visual-review attempt."""
from __future__ import annotations

from collections.abc import Mapping


PASS = "passed"
FAIL = "failed"
RETRY = "retry-needed"

IDENTITY_ERRORS = (
    "candidate_id does not match the source candidate",
    "website_url does not match the source candidate",
    "candidate context must list exactly four",
)
TRANSIENT_RUNNER_MARKERS = (
    "chatgpt did not confirm all uploaded images",
    "response remained empty",
    "could not find composer",
    "timed out",
    "timeout",
    "net::err_",
    "err_connection",
    "connection reset",
    "connection refused",
    "xvfb failed to start",
    "target page, context or browser has been closed",
)
PERMANENT_RUNNER_MARKERS = (
    "captcha",
    "human verification",
    "module not found",
    "no such file or directory",
    "invalid argument",
    "permission denied",
    "syntaxerror",
)


def classify_attempt(
    *,
    source_artifact_available: bool,
    preparation_ok: bool,
    process_exit_code: int | None,
    result: Mapping | None,
    preparation_error: str | None = None,
    runner_log_text: str = "",
) -> dict:
    """Return a durable status and whether another fresh-session attempt is useful.

    Missing source files and invalid candidate identity are deterministic input or
    integrity failures. ChatGPT transport, blank/invalid output, and image-upload
    confirmation failures can recover in a clean browser session.
    """
    if not source_artifact_available:
        return {"status": FAIL, "retryable": False,
                "failure_class": "source_artifact_unavailable",
                "error": "Lead-hunt source artifact is unavailable."}
    if not preparation_ok:
        return {"status": FAIL, "retryable": False,
                "failure_class": "screenshots_unavailable_or_invalid",
                "error": preparation_error or "Candidate screenshots could not be prepared."}

    if isinstance(result, Mapping) and result.get("status") == PASS and process_exit_code == 0:
        return {"status": PASS, "retryable": False, "failure_class": None, "error": None}

    errors = result.get("errors", []) if isinstance(result, Mapping) else []
    if isinstance(errors, str):
        errors = [errors]
    errors = [str(error) for error in errors] if isinstance(errors, list) else []
    identity_error = next((error for error in errors
                           if any(marker in error for marker in IDENTITY_ERRORS)), None)
    if identity_error:
        return {"status": FAIL, "retryable": False,
                "failure_class": "candidate_or_screenshot_identity_mismatch",
                "error": identity_error}

    if errors:
        if any("screenshots were not confirmed uploaded" in error for error in errors):
            failure_class = "screenshot_upload_unconfirmed"
        elif any("no parseable" in error for error in errors):
            failure_class = "blank_or_unparseable_response"
        else:
            failure_class = "invalid_scorecard"
        return {"status": RETRY, "retryable": True, "failure_class": failure_class,
                "error": "; ".join(errors)}

    log_lower = runner_log_text.lower()
    permanent_marker = next((marker for marker in PERMANENT_RUNNER_MARKERS if marker in log_lower), None)
    if permanent_marker:
        return {"status": FAIL, "retryable": False,
                "failure_class": "nonrecoverable_runner_error",
                "error": f"Runner reported a nonrecoverable error ({permanent_marker})."}
    transient_marker = next((marker for marker in TRANSIENT_RUNNER_MARKERS if marker in log_lower), None)
    if process_exit_code in {124, 130, 137, 143} or transient_marker:
        return {"status": RETRY, "retryable": True,
                "failure_class": "chatgpt_interrupted_or_unavailable",
                "error": (f"Runner reported a transient failure ({transient_marker})."
                          if transient_marker else
                          f"Runner was interrupted or timed out (exit {process_exit_code}).")}
    return {"status": FAIL, "retryable": False,
            "failure_class": "nonrecoverable_runner_error",
            "error": f"Runner exited with status {process_exit_code} without a recognized recoverable failure."}
