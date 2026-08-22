#!/usr/bin/env bash
# Seeds ./dashboards, ./data, and ./settings from templates/ so a fresh
# clone has something to run on the first `docker compose up`. Never
# overwrites a file that's already there — safe to re-run at any time,
# including after you've started customizing your own dashboards/settings.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

seed_dir() {
    local src_dir="$1"
    local dest_dir="$2"

    if [ ! -d "$src_dir" ]; then
        return
    fi

    mkdir -p "$dest_dir"

    find "$src_dir" -type f | while IFS= read -r src_file; do
        rel_path="${src_file#"$src_dir"/}"
        dest_file="$dest_dir/$rel_path"

        if [ -e "$dest_file" ]; then
            echo "skip   $dest_file (already exists)"
        else
            mkdir -p "$(dirname "$dest_file")"
            cp "$src_file" "$dest_file"
            echo "copied $dest_file"
        fi
    done
}

echo "Seeding onboarding files from templates/ ..."
seed_dir "templates/dashboards" "dashboards"
seed_dir "templates/data" "data"
seed_dir "templates/settings" "settings"

echo
echo "Done. Nothing here is overwritten on re-run — delete a file first if you want it reseeded from templates/."
echo "Next: docker compose up --build"
