#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_cmd="${MIDE_PYTHON:-$repo_dir/artifact/mide/.venv/bin/python}"
data_file="$repo_dir/artifact/data/demo/demo.example-replay-records.jsonl"
out_dir="$repo_dir/results/claim2"

if [[ "$python_cmd" == */* ]]; then test -x "$python_cmd"; else command -v "$python_cmd" >/dev/null; fi \
  || { echo "Run ./install.sh first." >&2; exit 1; }
rm -rf "$out_dir"
mkdir -p "$out_dir"
export MPLCONFIGDIR="$out_dir/.matplotlib"

"$python_cmd" "$repo_dir/artifact/analysis/conversion_replay_failure_analysis.py" "$data_file" --out-dir "$out_dir/conversion" --no-charts
"$python_cmd" "$repo_dir/artifact/analysis/endpoint_taxonomy_analysis.py" analyze "$data_file" --out-dir "$out_dir/taxonomy" --no-charts
"$python_cmd" "$repo_dir/artifact/analysis/response_validation_platform.py" prepare "$data_file" --out-dir "$out_dir/validation" --classes A D

test -s "$out_dir/conversion/summary.json"
test -s "$out_dir/taxonomy/summary.json"
test -s "$out_dir/validation/sample.json"
grep -F '"array_style_used":"php"' "$data_file" >/dev/null
grep -F '"array_style_used":"repeated"' "$data_file" >/dev/null
echo "Claim 2 passed: all offline evaluation outputs were generated."
