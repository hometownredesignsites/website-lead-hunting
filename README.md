# ChatGPT Website Lead Hunting

This repository runs local-business discovery and website review in GitHub Actions. The discovery step asks ChatGPT through the same headed Playwright/Xvfb runner used by `benspilker/git-inference`; the site-review step checks candidate websites directly and captures desktop and mobile screenshots.

## Start or continue a run

1. Open **Actions → ChatGPT website lead hunt → Run workflow**.
2. Set a stable `run_key`, categories and named locations (one per line), and the batch/site limits.
3. Use the same key and inputs to resume. A changed category/location/region set requires a new key.
4. The workflow processes up to 20 pending category/location prompts in a run, and checks up to 8 direct business websites per prompt. The default batch is smaller to keep runs manageable.
5. The Action commits the run folder to the selected branch and also uploads a seven-day backup artifact.

The default inputs target kitchen remodelers in Westminster, Colorado.

## ChatGPT browser runner

The production workflow runs the ChatGPT UI with headed Chromium under Xvfb using `xvfb-run --auto-servernum --server-args='-screen 0 1920x1080x24'`. It uses `tools/chatgpt_file_playwright_anonymous_ci.py` and the supporting `tools/playwright_runner/` modules from the successful `git-inference` ChatGPT pipeline (source revision `1b5654d8ff4cbd4e8d870a27eb44af878520ecf5`). The response wait, settle, and retry behavior stays in that runner; the lead-hunt code calls it as a child process and consumes its validated JSON response.

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

## ChatGPT runner check

**Actions → ChatGPT Xvfb runner check** sends a one-off prompt through the same runner used by lead hunting. It uploads the answer, metadata, success screenshot, and failure diagnostics as a seven-day artifact; it does not commit those probe files to a run folder.

## Screenshot retention

Screenshots are committed with the ledgers so the evidence stays with the run. Git history will grow as runs accumulate. Keep these outputs free of secrets and personal session data; the ChatGPT browser profile is temporary and excluded from the saved run.

## Local checks

```bash
python -m unittest discover -s tests -v
python -m py_compile tools/lead_hunt.py tools/chatgpt_file_playwright_anonymous_ci.py
```
