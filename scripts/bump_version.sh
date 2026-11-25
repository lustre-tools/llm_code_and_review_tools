#!/usr/bin/env bash
# Set the version of every tool and commit it: make bump VERSION=0.7.0
set -euo pipefail

new="${1:-}"
cd "$(git rev-parse --show-toplevel)"

if ! [[ "$new" =~ ^[0-9]+\.[0-9]+\.0$ ]]; then
	echo "usage: make bump VERSION=X.Y.0 (the last number counts commits)" >&2
	exit 2
fi

files=()
for file in */pyproject.toml; do  # patch_shepherd's carries no version
	if grep -q '^version = ' "$file"; then files+=("$file"); fi
done
version_of() { sed -n 's/^version = "\(.*\)"/\1/p' "$1" | head -1; }

if ! git diff --cached --quiet || ! git diff --quiet -- "${files[@]}"; then
	echo "staged changes, or edits to a pyproject.toml: commit them first" >&2
	exit 1
fi

current=$(version_of llm_tool_common/pyproject.toml)
newer=$(printf '%s\n%s\n' "$current" "$new" | sort -t. -k1,1n -k2,2n | tail -1)
if [[ "$new" == "$current" || "$newer" != "$new" ]]; then
	echo "$new is not newer than $current" >&2
	exit 1
fi

for file in "${files[@]}"; do
	# Not sed: "0,/re/", the first line only, is a GNU address.
	awk -v new="version = \"$new\"" \
		'!done && /^version = / { print new; done = 1; next } { print }' \
		"$file" > "$file.tmp"
	# cat, not mv: keep the file's mode.
	cat "$file.tmp" > "$file"
	rm -f "$file.tmp"
done

git commit -q -m "Version $new" -- "${files[@]}"
echo "$current -> $new, committed (not pushed)"
