The ChatGPT browser runner and its Playwright support modules were copied from
`https://github.com/benspilker/git-inference` at revision
`1b5654d8ff4cbd4e8d870a27eb44af878520ecf5`.

The response-submission and wait/recovery flow in `chatgpt_file_playwright_anonymous_ci.py`
and `stage_runner.py` is retained. The lead-hunting integration only adds a success screenshot
option and calls the runner from the resumable per-query workflow.
