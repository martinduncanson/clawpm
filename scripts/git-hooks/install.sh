#!/bin/sh
# Install clawpm's repo git hooks into .git/hooks. Idempotent; re-run anytime.
# Usage: sh scripts/git-hooks/install.sh
set -e
repo_root=$(git rev-parse --show-toplevel)
hooks_src="$repo_root/scripts/git-hooks"
hooks_dst="$repo_root/.git/hooks"
for hook in post-merge; do
  cp "$hooks_src/$hook" "$hooks_dst/$hook"
  chmod +x "$hooks_dst/$hook"
  echo "installed: .git/hooks/$hook"
done
