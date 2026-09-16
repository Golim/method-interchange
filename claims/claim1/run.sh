#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_cmd="${MIDE_PYTHON:-$repo_dir/artifact/mide/.venv/bin/python}"
data_file="$repo_dir/artifact/data/demo/demo.example-replay-records.jsonl"
out_dir="$repo_dir/results/claim1"

if [[ "$python_cmd" == */* ]]; then test -x "$python_cmd"; else command -v "$python_cmd" >/dev/null; fi \
  || { echo "Run ./install.sh first." >&2; exit 1; }
rm -rf "$out_dir"
mkdir -p "$out_dir"
export MPLCONFIGDIR="$out_dir/.matplotlib"

"$python_cmd" "$repo_dir/artifact/analysis/prevalence_analysis.py" "$data_file" \
  --out-dir "$out_dir" --no-charts --bootstrap-samples 100

grep -F "Strong interchangeability" "$out_dir/baseline-classes.txt" | grep -Eq '[[:space:]]10[[:space:]]*\|'
grep -F "Ambiguous" "$out_dir/baseline-classes.txt" | grep -Eq '[[:space:]]8[[:space:]]*\|'
grep -F "Baseline collision" "$out_dir/baseline-classes.txt" | grep -Eq '[[:space:]]6[[:space:]]*\|'
grep -F "Not interchangeable" "$out_dir/baseline-classes.txt" | grep -Eq '[[:space:]]6[[:space:]]*\|'
for content_type in form-urlencoded json multipart; do
  grep -F "$content_type" "$out_dir/content-type-breakdown.txt" | grep -Eq '[[:space:]]10[[:space:]]*\|'
done

echo "Claim 1 passed: A/B/C/D classes and all three body formats are covered."
