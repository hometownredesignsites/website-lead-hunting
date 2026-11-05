# Human review benchmark

Use the private human-review labels to compare the system's current assessments against the decisions made during interactive review. The `decision` column is the label of record; `markdown_decision` is not substituted when the two disagree. Keep the input labels, notes, and generated benchmark CSV in the private operations repository.

The benchmark utility matches rows by normalized website host and joins the newest versioned factor scorecard from each provider ledger. Legacy issue-cluster scores and older factor versions are excluded so they are not compared with the current rubric. A candidate can match by host while its provider assessment remains `not_assessed` until it has a current-version factor scorecard. A validated assessment with uncertain or missing required evidence is reported as `uncertain`, not as a numeric score. The ChatGPT v7 rubric applies 45-point floors for script-confirmed absent first-view hero media and moderate/severe dated design, scores image quality and design freshness explicitly, and uses Python as the sole score/verdict authority; there is no post-evaluation provider-specific score weighting.

```powershell
python tools/build_human_review_benchmark.py `
  --human-csv ..\website-lead-hunting-operations\config\human_review_calibration_labels.csv `
  --candidate-ledger ..\website-lead-hunting-operations\runs\<chatgpt-run>\CANDIDATE_LEDGER.jsonl `
  --candidate-ledger ..\website-lead-hunting-operations-duckai\runs\<duckai-run>\CANDIDATE_LEDGER.jsonl `
  --output ..\website-lead-hunting-operations\runs\<benchmark-run>\HUMAN_REVIEW_BENCHMARK.csv
```

The output keeps human decisions and notes alongside provider execution status, site outcome, assessment version, Python-calculated score/verdict, factor states and evidence, confidence, and review run ID. It intentionally excludes contact email and phone columns. Legacy scores are labeled `legacy_excluded`; a missing current-version scorecard remains `not_assessed`; failed, retry-needed, uncertain, and blocked assessments are not converted into pass labels. The builder also writes a companion `.summary.json` with coverage counts, a High/Medium/Pass confusion table, per-class false positives and false negatives, and the rate of paired provider scores within 10 points.

Each CSV row gets a stable, stratified split: one row of every five within each human-decision class is assigned to `holdout`; the others are `tuning`. Use tuning rows to adjust factor definitions and scoring. Keep holdout rows untouched until evaluating a proposed change. The split is reproducible from the normalized website host, so rebuilding the benchmark preserves the assignment.

The supplied human-review CSV contains 21 High and 42 Medium labels, with no Pass examples. Previous reports included legacy scores; those scores are not valid new-system predictions. Fresh v7 factor-based assessments must be collected for labeled sites before tuning decision thresholds or measuring false positives; add human-reviewed Pass examples as well. Keep holdout rows untouched until a proposed scoring change is ready for evaluation. Keep benchmark outputs private because they contain business-level review decisions and notes.
