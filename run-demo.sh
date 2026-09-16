#!/usr/bin/env bash
# Run the safe, offline demonstration over synthetic replay records.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_cmd="${MIDE_PYTHON:-$repo_dir/artifact/mide/.venv/bin/python}"
data_file="$repo_dir/artifact/data/demo/demo.example-replay-records.jsonl"
out_dir="$repo_dir/results/demo"

if [[ "$python_cmd" == */* ]]; then
  python_available="$(test -x "$python_cmd" && echo yes || true)"
else
  python_available="$(command -v "$python_cmd" >/dev/null 2>&1 && echo yes || true)"
fi
if [[ "$python_available" != yes ]]; then
  echo "Missing MIDE environment. Run ./install.sh first." >&2
  exit 1
fi

rm -rf "$out_dir"
mkdir -p "$out_dir"
export MPLCONFIGDIR="${MIDE_MPLCONFIGDIR:-$out_dir/.matplotlib}"

"$python_cmd" "$repo_dir/artifact/analysis/prevalence_analysis.py" "$data_file" --out-dir "$out_dir/prevalence" --no-charts --bootstrap-samples 100
"$python_cmd" "$repo_dir/artifact/analysis/conversion_replay_failure_analysis.py" "$data_file" --out-dir "$out_dir/conversion" --no-charts
"$python_cmd" "$repo_dir/artifact/analysis/endpoint_taxonomy_analysis.py" analyze "$data_file" --out-dir "$out_dir/taxonomy" --no-charts
"$python_cmd" "$repo_dir/artifact/analysis/response_validation_platform.py" prepare "$data_file" --out-dir "$out_dir/validation" --classes A D
"$python_cmd" "$repo_dir/artifact/analysis/csrf_token_replay_check.py" "$data_file" --out-dir "$out_dir/csrf" --test-invalid-post-token
"$python_cmd" "$repo_dir/artifact/analysis/wcd_confirmed_test.py" "$data_file" --out-dir "$out_dir/wcd" --dry-run

echo "Offline demonstration complete. Results are in $out_dir"
