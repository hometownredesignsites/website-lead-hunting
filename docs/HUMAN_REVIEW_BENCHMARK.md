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

The output keeps human decisions and notes alongside provider status, durable score/verdict, raw model score/verdict, confidence, and review run ID. It intentionally excludes contact email and phone columns. A missing scorecard remains `not_assessed`; failed, retry-needed, uncertain, and blocked assessments are not converted into pass labels.

The current supplied high/medium review CSV contains 21 High and 42 Medium labels, with no Pass examples. This is useful for measuring missed High/Medium opportunities, but not for validating false-positive rates or selecting balanced thresholds. Add a separate human-reviewed Pass cohort before changing score bands. Keep benchmark outputs private because they contain business-level review decisions and notes.
