# Visual opportunity scoring

This rubric defines the durable score written after the four-image visual assessment. It is shared by the ChatGPT and Duck.ai workflows. The scripted technical-priority score is a separate measure and is not part of this calculation.

The visual-review prompt now also carries a structured summary of the initial script audit, including hero-media detection, HTTP and resource findings, broken images, contact/privacy signals, link and interaction checks, content dates, and legacy Twitter branding. The audit is attached to the durable visual scorecard as `script_audit_evidence`. The model must keep that evidence separate from screenshot observations, visual issue clusters, and the screenshot-only visual score. This provides context and preserves provenance without treating a script result as a visual observation.

## Authoritative score

The validator calculates the durable score from the issue clusters in the assessment:

```text
score = min(100, 30 + 10 × moderate_cluster_count + 20 × severe_cluster_count)
```

The score bands are:

| Durable score | Verdict |
| --- | --- |
| 0–49 | Pass |
| 50–74 | Medium |
| 75–100 | High |
| No score | Uncertain |

The formula advances in ten-point steps, so the first reachable High score is 80. Examples: five moderate clusters produce 80; one severe plus three moderate clusters produce 80; two severe plus one moderate cluster produce 80. Two severe clusters alone produce 70, which is Medium.

`visual_opportunity_score` and `visual_verdict` are the authoritative, recalculated values. The model's response is retained separately in `model_reported_visual_opportunity_score` and `model_reported_visual_verdict` for audit. The model's raw score does not determine the durable score or verdict. If the visual evidence is insufficient and the response is `uncertain`, the score stays null.

## What counts as an issue cluster

- Count distinct, screenshot-supported root problems with a clear user-facing impact.
- Count a repeated observation of the same root problem once, even if it appears in multiple views. The assessment prompt asks the model to consolidate these; the validator counts the submitted cluster entries and does not independently deduplicate them.
- Do not count minor polish, preferences, speculative impacts, or ambiguous text sizing as material issues.
- A moderate cluster adds 10 points; a severe cluster adds 20. Severity is based on the demonstrated impact in the screenshots, not on the number of observations.
- `None observed`, `none`, `n/a`, and similar empty-list markers do not count as clusters.

## Missing severity headings

When either the `Moderate issue clusters` or `Severe issue clusters` section is missing from a response, the validator uses the listed `Major issues` as a fallback: each non-empty major issue counts as one moderate cluster, and no severe clusters are inferred. The scorecard records this source as `major_issues_moderate_fallback`; when both severity sections are present, it records `explicit_issue_clusters` and counts those sections instead.

This fallback keeps otherwise usable guest-chat responses scoreable while avoiding an unsupported severe classification. It can make the durable score differ from the model's raw score and verdict.

## Paired example: Christie's

The same candidate and lead-hunt screenshot artifact were reviewed on both branches:

| Provider branch | Workflow run | Model-reported score/verdict | Durable score/verdict | Evidence used |
| --- | --- | --- | --- | --- |
| `git-chatgpt` | [41771379776](https://github.com/hometownredesignsites/website-lead-hunting/actions/runs/41771379776) | 30 / Pass | 30 / Pass | No issue clusters reported |
| `git-duckai` | [41771342547](https://github.com/hometownredesignsites/website-lead-hunting/actions/runs/41771342547) | 55 / Medium | 40 / Pass | Severity sections were omitted; one major issue was counted as one moderate cluster |

The durable scores are ten points apart and both classify the site as Pass. The raw model scores are retained for transparency and can differ more because the providers may describe or rate the same evidence differently. Shared calibration makes the output scale consistent; it does not guarantee identical model judgments.

## Where scorecards are stored

The public ChatGPT and Duck.ai workflows write durable scorecards to their corresponding branches in `hometownredesignsites/website-lead-hunting-operations`. Candidate records use `chatgpt_visual_review` or `duckai_visual_review` and include the durable score, raw model score, verdicts, cluster counts, and calibration source. Screenshots and detailed inference diagnostics remain in the public workflow's short-lived Actions artifact.
