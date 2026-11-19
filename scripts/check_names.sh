#!/usr/bin/env bash
# Fail if any tracked (or about-to-be-tracked) file matches a pattern from the
# local, gitignored .names-denylist (one Perl-compatible regex per line,
# matched case-insensitively). The list stays out of the repo on purpose.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
denylist=.names-denylist

if [[ ! -f "$denylist" ]]; then
  echo "warning: $denylist not found, skipping the name check" >&2
  exit 0
fi

# --untracked also covers new files that are not ignored yet; ignored files
# (including the denylist itself) are skipped.
if git grep -n -I -i -P --untracked -f "$denylist"; then
  echo "error: denylisted names found in the files above" >&2
  exit 1
fi
echo "check-names: ok"
