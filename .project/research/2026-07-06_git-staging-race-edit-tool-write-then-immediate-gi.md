---
created: '2026-07-06'
id: clawpm-research-git-staging-race-edit-tool-write-then-immediate-gi
status: open
type: investigation
---
# Git staging race: Edit-tool-write then immediate git mv/commit can capture stale content

## Question

Workaround pattern from a sysops session: verify staged content via git show :path before committing, not a working-tree read, after editing a file and immediately git mv/commit-ing it in the same turn

## Summary

Plausible, actionable workaround; root-cause attribution to "the Edit tool's write not settling" is not proven and probably wrong in its specific mechanism, but the mitigation is sound regardless of true cause. Reported by a sysops session: editing a file then immediately `git mv`/`git add`/committing it in the same turn occasionally staged the PRE-edit content, even though a `cat`/`tail` read of the working tree immediately after showed the new content correctly. Happened 3x with the identical shape (edit a clawpm task file → git mv to done/ → commit → later discover via `git show HEAD:<path>` the committed blob still had the old text).

## Findings

- Modern file-write tools (Edit/Write) generally complete synchronously — a genuine "write not yet visible to the next process" race is unlikely to be the true mechanism on a local NTFS volume.
- More probable root causes, none confirmed: (a) Windows-specific I/O latency — antivirus real-time scanning or cloud-sync (OneDrive etc.) holding a lock/delaying visibility on the file briefly after write; (b) the classic "racy git" problem — git's index caches mtime+size to skip re-hashing unchanged files, and a write followed by a git operation within the same mtime-granularity window can (rarely, and mostly mitigated in modern git) cause a stale-content read; (c) some interaction specific to `git mv`'s implementation (physical move + re-add) under those conditions.
- This is NOT a clawpm-specific bug — it's a generic Edit-tool-then-Bash-git composition risk that could affect any file, any project, any tool session. Not filed as a clawpm task; captured here and in operator memory as a general operational discipline note.

## Conclusion

Adopt the mitigation regardless of true root cause: before committing anything just edited in the same turn, verify the STAGED content specifically (`git show :<path>`, not a working-tree `cat`/`tail`/Read), and re-`git add` if it's stale. Cheap, always-correct, and sidesteps needing to pin down the exact race mechanism.
