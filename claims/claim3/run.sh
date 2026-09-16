#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_cmd="${MIDE_PYTHON:-$repo_dir/artifact/mide/.venv/bin/python}"
data_file="$repo_dir/artifact/data/demo/demo.example-replay-records.jsonl"
out_dir="$repo_dir/results/claim3"

if [[ "$python_cmd" == */* ]]; then test -x "$python_cmd"; else command -v "$python_cmd" >/dev/null; fi \
  || { echo "Run ./install.sh first." >&2; exit 1; }
rm -rf "$out_dir"
mkdir -p "$out_dir"

"$python_cmd" "$repo_dir/artifact/analysis/csrf_token_replay_check.py" "$data_file" --out-dir "$out_dir/csrf" --test-invalid-post-token
"$python_cmd" "$repo_dir/artifact/analysis/csrf_token_replay_check.py" --results-csv "$repo_dir/artifact/data/demo/csrf-token-replay-summary.csv" --out-dir "$out_dir/csrf-recorded"
"$python_cmd" "$repo_dir/artifact/analysis/wcd_confirmed_test.py" "$data_file" --out-dir "$out_dir/wcd" --dry-run

grep -F "csrf_token" "$out_dir/csrf/token-replay-plan.csv" >/dev/null
grep -F "POST rejects; GET accepts" "$out_dir/csrf-recorded/csrf-token-replay-aggregate.txt" >/dev/null
grep -F "not_tested_safety" "$out_dir/wcd/wcd-output-classes.txt" >/dev/null
test "$(grep -c ',cacheable,' "$repo_dir/artifact/data/demo/wcd-recorded-outcomes.csv")" -eq 3
test "$(grep -c ',non_cacheable,' "$repo_dir/artifact/data/demo/wcd-recorded-outcomes.csv")" -eq 3
echo "Claim 3 passed: CSRF and WCD plans were generated without live requests."
