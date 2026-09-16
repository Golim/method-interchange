Expected validation
-------------------
`run.sh` sends no network requests. It writes a CSRF request plan containing the
synthetic `csrf_token` carrier, aggregates a recorded `POST rejects; GET accepts`
example, and writes a WCD dry-run report. The fixture has three recorded cacheable
and three non-cacheable WCD examples; the script validates both counts.
