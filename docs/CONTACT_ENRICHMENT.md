# Contact detail collection

The initial Python site audit collects publicly displayed North American phone numbers and email addresses from homepage text, `tel:`/`mailto:` links, and supported structured data. If the homepage lacks a contact form, email, or phone number, it follows up to two linked same-site Contact/About pages. It records the page URL for each selected value and reports `complete`, `partial`, or `not_found`. It does not submit forms or infer missing contact details.

The private run export `runs/<run_key>/HIGH_MEDIUM_CANDIDATES.csv` includes `email`, `phone`, `contact_status`, `email_source_url`, and `phone_source_url`. Those values are committed with the full candidate ledger and CSV to the private operations/results repository. Duck.ai receives screenshots and assessment evidence only; email and phone values are not added to the visual-review prompt.

Because GitHub Actions artifacts belong to the public repository, the lead-hunt workflow first commits the unredacted run to the private repository, then removes email and phone values from the public backup artifact's candidate ledger and CSV. The artifact can still contain screenshots where contact details are visibly rendered. Actions logs, screenshots, and other run details should therefore be treated as public.

Blank CSV contact fields mean the bounded lookup did not find a public value on inspected pages. The source URLs document where selected values were found; they do not establish that no other contact details exist elsewhere on the site.
