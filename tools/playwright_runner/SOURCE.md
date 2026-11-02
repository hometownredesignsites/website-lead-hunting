The ChatGPT browser runner and its Playwright support modules are used by the
GitHub Actions lead-hunting workflow to submit prompts, wait for completed
responses, and retry failed sends.

The response-submission, wait/settle, and retry flow in
`chatgpt_file_playwright_anonymous_ci.py` and `stage_runner.py` is retained. The lead-hunting
integration adds a success screenshot option and calls the runner from the resumable per-query
workflow. `browser_session.py` also recognizes the current anonymous ChatGPT shell's
`data-message-role="assistant"` message attribute and skips empty assistant rows that can follow
a completed response. This selector compatibility fix was verified in GitHub Actions run #5.
