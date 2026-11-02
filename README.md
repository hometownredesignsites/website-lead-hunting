# ChatGPT Website Lead Hunting

This repository runs local-business discovery and website review in GitHub Actions. The discovery step asks ChatGPT through a headed Playwright/Xvfb runner; the site-review step checks candidate websites directly and captures desktop and mobile screenshots.

## Start or continue a run

1. Open **Actions → ChatGPT website lead hunt → Run workflow** and choose the `git-chatgpt` branch in **Use workflow from**.
2. Set a stable `run_key`, categories and named locations (one per line), and the batch/site limits.
3. Use the same key and inputs to resume. A changed category/location/region set requires a new key.
4. The workflow processes up to 20 pending category/location prompts in a run, and checks up to 8 direct business websites per prompt. The default batch is smaller to keep runs manageable.
5. The Action commits the run folder to the selected branch and also uploads a seven-day backup artifact.

The default inputs target kitchen remodelers in Westminster, Colorado.

## GitHub Actions runtime

The workflows use Node.js 24-compatible action releases: `actions/checkout@v5` or later, `actions/setup-python@v6` or later, and `actions/upload-artifact@v6` or later. Keep these actions on Node 24-based releases to avoid the Node.js 20 deprecation warning.

## ChatGPT browser runner

The production workflow runs the ChatGPT UI with headed Chromium under Xvfb using `xvfb-run --auto-servernum --server-args='-screen 0 1920x1080x24'`. It uses `tools/chatgpt_file_playwright_anonymous_ci.py` and the supporting `tools/playwright_runner/` modules. The response wait, settle, and retry behavior stays in that runner; the lead-hunt code calls it as a child process and consumes its validated JSON response. The ChatGPT message selector also supports the anonymous shell's current `data-message-role="assistant"` markup and ignores empty trailing assistant rows.

Each query has two outer attempts, and the runner itself can retry a failed send after refreshing the chat. Successful response text, parsed JSON, runner metadata, and a full-page ChatGPT screenshot are saved in `runs/<run_key>/chatgpt/query_NNNN/`. Failure screenshots and HTML diagnostics are retained there too. The temporary browser profile is deleted after each attempt and is never committed.

The prompt requests independent local businesses with direct official website URLs, supporting source URLs, and service-area evidence. Invalid JSON or an empty response leaves the query pending for a later resume. The workflow records failures and screenshots rather than attempting CAPTCHA handling.

## Run outputs

Each `runs/<run_key>/` folder contains:

- `RUN_MANIFEST.json` — the input fingerprint, engine, and review policy.
- `QUERY_LEDGER.jsonl` — per-query prompt, response paths, sources, status, results, and dispositions.
- `CANDIDATE_LEDGER.jsonl` — unique candidate websites, direct-site evidence, screenshots, and pending review fields.
- `LOCATION_CATEGORY_PROGRESS.json` — batch progress and terminal status.
- `BEHAVIOR_LOG.jsonl` — chronological query, browser, retry, and site-review events.
- `chatgpt/` — prompts, instructions, raw responses, parsed JSON, runner metadata, screenshots, and error diagnostics.
- `screenshots/` — desktop and mobile screenshots of candidate websites.

ChatGPT discovery is not treated as proof of official-site ownership or service-area fit. The workflow filters known directories and aggregators, checks the candidate URL and redirects against public-address rules, visits eligible official sites directly, and keeps both Hermes visual review and human review pending.

## Visual and human review

The GitHub Action saves full-page desktop and mobile screenshots for each assessed business. Hermes visual scoring is a separate local step; it is not run by the GitHub Action. With the Hermes CLI configured for the NVIDIA provider, run:

```bash
python tools/complete_visual_reviews.py --run-dir runs/<run-key>
```

This updates `CANDIDATE_LEDGER.jsonl` and writes `HERMES_VISUAL_REVIEW_LEDGER.jsonl` using the desktop screenshot only. Hermes scoring is advisory and does not complete the human decision. Review ChatGPT's source URLs and service-area claims, inspect the direct website and screenshots, then record the human decision separately in the candidate ledger.

## ChatGPT runner check

**Actions → ChatGPT Xvfb runner check** sends a one-off prompt through the same runner used by lead hunting. It uploads the answer, metadata, success screenshot, and failure diagnostics as a seven-day artifact; it does not commit those probe files to a run folder.

The runner compatibility fix was exercised by [GitHub Actions run #5](https://github.com/hometownredesignsites/website-lead-hunting/actions/runs/41374972944), using one Kitchen remodelers query and one site assessment for Westminster, CO. The successful run committed the response, metadata, and desktop/mobile screenshots under `runs/westminster-kitchen-selector-test-20261101/`.

## Screenshot retention

Screenshots are committed with the ledgers so the evidence stays with the run. Git history will grow as runs accumulate. Keep these outputs free of secrets and personal session data; the ChatGPT browser profile is temporary and excluded from the saved run.

## Local checks

```bash
python -m unittest discover -s tests -v
python -m py_compile tools/lead_hunt.py tools/chatgpt_file_playwright_anonymous_ci.py tools/playwright_runner/browser_session.py
```

Visual-review helper outputs are written locally; commit and push the run folder if you want them persisted to GitHub.
