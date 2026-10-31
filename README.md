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
