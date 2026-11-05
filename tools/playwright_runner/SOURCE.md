The ChatGPT browser runner and its Playwright support modules power both the
GitHub Actions lead-hunt workflow and the separate visual-review workflows.
They submit prompts, wait for completed responses, classify retryable failures,
and save response and browser diagnostics.

The response-submission, wait/settle, and same-session retry flow is implemented
in `chatgpt_file_playwright_anonymous_ci.py` and `stage_runner.py`. The lead-hunt
workflow invokes it once per pending category/city query under Xvfb. The visual
review workflow invokes it once per candidate and attaches four desktop/mobile top and
mid-page screenshots. The scheduled retry workflow can
start a later job with a fresh browser profile for recoverable outcomes; it does
not replace the runner's in-session retry.

`browser_session.py` recognizes assistant messages in the anonymous ChatGPT
shell using `data-message-role="assistant"` and ignores empty assistant rows
that can appear after a completed response. The current end-to-end visual-review
and ledger-checkpoint behavior was verified in [GitHub Actions run #7](https://github.com/hometownredesignsites/website-lead-hunting/actions/runs/41565515143).
