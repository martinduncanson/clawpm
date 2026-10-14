# ClawPM reference: troubleshooting

Moved verbatim from `SKILL.md`. Covers: Windows argv glob expansion history, doctor/troubleshooting, Windows CLI caveats.

## Troubleshooting

```bash
clawpm doctor              # Check for issues
clawpm setup --check       # Verify installation
clawpm log tail            # See recent activity
```

**`add_failed` after `project init`?** Check `.project/settings.toml` — `repo_path` must use forward slashes on Windows (`F:/Git/...` not `F:\Git\...`). The CLI now warns when this is suspected, but old settings.toml files written by earlier versions may still be broken.

**Windows CLI caveats (observed 2026-07-05):**
- Fixed by CLAWP-109 (PR #77): Click's Windows argv glob expansion (not PowerShell, not the launcher) used to turn `--scope "scripts/**"` into extra positional arguments ("Got unexpected extra arguments") or silently drop the argument. Current installs pass arguments verbatim. On an older install, use `--predict-scope-file`/`--scope-file`/`--out-of-scope-file` (patterns read from a file, one per line, never touching argv; CLAWP-060) and verify with `clawpm tasks list` after a batch.
- `--predict-duration` (and any other duration-parsing flag) accepts combined units, e.g. `2h30m` -> 150 minutes, in addition to a single unit (`150m`, `2h`) (CLAWP-096).
- If multiple `clawpm.exe` shims are on PATH, a stale one can shadow the working install (`ModuleNotFoundError: No module named 'clawpm'`). Check with `where.exe clawpm` and invoke the shim next to the Python install that has clawpm importable.
- **`tasks edit` merges predictions field-by-field (CLAWP-108).** Only the prediction flags you pass are overwritten; every other existing prediction field, including `filled_by`, is kept. A repeatable list flag (`--predict-scope`, `--success-criteria`, ...) replaces just that one list. `--predicted-by` exists on `tasks add` but not `tasks edit`; an edit never changes `filled_by`. Caveat: an older installed clawpm (before this fix) still replaces the whole block, so re-pass all prediction flags if `clawpm --version` predates it.
