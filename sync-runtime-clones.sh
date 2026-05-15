#!/usr/bin/env bash
# Sync all clawpm runtime clones on this machine to F:/Git/clawpm main HEAD.
# Run this whenever F:/Git/clawpm has new local commits that the other clones should reflect.
#
# Each clone has F:/Git/clawpm registered as remote `local` — first-time setup is done.
# This script just fetches and resets.

set -euo pipefail

CANONICAL="/f/Git/clawpm"
CLONES=(
  "/f/Git/.agent-skills/skills/clawpm"
  "/f/Git/.q-skills/clawpm"
  "/c/Users/Martin Workspace/.claude/skills/clawpm"
)

canonical_head=$(git -C "$CANONICAL" rev-parse --short HEAD)
echo "Canonical HEAD: $canonical_head ($(git -C "$CANONICAL" log -1 --format='%s' HEAD))"
echo

for d in "${CLONES[@]}"; do
  if [ ! -d "$d/.git" ]; then
    echo "SKIP: $d — not a git clone"
    continue
  fi

  before=$(git -C "$d" rev-parse --short HEAD)
  if [ "$before" = "$canonical_head" ]; then
    echo "OK:    $d (already at $canonical_head)"
    continue
  fi

  # Ensure `local` remote points at canonical (idempotent)
  git -C "$d" remote remove local 2>/dev/null || true
  git -C "$d" remote add local "$CANONICAL"

  git -C "$d" fetch local main --quiet
  git -C "$d" reset --hard local/main --quiet

  after=$(git -C "$d" rev-parse --short HEAD)
  echo "SYNC:  $d ($before -> $after)"
done

# Mirror nested SKILL.md files to where Claude Code's skill loader discovers them.
# The repo layout nests skills under skills/<name>/SKILL.md; CC expects them at
# ~/.claude/skills/<name>/SKILL.md.
CC_CLONE="/c/Users/Martin Workspace/.claude/skills/clawpm"
CC_SKILLS_ROOT="/c/Users/Martin Workspace/.claude/skills"

mirror_skill () {
  local inner="$1"
  local outer="$2"
  if [ ! -f "$inner" ]; then
    return
  fi
  mkdir -p "$(dirname "$outer")"
  if ! cmp -s "$inner" "$outer" 2>/dev/null; then
    cp "$inner" "$outer"
    echo "MIRROR: $outer (refreshed for CC skill loader)"
  fi
}

mirror_skill "$CC_CLONE/skills/clawpm/SKILL.md" "$CC_CLONE/SKILL.md"
mirror_skill "$CC_CLONE/skills/clawpm-cowork/SKILL.md" "$CC_SKILLS_ROOT/clawpm-cowork/SKILL.md"

echo
echo "Done. Reminder: \`uv tool install -e F:/Git/clawpm\` is editable — CLI changes pick up automatically without reinstall."
