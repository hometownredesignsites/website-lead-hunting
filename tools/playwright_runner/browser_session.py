from __future__ import annotations

import json
import re
import time
from pathlib import Path


def first_visible_locator(page, selectors: list[str], timeout_ms: int):
    # Keep selector probing quick; we loop repeatedly in callers, so long
    # per-selector waits only add latency without improving reliability.
    per_selector_timeout = min(700, max(250, int(timeout_ms / max(1, len(selectors)))))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=per_selector_timeout)
            return locator
        except Exception:
            continue
    return None


def dismiss_blocking_modals(page) -> None:
    modal_selectors = [
        "#modal-moonshine-nux-v2",
        "[data-testid='modal-moonshine-nux-v2']",
    ]
    close_selectors = [
        "button[aria-label='Close']",
        "button:has-text('Close')",
        "button:has-text('Got it')",
        "button:has-text('Continue')",
        "button:has-text('Skip')",
        "button:has-text('Done')",
    ]
    for modal_selector in modal_selectors:
        modal = page.locator(modal_selector).first
        try:
            if modal.count() == 0 or not modal.is_visible():
                continue
        except Exception:
            continue
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
        for close_selector in close_selectors:
            try:
                btn = modal.locator(close_selector).first
                if btn.count() > 0 and btn.is_visible():
                    try:
                        btn.click(timeout=1500)
                    except Exception:
                        handle = btn.element_handle()
                        if handle is not None:
                            handle.evaluate("(el) => el.click()")
                    page.wait_for_timeout(250)
                    break
            except Exception:
                continue


def find_chat_composer(page, timeout_ms: int):
    selectors = [
        "#prompt-textarea",
        "[data-testid='prompt-textarea']",
        "textarea[data-testid='prompt-textarea']",
        "form textarea#prompt-textarea",
        "form textarea[placeholder*='Message']",
        "form textarea",
        "textarea[placeholder*='Message']",
        "textarea[aria-label*='Message']",
        "textarea",
        "[contenteditable='true'][id*='prompt']",
        "[contenteditable='true'][data-testid*='prompt']",
        "[contenteditable='true'][data-lexical-editor='true']",
        "[contenteditable='true'][aria-label*='Message']",
        "[contenteditable='true'][role='textbox']",
        "div[contenteditable='true'][role='textbox']",
    ]
    deadline = time.time() + max(1.0, timeout_ms / 1000.0)
    while time.time() < deadline:
        dismiss_blocking_modals(page)
        composer = first_visible_locator(page, selectors, timeout_ms=min(2500, max(1000, timeout_ms)))
        if composer is not None:
            try:
                if composer.is_enabled():
                    return composer
            except Exception:
                return composer
        page.wait_for_timeout(350)
    return None


def assistant_turns(page):
    selectors = [
        "[data-testid^='conversation-turn-'][data-turn='assistant'] [data-message-author-role='assistant']",
        "[data-testid^='conversation-turn-'][data-turn='assistant']",
        "[data-message-author-role='assistant']",
        # ChatGPT's anonymous mobile shell uses this attribute on message list items.
        "[data-message-role='assistant']",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return page.locator("[data-message-author-role='assistant']")


def extract_response_text(assistant_messages, before_count: int, before_last_text: str) -> str:
    def _normalize(text: str) -> str:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^\s*chatgpt\s+said:\s*\n+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*chatgpt\s*:\s*\n+", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    after_count = assistant_messages.count()
    if after_count == 0:
        return ""

    # The ChatGPT anonymous shell may append empty assistant list items after a
    # completed response (for example, for stopped/continuation UI state). Walk
    # newest to oldest and return the latest actual message text instead of
    # treating the final empty list item as an empty response.
    start = min(before_count, after_count - 1) if after_count > before_count else 0
    for index in range(after_count - 1, start - 1, -1):
        try:
            text = _normalize(assistant_messages.nth(index).inner_text())
        except Exception:
            continue
        if text and text != before_last_text and not looks_like_non_answer_text(text):
            return text
    return ""


def looks_like_non_answer_text(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    blocked_markers = [
        "i hit an internal automation issue while generating a response",
        "please send your message again",
        "new session started",
    ]
    return any(marker in lowered for marker in blocked_markers)


def set_composer_text(composer, text: str) -> None:
    handle = composer.element_handle()
    if handle is None:
        raise RuntimeError("Failed to resolve composer element.")
    handle.evaluate(
        """(el, value) => {
            const isEditable = el.getAttribute('contenteditable') === 'true' || el.isContentEditable;
            if (isEditable) {
                el.focus();
                el.textContent = '';
                const lines = value.split('\\n');
                for (let i = 0; i < lines.length; i++) {
                    if (i > 0) el.appendChild(document.createElement('br'));
                    el.appendChild(document.createTextNode(lines[i]));
                }
                el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return;
            }
            el.focus();
            el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        text,
    )


def attach_image_files(page, image_paths: list[Path], timeout_ms: int = 15000) -> list[dict]:
    """Attach one or more local images through ChatGPT's visible composer file input."""
    image_paths = [path.expanduser().resolve() for path in image_paths]
    if not image_paths:
        return []
    for image_path in image_paths:
        if not image_path.is_file():
            raise FileNotFoundError(f"Image attachment does not exist: {image_path}")
        if image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("ChatGPT image attachments must be PNG, JPEG, or WebP files")

    file_inputs = page.locator("input[type='file']")
    if file_inputs.count() == 0:
        plus_selectors = [
            "button[data-testid='composer-plus-btn']",
            "button[aria-label*='Attach']",
            "button[aria-label*='attach']",
            "button[aria-label*='Upload']",
            "button[aria-label*='upload']",
        ]
        clicked = False
        for selector in plus_selectors:
            candidate = page.locator(selector).first
            try:
                if candidate.count() and candidate.is_visible() and candidate.is_enabled():
                    candidate.click(timeout=2000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            raise RuntimeError("Could not find a visible ChatGPT attachment control")
        deadline = time.time() + max(1, timeout_ms / 1000)
        while time.time() < deadline and file_inputs.count() == 0:
            page.wait_for_timeout(250)

    if file_inputs.count() == 0:
        raise RuntimeError("ChatGPT did not expose a file input after opening the attachment menu")
    input_count = file_inputs.count()
    selected = file_inputs.nth(input_count - 1)
    selected.set_input_files([str(image_path) for image_path in image_paths], timeout=timeout_ms)
    deadline = time.time() + max(1, timeout_ms / 1000)
    uploaded: list[dict] = []
    while time.time() < deadline:
        try:
            attachment_inputs = page.locator("input[name='imageAttachments']")
            pending_inputs = page.locator("input[name='pendingImageUploads']")
            if attachment_inputs.count():
                raw_attachments = attachment_inputs.first.evaluate("el => el.value")
                attachments = json.loads(raw_attachments or "[]")
                pending = (json.loads(pending_inputs.first.evaluate("el => el.value") or "[]")
                           if pending_inputs.count() else [])
                uploaded_by_name = {item.get("name"): item for item in attachments
                                    if isinstance(item, dict)}
                uploaded = [uploaded_by_name[path.name] for path in image_paths
                            if path.name in uploaded_by_name]
                if len(uploaded) == len(image_paths) and not pending:
                    break
        except Exception:
            uploaded = None
        page.wait_for_timeout(250)
    if not uploaded or len(uploaded) != len(image_paths):
        raise RuntimeError("ChatGPT did not confirm all uploaded images in its composer")
    page.wait_for_timeout(500)
    return [{"status": "uploaded", "filename": image_path.name,
             "bytes": image_path.stat().st_size, "file_id": item.get("fileId"),
             "width": item.get("width"), "height": item.get("height"),
             "file_input_count": input_count}
            for image_path, item in zip(image_paths, uploaded)]


def attach_image_file(page, image_path: Path, timeout_ms: int = 15000) -> dict:
    """Backward-compatible single-image helper."""
    return attach_image_files(page, [image_path], timeout_ms=timeout_ms)[0]


def send_prompt(page, composer, prompt_text: str) -> None:
    dismiss_blocking_modals(page)
    try:
        composer.click(timeout=3000)
    except Exception:
        composer.click(force=True, timeout=3000)
    set_composer_text(composer, prompt_text)
    send_selectors = [
        "button[data-testid='send-button']",
        "button[aria-label*='Send']:not([data-testid='composer-plus-btn'])",
        "button[aria-label*='send']:not([data-testid='composer-plus-btn'])",
        "form button:not([data-testid='composer-plus-btn']):has(svg[data-icon='arrow-up'])",
    ]
    deadline = time.time() + 15
    while time.time() < deadline:
        for selector in send_selectors:
            btn = page.locator(selector).first
            try:
                if btn.count() == 0 or not btn.is_visible() or not btn.is_enabled():
                    continue
                btn.click(timeout=2000)
                return
            except Exception:
                continue
        page.wait_for_timeout(300)
    composer.press("Enter")


def wait_for_valid_response(
    page,
    assistant_messages,
    before_count: int,
    before_last_text: str,
    wait_seconds: int,
) -> str:
    deadline = time.time() + max(1, wait_seconds)
    while time.time() < deadline:
        text = extract_response_text(assistant_messages, before_count, before_last_text)
        lowered = text.lower()
        if text and "something went wrong" not in lowered and not looks_like_non_answer_text(text):
            return text
        page.wait_for_timeout(1000)
    return ""


def stabilize_response(
    page,
    assistant_messages,
    post_response_wait_seconds: int,
    response_settle_seconds: int,
    max_settle_wait_seconds: int,
) -> str:
    if post_response_wait_seconds > 0:
        page.wait_for_timeout(post_response_wait_seconds * 1000)
    stable_for = 0
    waited = 0
    previous = ""
    while waited < max_settle_wait_seconds:
        stop_visible = False
        try:
            stop_visible = page.locator(
                "button[data-testid='stop-button'], button:has-text('Stop generating')"
            ).first.is_visible()
        except Exception:
            pass
        try:
            current = assistant_messages.last.inner_text().strip()
        except Exception:
            current = ""
        if current and current == previous and not stop_visible:
            stable_for += 1
        else:
            stable_for = 0
        previous = current
        if stable_for >= max(1, response_settle_seconds):
            break
        page.wait_for_timeout(1000)
        waited += 1
    return previous
