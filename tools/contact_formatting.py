"""Canonical formatting for North American business contact phone numbers."""
from __future__ import annotations

import re
from urllib.parse import unquote


_PHONE_CANDIDATE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s().-]*)?\(?\d{3}\)?[\s().-]*\d{3}[\s.-]*\d{4}"
    r"(?:\s*(?:ext(?:ension)?\.?|x|#)\s*\d{1,6})?(?!\d)",
    re.IGNORECASE,
)
_EXTENSION_RE = re.compile(r"(?:ext(?:ension)?\.?|x|#)\s*[:.]?\s*(\d{1,6})\s*$", re.IGNORECASE)
_TEL_EXTENSION_RE = re.compile(r";\s*ext(?:ension)?\s*=?\s*(\d{1,6})", re.IGNORECASE)


def normalize_phone(value: str | None) -> str:
    """Format a US/Canada number as ``(###) ###-####`` and drop a leading +1.

    Dots, spaces, hyphens, and missing opening parentheses are accepted as
    input. A recognized extension is retained after the canonical base number.
    Unsupported or incomplete numbers return an empty string rather than an
    inconsistently formatted value.
    """
    text = unquote(str(value or "")).strip()
    if not text:
        return ""
    text = re.sub(r"^tel:", "", text, flags=re.IGNORECASE).split("?", 1)[0]
    tel_extension = _TEL_EXTENSION_RE.search(text)
    extension = tel_extension.group(1) if tel_extension else ""
    if tel_extension:
        text = text[:tel_extension.start()]

    match = _PHONE_CANDIDATE_RE.search(text)
    candidate = match.group(0) if match else text
    extension_match = _EXTENSION_RE.search(candidate)
    if extension_match:
        extension = extension_match.group(1)
        candidate = candidate[:extension_match.start()]

    digits = re.sub(r"\D", "", candidate)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""

    formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return f"{formatted} ext. {extension}" if extension else formatted
