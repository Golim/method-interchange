Expected validation
-------------------
`run.sh` succeeds and writes `results/claim1/baseline-classes.txt`. The expected
counts are 10 A, 8 B, 6 C, and 6 D records. It also verifies that the content-type
table contains 10 form-urlencoded, 10 JSON, and 10 multipart records.
