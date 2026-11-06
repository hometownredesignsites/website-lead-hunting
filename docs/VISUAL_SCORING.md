# Visual opportunity scoring

This rubric is used by the ChatGPT visual-review workflow. Scripted technical priority remains a separate initial measure. The visual review receives structured script-audit evidence and four screenshot views: desktop and mobile top, plus a mid-page view at 25% of desktop and 48% of mobile scrollable height. Before assessment, a capture-validation step requires the mid crop to be at least 150 pixels below the top crop, verifies it is not an identical image, and checks measured browser scroll movement on fresh captures. If a distinct mid view cannot be verified, the candidate is marked failed with diagnostics and ChatGPT is not sent duplicate screenshots. Page-flow measurements separately record where the first meaningful image and contact form occur. These four views match the earlier successful ChatGPT image-review setup; the middle crops are representative and are not guaranteed to show a particular semantic section.

## Required factors

Each completed assessment must classify all seven factors with an explicit assessment state and specific evidence or a reason the factor cannot be rated:

| Factor | Evidence source | What it covers |
| --- | --- | --- |
| Hero imagery | Screenshots | Presence and presentation of meaningful first-view imagery |
| Image quality | Screenshots | Visible blur, pixelation, stretching, poor crops, and quality across the portfolio |
| Design freshness | Screenshots | Concrete signs that the overall visual language looks dated; not trend conformity |
| Readability | Screenshots | Legibility of visible text, including text over imagery |
| Layout and mobile use | Screenshots | Hierarchy, content organization, and desktop/mobile usability |
| Broken images | Script audit | Confirmed failed image loads and the lazy-image sample coverage |
| Interactions | Script audit | Tested controls and interaction failures |

The model must not describe script findings as visually observed. Visual observations and script evidence remain separately identified in the scorecard.

For broken images, all lazy-loaded images are checked when there are 20 or fewer. If the page has more, a 20-image sample is sufficient evidence for this factor; remaining lazy images do not make the scorecard uncertain. The validator uses the script audit as the authority for a clear or sufficient sampled result with no confirmed failures and sets the factor to `assessed: none`, even if the model labels it uncertain. `ERR_ABORTED` request cancellations are retained as transient diagnostics and are not counted as confirmed broken images by themselves. The scorecard must report when the finding is sample-based. Only confirmed failures in the browser or network evidence count as broken images.

Each factor stores `state` as `assessed`, `not_applicable`, or `uncertain`. Assessed factors also store severity (`none`, `minor`, `moderate`, or `severe`); not-applicable and uncertain factors have no severity. Every factor stores a concrete evidence statement and its required source. The parser accepts common source-label spelling variants, uses labeled factor text over a duplicate structured JSON copy, and records the canonical source according to the factor schema; formatting differences alone do not invalidate otherwise complete evidence. “No control was tested” is not evidence that controls work.

The preliminary script audit also checks for image-enlargement affordances on the homepage. When it sees at least four substantial images and finds no direct-image link or explicit lightbox/zoom markup, it adds a small technical-opportunity signal. This is a bounded heuristic: JavaScript can provide a lightbox without an obvious markup hint, so “not detected” is not proof that enlargement is unavailable. It is not one of the seven visual factors and does not establish that an image interaction is broken.

For **design freshness**, require concrete evidence from typography, effects, controls, spacing, iconography, or the overall visual language. Use minor for one limited outdated detail, moderate for dated patterns repeated across prominent sections, and severe when the pervasive presentation looks obsolete and materially weakens credibility. A site does not need to follow current trends to receive no points, and one isolated stylistic preference is not enough. Treat copyright years as a separate weak footer clue; neither a current generated year nor an old displayed year determines the design-freshness rating.

For **image quality**, score what is visibly apparent in the screenshots. An isolated weak image is minor; several prominent images that are blurry, pixelated, stretched, or poorly cropped are moderate; low-quality imagery that dominates the hero or portfolio is severe. This can be a redesign opportunity even when every image loads successfully. Do not infer quality from source filenames or pixel dimensions alone. Avoid double-counting: use hero imagery for first-view composition, image quality for the images themselves, and design freshness for the site's broader visual language.

## Durable score

The validator calculates the score from these fixed factor ratings, not from how many issue sentences or clusters the model writes:

| Factor severity | Points |
| --- | ---: |
| None | 0 |
| Minor | 5 |
| Moderate | 25 |
| Severe | 50 |

If the script confirms the first viewport has no prominent image, video, or background image, the hero-imagery factor receives at least 45 points. A moderate or severe design-freshness finding also receives at least 45 points: that signals a repeated or pervasive dated visual language and represents a redesign opportunity on par with missing hero media. Each floor replaces a lower value within its own factor; it does not count the same issue twice. A minor, isolated outdated detail remains 5 points. Other independently evidenced factor issues add their points.

`score = min(100, sum(points for assessed factors after the applicable hero-media and dated-design floors) + stale-copyright points)`

Verdict bands remain Pass 0–49, Medium 50–74, and High 75–100. ChatGPT returns factor evidence only. Python is the sole authority for the numeric score and site outcome. `review_execution_status` records whether ChatGPT returned a response, `scorecard_validation_status` records whether that response passed validation, retry status records whether another attempt is needed, and `site_outcome` records High, Medium, Pass, or Uncertain. These fields describe separate things; a completed review can still have an invalid or uncertain scorecard.

An uncertain state on any required factor yields an Uncertain site outcome with a null score. A not-applicable factor contributes zero only when there is evidence that the check truly does not apply. For interactions, no discovered safe controls means not applicable; discovered controls left untested means uncertain; tested controls are assessed from their observed results. Missing factor states, severities for assessed factors, evidence, or source invalidate the scorecard and trigger the existing failure/retry path. Explicit model score/verdict headings and prose that contradicts a factor's assessment also invalidate the scorecard. Invalid responses are not saved as numeric site ratings. The validator never silently assigns zero to missing evidence.

## Durable evidence

For each candidate, capture validation runs before image assessment. A passing validation establishes one four-image set: desktop top/mid and mobile top/mid. Three independent ChatGPT browser sessions receive the exact same four files; each session has a fresh Xvfb display and browser profile. This isolates variation in model/session responses from variation in screenshots.

All three responses must produce valid, non-uncertain scorecards before a durable aggregate rating is written. Python calculates the arithmetic mean of the three individual scores, rounds half up to an integer, and applies the same Pass 0–49, Medium 50–74, and High 75–100 bands. The ledger record includes all three individual scores and factor evidence, the mean, minimum, maximum, range, and each raw model response. If capture validation fails, images are not submitted. If one or more assessment replicates fail validation or are uncertain, the candidate receives no aggregate score and enters the existing retry or failed path.

Each scorecard record stores process status separately from validation status, retry state, and site outcome, alongside normalized factor states and severities, factor evidence and source, structured script-audit summary, page-flow measurements, screenshot observations, explanatory issue notes, the Python-calculated score/verdict when valid, and the complete raw model responses. Invalid or incomplete replicate sets are not accepted as numeric results. The private candidate ledger retains these durable results. Screenshots and detailed browser diagnostics remain in the public workflow's seven-day artifact. No post-evaluation provider-specific weight or score offset is applied.

## Human-label calibration

The benchmark joins the interactive `decision` field to the newest provider scorecards by normalized host. It reports coverage, per-label outcome comparisons, provider agreement, and score differences. Use a deterministic, stratified holdout for evaluation; do not use holdout sites to tune factors or thresholds. The current human file has 21 High and 42 Medium labels, no Pass examples. The v8 rubric applies 45-point floors to confirmed absent first-view hero media and moderate/severe dated design, scores image quality and design freshness explicitly, requires factor state/evidence validation, and treats cancelled image requests as transient diagnostics. Historical v3 through v7 scores are excluded from current-version benchmark comparisons; rerun candidates under v8 before using them for calibration. Do not claim balanced accuracy or threshold quality until the remaining classes are evaluated, and add human-reviewed Pass examples.

See [HUMAN_REVIEW_BENCHMARK.md](HUMAN_REVIEW_BENCHMARK.md) for benchmark use and limitations.
