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

The workflows use Node.js 24-compatible action releases: `actions/checkout@v5`, `actions/setup-python@v6`, and `actions/upload-artifact@v6`. The production ChatGPT runner uses headed Chromium under Xvfb through `tools/chatgpt_file_playwright_anonymous_ci.py` and the supporting `tools/playwright_runner/` modules. It waits for and validates responses, can request a short same-chat follow-up when a visual response is blank or lacks a scorecard, and records diagnostics. The batch workflow retries only recoverable failures in a new Xvfb/ChatGPT session with a new browser profile. It does not attempt CAPTCHA handling.

The prompt requests independent local businesses with direct official website URLs, supporting source URLs, and service-area evidence. The workflow filters known directories and aggregators, checks candidate URLs and redirects against public-address rules, visits eligible sites directly, and keeps automated visual suggestions separate from the human decision.

## Run outputs and audit

Each `runs/<run_key>/` folder contains:

- `RUN_MANIFEST.json` — input fingerprint, source CSV hashes, engine, and review policy.
- `QUERY_LEDGER.jsonl` — prompt, response paths, sources, status, returned candidates, and dispositions.
- `CANDIDATE_LEDGER.jsonl` — deduplicated candidate sites, direct-site evidence, structured site-check findings, screenshot paths, and review fields.
- `LOCATION_CATEGORY_PROGRESS.json` and `BEHAVIOR_LOG.jsonl` — progress and chronological events.
- `GOOGLE_SEARCH_LEDGER.jsonl` and `search_evidence/google/` — optional raw Google comparison evidence.
- `RUN_SUMMARY.json` and `RUN_SUMMARY.md` — validator report with expected/processed queries, returned/reviewed candidates, dispositions, site visits, and review status.
- `HIGH_MEDIUM_CANDIDATES.csv` — import-ready shortlist of candidates with either an automated or ChatGPT visual high/medium opportunity signal. The export includes scores, current review/attempt statuses, and concise evidence notes.
- `CHATGPT_VISUAL_REVIEW_RETRY_QUEUE.json` — durable retry state for candidates awaiting an automatic delayed review.
- `chatgpt/` — text response records and diagnostics. Screenshots are attached to the seven-day Actions artifact for inference and are not committed to the repository.

`tools/validate_run.py` reconciles query indices and CSV inputs, candidate dispositions, review counts, and screenshot references. The workflow writes a summary even when the research step fails, so partial progress stays inspectable.

## Automated website checks

Each directly assessed candidate now gets a `site_checks` object in `CANDIDATE_LEDGER.jsonl`. It keeps observable checks separate from the visual score and human decision. `RUN_SUMMARY.json` and `RUN_SUMMARY.md` report aggregate outcomes.

- **HTTP links and insecure resources:** records declared HTTP anchors and resources, request/status, redirects, final URL, and whether the destination ends on HTTPS. It checks at most 12 prioritized HTTP or same-site links per homepage; entries beyond the cap are reported as partial coverage. Browser-observed HTTP resources include page assets such as stylesheets, scripts, images, fonts, and media.
- **Broken images:** scrolls up to 20 lazy-loaded images into view before measuring and capturing. Failed image entries include URL, alt text, `loading` state, and whether the bounded lazy-load pass completed. CSS background image failures are included when the browser reports a failed image request.
- **Contact form and privacy link:** detects native HTML forms and visible form-like field/submit groups on the home page, then, when needed, checks up to two linked same-site contact pages until a form is found. It reports whether a privacy-policy link was found on inspected pages; it does not judge legal adequacy.
- **Contact email domain:** looks for publicly displayed email addresses in homepage text and `mailto:` links, and checks up to two linked same-site contact pages when the homepage has no email or no detected form. A recognized consumer provider (such as Gmail, Yahoo, or Outlook) whose domain differs from the website domain is reported as a weak small-business opportunity clue. It does not establish business size or redesign need.
- **Broken links and interactions:** checks up to 12 prioritized HTTP or same-site links, same-page fragment targets, browser request failures, and JavaScript console errors. It smoke-tests up to eight accessible menu, disclosure, or tab controls. It never submits contact forms or clicks purchase, account, or other transactional controls. A 401/403/429 response is reported as restricted/rate-limited rather than automatically called a broken link.
- **Blog freshness:** looks for linked blog/news pages, then tries a few conventional same-site routes. It reports the latest published date and source URL when found, otherwise `not_found`.
- **Content freshness without a blog:** checks visible `<time>` elements, article dates, structured `datePublished`/`dateModified`, relevant portfolio/project pages, HTTP `Last-Modified`, and sitemap `lastmod`. Structured/visible content dates are stronger signals; server and sitemap dates are weak clues. Copyright years are excluded: a current year can be generated automatically and does not show when content was updated. An old displayed year is reported separately as a weak possible stale-footer clue and can never establish content freshness. If no meaningful content date is found, the result is `unknown`, not a claim that the site is stale. The report includes date age in days and an age band when a date is available.
- **Legacy Twitter branding:** flags likely Twitter bird/logo assets when no recognizable X logo asset is found. It does not treat a Twitter account link by itself as an old logo; asset detection is heuristic and should be visually confirmed.

The initial technical screen also records the homepage response status and final HTTPS scheme, title and metadata presence, contact path and CTA presence, image load ratio, mobile viewport metadata, and horizontal overflow at a 390 px viewport. It captures desktop and mobile page screenshots and uses these signals for an initial technical-opportunity score. A detected consumer-email/domain mismatch adds 5 opportunity points only if the technical score was already medium or high; it cannot turn a technically passing candidate into a medium. An old copyright year (at least five years old) is one low-weight outdatedness signal; a current year adds no freshness evidence.

These checks are bounded for batch runtime, so the result includes scope limits and can report `partial` or `unknown`. They inspect the homepage plus a small set of relevant same-site pages, not every URL on a business's domain. Four-image ChatGPT visual assessment is a separate stage. It covers desktop top/mid and mobile top/mid views for visual judgments such as hero imagery, text contrast, layout, hierarchy, clutter, image presentation, and mobile usability. The visual score follows the rubric used by the successful browser run: high for a severe flaw or several major flaws, and medium for a meaningful opportunity such as dated styling, weak hierarchy, sparse or poor project photos, text-heavy layout, or confusing navigation. The screenshots must visibly support the score; this review does not make functional claims that require scripted checks.

For a group of sites, use **Actions → ChatGPT visual review batch**. Enter the source lead-hunt run ID and run key, then paste candidate IDs separated by commas or newlines. The workflow starts one isolated Xvfb/ChatGPT job per candidate, with at most two review jobs running at once and `fail-fast` disabled. Each candidate gets up to three attempts; each retry closes the previous browser and starts a new Xvfb process and clean browser profile. The same-chat text follow-up for an empty or scoreless first response still happens within that attempt before it is classified for a fresh-session retry. A permanent input or identity problem is `failed`; a recoverable problem that persists after all attempts, or a candidate job interrupted before it uploads results, is `retry-needed`; a validated scorecard is `passed`. Candidate failures do not stop other candidates or mark the batch failed. After all jobs finish, the workflow merges valid scorecards into their matching `chatgpt_visual_review` candidate-ledger fields, records each attempt status and failure class, refreshes the run summary, and commits those text records to the selected branch. Detailed responses, screenshots, network logs, and runner diagnostics stay in the seven-day per-candidate artifacts. The batch workflow succeeds when candidate-level reviews fail, provided the results and statuses can be merged and saved. A workflow-level failure is reserved for execution problems that prevent the batch from being planned, merged, or committed. Recoverable candidates marked `retry-needed` are queued for up to two delayed automatic rounds; run the batch manually after those rounds if another attempt is warranted.

The fixture-driven recovery checks cover missing source screenshots, screenshot upload mismatches, unavailable source artifacts, blank responses, invalid scorecards, identity mismatches, and missing per-candidate artifacts from interrupted jobs. Run them with `python -m unittest tests.test_visual_review_recovery -v`; they do not make live ChatGPT calls.

## Automatic visual-review retries

When a batch merge records a recoverable `retry-needed` result, it writes a due time 30 minutes later to `CHATGPT_VISUAL_REVIEW_RETRY_QUEUE.json`. **Actions → Schedule ChatGPT visual review retries** checks due entries every five minutes, verifies that the original lead-hunt artifact still exists, and dispatches only candidates whose latest attempt still needs a retry. It commits each retry reservation before dispatching the review workflow, so the review job can validate the reservation from the branch. This normally starts the retry about 30–35 minutes after failure; GitHub may delay scheduled events under load. The scheduler runs from the default branch and shares the lead-hunt concurrency lock so queue writes and ledger merges do not race.

Each dispatched batch still uses up to three fresh ChatGPT sessions per candidate. The scheduler allows two delayed rounds after the initial batch (up to nine session attempts total). A passing score resolves the queue entry, a permanent `failed` result is not retried, and candidates still marked `retry-needed` after both automatic rounds are left for manual review. If the source artifact has expired or is unavailable, the queue records that new screenshots are required instead of repeatedly dispatching reviews that cannot start. Source artifacts are retained for seven days.

## Temporary image scoring and cleanup

Images are temporary inference inputs. The workflow uploads them with the complete run folder as a GitHub Actions artifact for seven days. They are excluded from Git commits by policy and `.gitignore`; the visual-review workflow reads screenshots only from the source Actions artifact and does not fall back to repository files. The generated high/medium CSV and candidate ledger remain durable in the branch after the artifact expires.

The main lead-hunt workflow does not automatically run visual inference. For the existing Hermes path, download and extract an artifact into `runs/<run_key>/`, configure the local Hermes CLI/provider, and run:

```bash
python tools/complete_visual_reviews.py --run-dir runs/<run-key>
python tools/validate_run.py --run-dir runs/<run-key> --write-summary
python tools/purge_screenshots.py --run-key <run-key>
python tools/purge_screenshots.py --run-key <run-key> --apply
```

The first purge command is a dry run. `--apply` deletes local image files after every candidate with a saved website screenshot has an assessed Hermes review. It preserves ledgers and text evidence and records the purge in the manifest. This optional local cleanup is for extracted or downloaded run folders; GitHub Actions artifacts expire after seven days. Hermes visual scoring uses desktop candidate screenshots; it is advisory and does not complete the human decision.

Older screenshots were removed from both branch tips and Git history as part of the cleanup. This history rewrite changes commit IDs; collaborators with existing clones should fetch the rewritten branches and re-clone or reset local branches before pushing.

## ChatGPT runner check

**Actions → ChatGPT Xvfb runner check** sends one-off prompts through the same Xvfb runner. It uploads the answer, metadata, success screenshot, and diagnostics as a seven-day artifact; it does not commit probe files to a run folder. **Attach test image** checks basic image upload and recognition with a synthetic fixture. **Review saved candidate** is a single-candidate capability probe: it requires the numeric source run ID from the lead-hunt Actions URL and downloads that run's `lead-hunt-<run_key>-<run_id>` artifact, which includes the candidate ledger and temporary screenshots. It reads the candidate and screenshots only from that downloaded artifact. If the artifact, ledger, or any source screenshot is unavailable, the workflow fails; it does not fall back to checked-in repository files. It prepares four JPEG viewport crops: desktop top, desktop mid-page, mobile top, and mobile mid-page. Mid-page crops are centered at 35% of the desktop and 60% of the mobile screenshot's scrollable height. All four are attached to guest ChatGPT. This one-off probe can refresh and retry within its existing browser session; the batch workflow instead uses fresh sessions per retry and persists candidate statuses and scorecards.

The runner compatibility fix was exercised by [GitHub Actions run #5](https://github.com/hometownredesignsites/website-lead-hunting/actions/runs/41374972944), using one Kitchen remodelers query and one site assessment for Westminster, CO. Its response data remains in the run folder; screenshots are retained only in the short-lived Actions artifact.

## Local checks

```bash
python -m unittest discover -s tests -v
python -m py_compile tools/lead_hunt.py tools/chatgpt_file_playwright_anonymous_ci.py tools/playwright_runner/browser_session.py
```

The batch workflow commits scorecards, attempt status, text summaries, and the refreshed high/medium CSV. Source screenshots and review diagnostics remain in seven-day Actions artifacts and are not added to Git history.
