#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_cmd="${MIDE_PYTHON:-$repo_dir/artifact/mide/.venv/bin/python}"
config="$repo_dir/artifact/data/mide-fixture/mide-fixture.yaml"
target="http://mide-fixture"
out_dir="$repo_dir/results/claim4"

if [[ "$python_cmd" == */* ]]; then test -x "$python_cmd"; else command -v "$python_cmd" >/dev/null; fi \
  || { echo "Run ./install.sh first, or use docker compose run --rm artifact ./claims/claim4/run.sh." >&2; exit 1; }

rm -rf "$out_dir"
mkdir -p "$out_dir"
"$python_cmd" "$repo_dir/artifact/mide/main.py" crawl "$target" \
  --config "$config" --max-pages 20 --max-depth 4 --timeout 6 --quiet
"$python_cmd" "$repo_dir/artifact/mide/main.py" post2get "$target" \
  --config "$config" --output-dir "$out_dir" --quiet

session_dir="$(find "$out_dir/mide-fixture" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)"
if [[ -z "$session_dir" ]]; then
  echo "Crawl did not produce a MIDE session under $out_dir/mide-fixture." >&2
  exit 1
fi
"$python_cmd" "$repo_dir/artifact/mide/main.py" analyze "$session_dir" \
  --config "$config" --quiet
"$python_cmd" "$repo_dir/claims/claim4/validate.py" "$session_dir"

echo "Claim 4 passed: crawl, POST capture, conversion, replay, and analysis ran against the local Flask target."
echo "Results: $session_dir"
