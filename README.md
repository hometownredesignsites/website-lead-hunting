# ChatGPT Website Lead Hunting

This repository runs local-business discovery and direct website assessment in GitHub Actions. The discovery step asks ChatGPT through a headed Playwright/Xvfb runner; the site-review step visits eligible candidate sites and captures temporary desktop and mobile screenshots.

## Inputs and batch size

The workflow reads two committed CSVs under `config/`:

- `config/business_categories.csv` — one `category` column.
- `config/cities.csv` — one `city` column; values containing commas should be quoted.

The included rows are starter examples. Replace them with the complete approved category and city lists before a market run. The planning step prints the Cartesian product and the current batch size before research begins.

Open **Actions → ChatGPT website lead hunt → Run workflow** on the `git-chatgpt` branch. Use a stable `run_key` to resume the same inputs. A changed category/city set requires a new key. Each manual dispatch processes up to 20 pending category/city prompts and assesses up to 10 sites per prompt. Runs are serial and bounded; the current workflow has no schedule or automatic fan-out. It uses a six-hour job timeout, so large lists must be run in measured batches. A 3,200-query input requires at least 160 dispatches at 20 queries each. Benchmark smaller batches before increasing volume.

`Capture Google evidence` is an optional comparison pass that stores raw Google result snippets and reported counts beside ChatGPT's results. It is off by default. Google evidence extraction does not visit or independently assess every Google result.

## GitHub Actions runtime

The workflows use Node.js 24-compatible action releases: `actions/checkout@v5`, `actions/setup-python@v6`, and `actions/upload-artifact@v6`. The production ChatGPT runner uses headed Chromium under Xvfb through `tools/chatgpt_file_playwright_anonymous_ci.py` and the supporting `tools/playwright_runner/` modules. The runner owns response wait, settle, and retry behavior. The lead-hunt code consumes its validated response. The workflow records failures and diagnostics and does not attempt CAPTCHA handling.

The prompt requests independent local businesses with direct official website URLs, supporting source URLs, and service-area evidence. The workflow filters known directories and aggregators, checks candidate URLs and redirects against public-address rules, visits eligible sites directly, and keeps automated visual suggestions separate from the human decision.

## Run outputs and audit

Each `runs/<run_key>/` folder contains:

- `RUN_MANIFEST.json` — input fingerprint, source CSV hashes, engine, and review policy.
- `QUERY_LEDGER.jsonl` — prompt, response paths, sources, status, returned candidates, and dispositions.
- `CANDIDATE_LEDGER.jsonl` — deduplicated candidate sites, direct-site evidence, screenshot paths, and review fields.
- `LOCATION_CATEGORY_PROGRESS.json` and `BEHAVIOR_LOG.jsonl` — progress and chronological events.
- `GOOGLE_SEARCH_LEDGER.jsonl` and `search_evidence/google/` — optional raw Google comparison evidence.
- `RUN_SUMMARY.json` and `RUN_SUMMARY.md` — validator report with expected/processed queries, returned/reviewed candidates, dispositions, site visits, and review status.
- `chatgpt/` and `screenshots/` — response diagnostics and temporary images used for visual inference.

`tools/validate_run.py` reconciles query indices and CSV inputs, candidate dispositions, review counts, and screenshot references. The workflow writes a summary even when the research step fails, so partial progress stays inspectable.

## Temporary image scoring and cleanup

Image files are temporary inference inputs. The workflow uploads the complete run folder as a GitHub Actions artifact for seven days. The **Commit screenshots** switch now defaults to `false`, so branch commits retain ledgers and reports without adding new image files. Turn it on only when a run needs image files in the branch. Artifacts are still uploaded either way and expire after seven days.

The main lead-hunt workflow does not yet run visual inference or save model visual scores into the candidate ledger. For the existing Hermes path, download and extract an artifact into `runs/<run_key>/`, configure the local Hermes CLI/provider, and run:

```bash
python tools/complete_visual_reviews.py --run-dir runs/<run-key>
python tools/validate_run.py --run-dir runs/<run-key> --write-summary
python tools/purge_screenshots.py --run-key <run-key>
python tools/purge_screenshots.py --run-key <run-key> --apply
```

The first purge command is a dry run. `--apply` deletes all image files in that run after every candidate with a saved website screenshot has an assessed Hermes review. It preserves ledgers and text evidence, records the purge in the manifest, and removes the image files from the current branch tip once the cleanup commit is pushed. Hermes visual scoring uses desktop candidate screenshots; it is advisory and does not complete the human decision. Keep the original artifact until scoring and review are complete. It expires after seven days.

Images committed by earlier workflow runs remain in older Git commits. Removing them from the repository's full history requires a history rewrite and force push; the current change prevents future image commits by default but does not rewrite existing history.

## ChatGPT runner check

**Actions → ChatGPT Xvfb runner check** sends one-off prompts through the same runner. It uploads the answer, metadata, success screenshot, and diagnostics as a seven-day artifact; it does not commit probe files to a run folder. **Attach test image** checks basic image upload and recognition with a synthetic fixture. **Review saved candidate** reads a saved candidate's desktop and mobile screenshots from `runs/<review_run_key>/`, crops the top viewport from each full-page capture to preserve legibility, sends both to guest ChatGPT, and validates a structured visual redesign score, verdict, viewport observations, confidence, and limitations against the candidate ledger. Its prompt uses the visual-opportunity rubric in the website-lead-hunting skill. The review output and cropped screenshots stay in the seven-day artifact. This is an end-to-end capability probe; it does not yet update the lead-hunt candidate ledger or replace Hermes in the main batch workflow.

The runner compatibility fix was exercised by [GitHub Actions run #5](https://github.com/hometownredesignsites/website-lead-hunting/actions/runs/41374972944), using one Kitchen remodelers query and one site assessment for Westminster, CO. That run committed response data and desktop/mobile screenshots under `runs/westminster-kitchen-selector-test-20261101/`.

## Local checks

```bash
python -m unittest discover -s tests -v
python -m py_compile tools/lead_hunt.py tools/chatgpt_file_playwright_anonymous_ci.py tools/playwright_runner/browser_session.py
```

Visual-review outputs are written locally. Commit and push review ledgers and reports after purging temporary images if you want scored results persisted in GitHub.
