from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .browser_session import (
    assistant_turns,
    find_chat_composer,
    send_prompt,
    attach_image_files,
    stabilize_response,
    wait_for_valid_response,
)
from .diagnostics import save_failure_diagnostics
from .prompt_contracts import extract_json_payload
from .recovery import click_retry_if_visible, refresh_chat


class ResponseFollowupError(RuntimeError):
    """Raised when Duck.ai stays silent after a same-chat text follow-up."""


def run_stage_once(
    page,
    timeout_ms: int,
    prompt_text: str,
    wait_seconds: int,
    post_response_wait_seconds: int,
    response_settle_seconds: int,
    max_settle_wait_seconds: int,
    allow_retry: bool = True,
    refresh_before_retry: bool = False,
    stage_name: str = "generic",
    error_screenshot: Path | None = None,
    image_files: list[Path] | None = None,
    expect_review_score: bool = False,
) -> tuple[str, dict]:
    metadata = {
        "attempt": 1,
        "retry_count": 0,
        "used_retry": False,
        "page_refreshed": False,
        "thread_reused": True,
        "new_chat_started": False,
        "failure_reason": None,
        "attempt_failures": [],
        "image_attachment": None,
        "image_attachments": [],
        "response_followup_used": False,
        "response_followup_reason": None,
    }
    start = time.time()

    def _capture_attempt_failure(attempt_no: int, reason: str) -> None:
        if error_screenshot is None:
            return
        try:
            screenshot = error_screenshot.with_name(
                f"{error_screenshot.stem}.stage-{stage_name}.attempt-{attempt_no}{error_screenshot.suffix}"
            )
            html_path = screenshot.with_suffix(".html")
            save_failure_diagnostics(
                page,
                screenshot,
                metadata={
                    "stage_name": stage_name,
                    "attempt": attempt_no,
                    "failure_reason": reason,
                    "captured_at_epoch": time.time(),
                },
                html_path=html_path,
            )
            metadata["attempt_failures"].append(
                {
                    "attempt": attempt_no,
                    "failure_reason": reason,
                    "screenshot": str(screenshot),
                    "html": str(html_path),
                }
            )
        except Exception:
            pass

    def _single_attempt():
        composer = find_chat_composer(page, timeout_ms=timeout_ms)
        if composer is None:
            raise RuntimeError("Could not find composer for stage execution.")
        files = list(image_files or [])
        metadata["image_batches"] = 1 if files else 0
        if files:
            metadata["image_attachments"] = attach_image_files(
                page, files, timeout_ms=timeout_ms
            )
            if len(metadata["image_attachments"]) == 1:
                metadata["image_attachment"] = metadata["image_attachments"][0]
        assistant_messages = assistant_turns(page)
        before_count = assistant_messages.count()
        before_last_text = ""
        if before_count > 0:
            try:
                before_last_text = assistant_messages.last.inner_text().strip()
            except Exception:
                before_last_text = ""
        send_prompt(page, composer, prompt_text)
        page.wait_for_timeout(1200)
        assistant_messages = assistant_turns(page)
        response_text = wait_for_valid_response(
            page,
            assistant_messages,
            before_count=before_count,
            before_last_text=before_last_text,
            wait_seconds=wait_seconds,
            current_prompt=prompt_text,
        )
        settle_prompt_text = prompt_text
        followup_reason = None
        if not response_text:
            followup_reason = "empty_response"
        elif (expect_review_score and extract_json_payload(response_text) is None
              and not re.search(r"^\s*Visual opportunity score\s*:", response_text, re.IGNORECASE | re.MULTILINE)):
            followup_reason = "missing_scorecard"
        if followup_reason:
            metadata["response_followup_used"] = True
            metadata["response_followup_reason"] = followup_reason
            followup_composer = find_chat_composer(page, timeout_ms=timeout_ms)
            if followup_composer is None:
                raise ResponseFollowupError(
                    "Duck.ai response needed a follow-up, but no composer was available in the same chat."
                )
            followup_messages = assistant_turns(page)
            followup_before_count = followup_messages.count()
            followup_before_last_text = ""
            if followup_before_count > 0:
                try:
                    followup_before_last_text = followup_messages.last.inner_text().strip()
                except Exception:
                    followup_before_last_text = ""
            if followup_reason == "empty_response":
                followup_prompt = (
                    "Your previous response was empty. Please retry the visual assessment using the two "
                    "labeled screenshot sheets already attached and the candidate context in this chat. Follow the original "
                    "rubric and output format. Include the explicit score, verdict, confidence, and labeled "
                    "observation sections. Return the concise plain-text scorecard without a preamble."
                )
            else:
                followup_prompt = (
                    "Your previous response did not include the required explicit scorecard. Please retry the "
                    "visual assessment using the two labeled screenshot sheets already attached and the candidate context "
                    "in this chat. Follow the original rubric and output format, include the labeled score, "
                    "verdict, confidence, and observation sections, and return only the concise plain-text "
                    "scorecard without a preamble."
                )
            send_prompt(page, followup_composer, followup_prompt)
            settle_prompt_text = followup_prompt
            page.wait_for_timeout(1200)
            followup_messages = assistant_turns(page)
            response_text = wait_for_valid_response(
                page,
                followup_messages,
                before_count=followup_before_count,
                before_last_text=followup_before_last_text,
                wait_seconds=wait_seconds,
                current_prompt=followup_prompt,
            )
            if not response_text:
                raise ResponseFollowupError(
                    "Assistant response remained empty after the same-chat text follow-up."
                )
            assistant_messages = followup_messages
        refreshed = stabilize_response(
            page,
            assistant_messages,
            post_response_wait_seconds=post_response_wait_seconds,
            response_settle_seconds=response_settle_seconds,
            max_settle_wait_seconds=max_settle_wait_seconds,
            current_prompt=settle_prompt_text,
        )
        return refreshed or response_text

    try:
        response = _single_attempt()
    except Exception as exc:
        _capture_attempt_failure(1, str(exc))
        if isinstance(exc, ResponseFollowupError):
            metadata["failure_reason"] = str(exc)
            raise
        if not allow_retry:
            metadata["failure_reason"] = str(exc)
            raise
        metadata["used_retry"] = True
        metadata["retry_count"] = 1
        metadata["attempt"] = 2
        retried_with_click = click_retry_if_visible(page)
        metadata["retry_clicked"] = bool(retried_with_click)
        # Always refresh before retrying to recover transient composer/UI failures.
        refresh_chat(page, timeout_ms)
        metadata["page_refreshed"] = True
        try:
            response = _single_attempt()
        except Exception as retry_exc:
            _capture_attempt_failure(2, str(retry_exc))
            metadata["failure_reason"] = str(retry_exc)
            raise

    end = time.time()
    metadata["start_time"] = start
    metadata["end_time"] = end
    metadata["duration_seconds"] = round(end - start, 3)
    return response, metadata


def write_stage_outputs(
    response_text: str,
    output_file: Path,
    metadata_output_file: Path | None,
    parsed_output_file: Path | None,
    stage_name: str,
    expect_json: bool,
    run_metadata: dict | None = None,
    response_mode: str = "text",
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(response_text + "\n", encoding="utf-8")

    parsed = extract_json_payload(response_text) if expect_json else None
    if expect_json and parsed_output_file is not None and parsed is not None:
        parsed_output_file.parent.mkdir(parents=True, exist_ok=True)
        parsed_output_file.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if metadata_output_file is not None:
        metadata_output_file.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "stage_name": stage_name,
            "expect_json": expect_json,
            "parsed_json": parsed is not None if expect_json else False,
            "response_mode": response_mode,
            "response_chars": len(response_text or ""),
        }
        if isinstance(run_metadata, dict):
            metadata.update(run_metadata)
        metadata_output_file.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
