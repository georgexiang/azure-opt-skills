#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source_dir="$root/azure-opt-skills/azure-ops"
target_dir="$root/deployment/sandbox/skills/azure-ops"
stage_dir="$(mktemp -d "$root/deployment/sandbox/skills/.azure-ops.XXXXXX")"
backup_dir="$root/deployment/sandbox/skills/.azure-ops.backup"

cleanup() {
	if [[ ! -e "$target_dir" && -e "$backup_dir" ]]; then
		mv "$backup_dir" "$target_dir"
	fi
	rm -rf "$stage_dir"
}
trap cleanup EXIT

test -f "$source_dir/SKILL.md"
cp -a "$source_dir/." "$stage_dir/"
find "$stage_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$stage_dir" -type f -name '*.py[co]' -delete
chmod 0755 "$stage_dir/scripts/az_guard.py"

rm -rf "$backup_dir"
if [[ -e "$target_dir" ]]; then
	mv "$target_dir" "$backup_dir"
fi
if mv "$stage_dir" "$target_dir"; then
	rm -rf "$backup_dir"
else
	if [[ -e "$backup_dir" ]]; then
		mv "$backup_dir" "$target_dir"
	fi
	exit 1
fi
