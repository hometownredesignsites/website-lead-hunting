# Visual opportunity scoring

This rubric is shared by the ChatGPT and Duck.ai visual-review workflows. Scripted technical priority remains a separate initial measure. The visual review receives structured script-audit evidence and four screenshot views; each source is used only for the factors it can support.

## Required factors

Each completed assessment must rate all five factors and include specific supporting evidence:

| Factor | Evidence source | What it covers |
| --- | --- | --- |
| Hero imagery | Screenshots | Presence and presentation of meaningful first-view imagery |
| Readability | Screenshots | Legibility of visible text, including text over imagery |
| Layout and mobile use | Screenshots | Hierarchy, content organization, and desktop/mobile usability |
| Broken images | Script audit | Failed image loads and the audit's lazy-load results |
| Interactions | Script audit | Tested controls and interaction failures |

The model must not describe script findings as visually observed. Visual observations and script evidence remain separately identified in the scorecard.

## Durable score

The validator calculates the score from these fixed factor ratings, not from how many issue sentences or clusters the model writes:

| Factor severity | Points |
| --- | ---: |
| None | 0 |
| Minor | 5 |
| Moderate | 25 |
| Severe | 50 |

`score = min(100, sum(points for the five factors))`

Verdict bands remain Pass 0–49, Medium 50–74, and High 75–100. The model-reported score/verdict are retained separately; the validator-calculated score and verdict are authoritative.

If a factor is explicitly `not_assessable`, the result is Uncertain with a null score. Missing factor ratings or supporting evidence invalidate the scorecard and trigger the existing failure/retry path. The validator never silently assigns zero to missing evidence.

## Durable evidence

Each accepted scorecard stores the normalized factor ratings, factor evidence and source, structured script-audit summary, screenshot observations, explanatory issue notes, the model-reported score/verdict, and the complete raw model response. The private candidate ledger retains these with the durable calculated score. Screenshots and detailed browser diagnostics remain in the public workflow's seven-day artifact.

## Human-label calibration

The benchmark joins the interactive `decision` field to the newest provider scorecards by normalized host. It reports coverage, per-label outcome comparisons, provider agreement, and score differences. Use a deterministic, stratified holdout for evaluation; do not use holdout sites to tune factors or thresholds. The current human file has 21 High and 42 Medium labels, no Pass examples. The updated v2 visual instructions clarify that materially dated or amateur presentation, weak hero imagery, sparse portfolios, and poor image treatment can be redesign signals when visible in the screenshots. Severity weights and verdict thresholds are unchanged pending assessment of the human-labeled Medium cohort. Do not claim balanced accuracy or threshold quality until the remaining classes are evaluated, and add human-reviewed Pass examples.

See [HUMAN_REVIEW_BENCHMARK.md](HUMAN_REVIEW_BENCHMARK.md) for benchmark use and limitations.
