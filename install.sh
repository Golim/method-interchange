#!/usr/bin/env bash
# Install the pinned Python environment and the Chromium browser used by MIDE.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required, but python3 was not found." >&2
  exit 1
fi

python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required (found $python_version)." >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  uv_cmd="$(command -v uv)"
else
  echo "Installing uv in the current user's Python environment..."
  if ! python3 -m pip --version >/dev/null 2>&1; then
    echo "uv is not installed and python3-pip is unavailable. Install uv or python3-pip, then re-run this script." >&2
    exit 1
  fi
  python3 -m pip install --user uv
  uv_cmd="${HOME}/.local/bin/uv"
  if [[ ! -x "$uv_cmd" ]]; then
    uv_cmd="$(python3 -m site --user-base)/bin/uv"
  fi
fi

if [[ ! -x "$uv_cmd" ]] && ! command -v "$uv_cmd" >/dev/null 2>&1; then
  echo "uv installation failed or is not on PATH. Install uv and re-run this script." >&2
  exit 1
fi

"$uv_cmd" sync --project "$repo_dir/artifact/mide" --frozen --all-groups
if command -v apt-get >/dev/null 2>&1; then
  echo "Installing Chromium and its Ubuntu system dependencies (sudo may prompt)..."
  "$uv_cmd" run --project "$repo_dir/artifact/mide" python -m playwright install --with-deps chromium
else
  "$uv_cmd" run --project "$repo_dir/artifact/mide" python -m playwright install chromium
fi

echo "Installation complete. Run ./run-demo.sh to exercise the offline artifact."
