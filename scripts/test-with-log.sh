#!/usr/bin/env bash
set -euo pipefail

extract_stem() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            -*) continue ;;
        esac
        case "$arg" in
            tests/*|tests)
                local path="${arg%%::*}"
                path="${path%.py}"
                basename "$path"
                return 0
                ;;
        esac
    done
    echo "pytest"
}

cd "$(dirname "$0")/.."
stem=$(extract_stem "$@")
uv run python scripts/run_with_timing.py --label "$stem" -- uv run pytest "$@"
