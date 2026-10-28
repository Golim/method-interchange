#!/usr/bin/env python3
"""Check expected evidence in a completed local-fixture MIDE session."""

import json
import sys
from pathlib import Path

session = Path(sys.argv[1])
captured_path = session / "mide-fixture-captured-posts.jsonl"
replay_path = session / "mide-fixture-replay-records.jsonl"
analysis_path = session / "mide-fixture-analysis.json"

for path in (captured_path, replay_path, analysis_path):
    if not path.is_file():
        raise SystemExit(f"Missing pipeline output: {path}")

captured = [json.loads(line) for line in captured_path.read_text().splitlines() if line.strip()]
replayed = [json.loads(line) for line in replay_path.read_text().splitlines() if line.strip()]
analysis = json.loads(analysis_path.read_text())

expected = {"echo", "post-only", "different", "multipart", "json", "required"}
captured_cases = {Path(row["url"].split("?", 1)[0]).name for row in captured}
if not expected.issubset(captured_cases):
    raise SystemExit(f"Crawl did not capture every fixture endpoint: {sorted(expected - captured_cases)}")
if len(replayed) < len(expected):
    raise SystemExit(f"Expected at least {len(expected)} replay records, got {len(replayed)}")

per_case = {
    Path(row["url"].split("?", 1)[0]).name: row
    for row in analysis.get("per_record", [])
    if "/endpoint/" in row.get("url", "")
}
missing = expected - per_case.keys()
if missing:
    raise SystemExit(f"Analysis is missing fixture cases: {sorted(missing)}")

for case in ("echo", "multipart", "json", "required"):
    row = per_case[case]
    if row["route_acceptance"]["verdict"] != "accepted":
        raise SystemExit(f"Converted GET for {case} was not accepted")
    if row["source_equivalence"]["verdict"] != "equivalent":
        raise SystemExit(f"Converted GET for {case} did not match the POST response")

if per_case["post-only"]["route_acceptance"]["verdict"] != "rejected":
    raise SystemExit("POST-only endpoint was not classified as rejecting GET")
if per_case["different"]["route_acceptance"]["verdict"] != "accepted":
    raise SystemExit("Different-response endpoint should accept GET")
if per_case["different"]["source_equivalence"]["verdict"] != "not_equivalent":
    raise SystemExit("Different-response endpoint was not classified as non-equivalent")

print(f"Validated {len(captured)} captures, {len(replayed)} replay records, and all six analyzed cases.")
