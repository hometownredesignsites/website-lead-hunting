# Visual opportunity scoring

This rubric is used by the ChatGPT visual-review workflow. Scripted technical priority remains a separate initial measure. The visual review receives structured script-audit evidence and four screenshot views: desktop and mobile top, plus a mid-page view at 25% of desktop and 48% of mobile scrollable height. Page-flow measurements separately record where the first meaningful image and contact form occur. These four views match the earlier successful ChatGPT image-review setup; the middle crops are representative and are not guaranteed to show a particular semantic section.

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

If the script confirms the first viewport has no prominent image, video, or background image, the hero-imagery factor receives at least 45 points. This replaces a lower point value for that same hero issue; it does not count the absent hero twice. Other independently evidenced factor issues add their points.

`score = min(100, sum(points for the five factors after the absent-hero floor))`

Verdict bands remain Pass 0–49, Medium 50–74, and High 75–100. The model-reported score/verdict are retained separately; the validator-calculated score and verdict are authoritative.

If a factor is explicitly `not_assessable`, the result is Uncertain with a null score. Missing factor ratings or supporting evidence invalidate the scorecard and trigger the existing failure/retry path. The validator never silently assigns zero to missing evidence.

## Durable evidence

Each accepted scorecard stores the normalized factor ratings, factor evidence and source, structured script-audit summary, page-flow measurements, screenshot observations, explanatory issue notes, the model-reported score/verdict, and the complete raw model response. The private candidate ledger retains these with the durable calculated score. Screenshots and detailed browser diagnostics remain in the public workflow's seven-day artifact. No post-evaluation provider-specific weight or score offset is applied.

## Human-label calibration

The benchmark joins the interactive `decision` field to the newest provider scorecards by normalized host. It reports coverage, per-label outcome comparisons, provider agreement, and score differences. Use a deterministic, stratified holdout for evaluation; do not use holdout sites to tune factors or thresholds. The current human file has 21 High and 42 Medium labels, no Pass examples. The v3 rubric treats a confirmed text-only first viewport as a 45-point issue, then adds other independent evidence through named factors. Do not claim balanced accuracy or threshold quality until the remaining classes are evaluated, and add human-reviewed Pass examples.

See [HUMAN_REVIEW_BENCHMARK.md](HUMAN_REVIEW_BENCHMARK.md) for benchmark use and limitations.
