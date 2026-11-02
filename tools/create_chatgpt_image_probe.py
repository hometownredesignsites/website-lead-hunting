#!/usr/bin/env python3
"""Create a small screenshot fixture for the ChatGPT guest image-attachment probe."""
from __future__ import annotations

import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright


HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}body{margin:0;width:1100px;height:760px;background:#f2f5f9;
font-family:Arial,sans-serif;display:grid;place-items:center}.card{width:900px;height:560px;background:white;
border:8px solid #1f2937;border-radius:28px;display:flex;align-items:center;justify-content:space-around;
box-shadow:0 20px 50px #64748b55}.label{font-size:28px;color:#475569;text-transform:uppercase;letter-spacing:5px}
.code{font-size:72px;font-weight:800;color:#111827;margin-top:20px}.circle{width:180px;height:180px;border-radius:50%;background:#f97316;
border:10px solid #9a3412}.foot{font-size:20px;color:#64748b;margin-top:20px}
</style></head><body><main class="card"><section><div class="label">Image reading probe</div>
<div class="code">LILAC OTTER 638</div><div class="foot">Read the code and the circle color.</div></section>
<div class="circle" aria-label="orange circle"></div></main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 820}, device_scale_factor=1)
        page.set_content(HTML, wait_until="load")
        page.screenshot(path=str(output), full_page=True)
        browser.close()
    print(f"Created image probe screenshot: {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
