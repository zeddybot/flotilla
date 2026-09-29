#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is required: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi

exec uv run --frozen --project "$repo_dir" python "$repo_dir/scripts/install_service.py" "$@"
