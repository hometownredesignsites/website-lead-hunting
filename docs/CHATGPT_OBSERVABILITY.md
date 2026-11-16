# ChatGPT lead-hunting dashboard

The public `website-lead-hunting` Actions workflow builds this private report on the `git-chatgpt` branch of `website-lead-hunting-operations` at `observability/chatgpt/index.html`. It uses the existing `PRIVATE_RESULTS_TOKEN` secret for the matching private repository only.

The workflow refreshes hourly, on manual dispatch, after a campaign/config CSV update, and after the listed lead-hunt workflows complete. The output includes a self-contained HTML report, a JSON snapshot, and CSV exports for category-city progress and lead-hunt batch timing.

The campaign checkpoint supplies the category rows and city snapshot. Category rows retain their source index, including duplicate labels. A query with a final ledger status counts as processed; failed queries remain separately visible with their reason. Batch durations use query-ledger timestamps, which remain available after public Actions artifacts expire.

Download `observability/chatgpt/index.html` from the private operations branch and open it locally. GitHub's file viewer displays HTML source instead of running the report as a private website.
