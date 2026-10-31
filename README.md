# DuckDuckGo Xvfb Search Probe

A small GitHub Actions probe for checking whether DuckDuckGo returns extractable search results from headed Chromium running under Xvfb on a GitHub-hosted Ubuntu runner.

The default query is `in-ground pool companies Indianapolis, IN`. Dispatch the workflow manually to use it or enter another single query.

## What it does

1. Starts Xvfb with a 1440×1000 display.
2. Launches Chromium in headed mode through Playwright.
3. Opens DuckDuckGo once and records the response status, final URL, visible result selectors, and up to five sample organic results.
4. Saves a full-page screenshot and JSON summary as a 7-day GitHub Actions artifact.
5. Stops without interacting if a challenge page is detected.

The probe does not open result websites, solve challenges, or disguise browser identity. A successful search confirms that the runner can retrieve and parse one DuckDuckGo results page; it does not validate businesses or establish a complete lead-hunting pipeline.

## Run it

1. Open **Actions** in the repository.
2. Select **DuckDuckGo Xvfb search probe**.
3. Select **Run workflow**, optionally edit the query, and start the run.
4. Download the run artifact to inspect `duckduckgo-result.json` and `duckduckgo-page.png`.

The expected successful status is `results_found`. The workflow fails when the probe returns a challenge, extracts no results, or encounters an error; artifacts are uploaded whenever possible.

## Local use

Requires Python 3.11+, Playwright, Chromium, and Xvfb:

```bash
python -m pip install "playwright==1.58.0"
python -m playwright install chromium
xvfb-run --auto-servernum --server-args="-screen 0 1440x1000x24" \
  python tools/search_engine_xvfb_probe.py \
    --engine duckduckgo \
    --query "in-ground pool companies Indianapolis, IN" \
    --artifacts-dir artifacts/duckduckgo-xvfb-probe
```

## Resumable website lead-hunt pipeline

The `Website lead hunt` GitHub Actions workflow runs a bounded DDG search batch in headed Chromium under Xvfb, performs first-pass result classification, visits eligible public business websites, captures desktop and mobile screenshots, records observable technical signals, and commits the run folder back to the selected branch. It does not ask a vision model to make the final redesign decision.

### Start or continue a run

1. Open **Actions → Website lead hunt → Run workflow**.
2. Set a stable `run_key`, category and location lists (one per line), and the batch/site limits.
3. The workflow stores the run at `runs/<run_key>/`. Reusing that key with the same category and location inputs resumes pending queries and preserves completed query evidence. Use a new key when changing inputs; a mismatched fingerprint is rejected.
4. The workflow commits JSONL ledgers and screenshots to the branch that was selected when dispatching. A seven-day artifact is also uploaded as a backup.

Each search is capped to 20 queries per invocation and 8 direct-site checks per query. It stops if DDG presents a challenge. Sponsored results, known directories/lead-generation sites, social/video pages, and common editorial/listicle results are recorded with a disposition and are not visited as business sites. Domain exclusions are maintained in `config/excluded_domains.txt`.

The machine-generated `automated_priority` is based only on captured technical signals. `hermes_visual_review` and `human_review` remain pending; search rank, website metrics, and screenshots do not prove that a company is active, local, or a good sales prospect. Review candidate identity and market fit separately, then run Hermes locally against the saved desktop screenshots:

```bash
python tools/complete_visual_reviews.py --run-dir runs/<run_key>
```

Hermes writes its visual assessment into `HERMES_VISUAL_REVIEW_LEDGER.jsonl` and each candidate row. Its `visual_priority` is kept separate from the human decision. The mobile screenshot is evidence for a person to inspect; the Hermes prompt is explicitly limited to the desktop screenshot and does not infer mobile behavior.

### Screenshot retention

Keep the screenshots until every screenshot-bearing candidate has an `assessed` Hermes review. From a local checkout, first preview the number of files to remove, then apply the purge:

```bash
python tools/purge_screenshots.py --run-key <run_key>
python tools/purge_screenshots.py --run-key <run_key> --apply
```

Commit the deletion and updated manifest. If repository storage warrants removing those image blobs from Git history too, the separate `--rewrite-history` option requires the exact repo/run confirmation string, a clean committed `main` checkout, installed `git-filter-repo`, and no remaining screenshots. It rewrites local history and force-pushes `main`; coordinate that disruptive operation with other collaborators. Do not run it until the review and purge commit are complete.

### Local checks

```bash
python -m unittest discover -s tests -v
python -m py_compile tools/lead_hunt.py tools/complete_visual_reviews.py tools/purge_screenshots.py
```
