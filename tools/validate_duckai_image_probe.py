#!/usr/bin/env python3
"""Check whether Duck.ai accepted and correctly read the guest image probe."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    response = args.response.read_text(encoding="utf-8").lower()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    attachment = metadata.get("image_attachment") or {}
    result = {
        "attachment_selected": attachment.get("status") == "uploaded",
        "visual_content_recognized": "lilac otter 638" in response and "orange" in response,
        "attachment": attachment,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["attachment_selected"] and result["visual_content_recognized"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
