# Included data

`demo/demo.example-replay-records.jsonl` is a 30-record, synthetic JSONL fixture.
It contains no requests to real services, credentials, or personal data. It follows
MIDE's replay-record schema:
each line contains the captured POST metadata and response, zero or more converted
GET candidates, and the no-query GET control response for each candidate.

Coverage is deliberately balanced across the test cases needed by the runnable
claims: 10 class-A, 8 class-B, 6 class-C, and 6 class-D records; 10 records each for
form-urlencoded, JSON, and multipart bodies; six JSON records with both PHP-style
and repeated-key candidates; and both HTML and JSON responses in equivalent and
non-equivalent cases. Six class-A records contain synthetic CSRF-token carriers.
Their recorded offline outcomes are in `demo/csrf-token-replay-summary.csv`, with
examples of POST rejection and GET acceptance. `demo/wcd-recorded-outcomes.csv`
contains three synthetic cacheable and three non-cacheable recorded examples. These
two CSV files are recorded fixtures, not live network experiments.

`demo/generate_fixture.py` deterministically regenerates the replay JSONL fixture.

The raw data from the Tranco measurement is intentionally not included in this
repository: it contains live target URLs and captured request/response material that
may change over time or contain sensitive values.
