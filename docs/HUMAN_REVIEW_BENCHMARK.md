# Human review benchmark

Use the private human-review labels to compare the system's current assessments against the decisions made during interactive review. The `decision` column is the label of record; `markdown_decision` is not substituted when the two disagree. Keep the input labels, notes, and generated benchmark CSV in the private operations repository.

The benchmark utility matches rows by normalized website host and joins the newest available scorecard from each provider ledger:

```powershell
python tools/build_human_review_benchmark.py `
  --human-csv ..\website-lead-hunting-operations\config\human_review_calibration_labels.csv `
  --candidate-ledger ..\website-lead-hunting-operations\runs\<chatgpt-run>\CANDIDATE_LEDGER.jsonl `
  --candidate-ledger ..\website-lead-hunting-operations-duckai\runs\<duckai-run>\CANDIDATE_LEDGER.jsonl `
  --output ..\website-lead-hunting-operations\runs\<benchmark-run>\HUMAN_REVIEW_BENCHMARK.csv
```

The output keeps human decisions and notes alongside provider status, durable score/verdict, raw model score/verdict, factor assessments, confidence, and review run ID. It intentionally excludes contact email and phone columns. A missing scorecard remains `not_assessed`; failed, retry-needed, uncertain, and blocked assessments are not converted into pass labels. The builder also writes a companion `.summary.json` with coverage counts, a High/Medium/Pass confusion table, per-class false positives and false negatives, and the rate of paired provider scores within 10 points.

Each CSV row gets a stable, stratified split: one row of every five within each human-decision class is assigned to `holdout`; the others are `tuning`. Use tuning rows to adjust factor definitions and scoring. Keep holdout rows untouched until evaluating a proposed change. The split is reproducible from the normalized website host, so rebuilding the benchmark preserves the assignment.

The current supplied high/medium review CSV contains 21 High and 42 Medium labels, with no Pass examples. Only 21 High sites currently match available provider assessments; the Medium rows are coverage gaps and must be freshly assessed. This data cannot validate Pass false positives or balanced thresholds. Add a human-reviewed Pass cohort, assess every labeled site with the factor scorecard, and compare per-class outcomes and ChatGPT/Duck.ai score deltas before changing weights or score bands. Keep benchmark outputs private because they contain business-level review decisions and notes.
