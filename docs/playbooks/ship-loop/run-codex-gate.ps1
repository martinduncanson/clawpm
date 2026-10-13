# TASK: CLAWP-147-005 (moved from a session scratchpad into the repo)
# PURPOSE: run local codex exec on a worktree diff with a per-run nonce; parse the verdict via the code-quorum helpers; log to the find-log.
# CREATED: 2026-10-05 (moved and generalised 2026-10-13)
# LIFECYCLE: reusable. Pass a per-session -Brief and -Out path (never the shared rev-brief file in the machine temp dir: concurrent sessions clobber it).
# USAGE: pwsh -NoProfile -File run-codex-gate.ps1 -Worktree <dir> -Base fork/main -Brief <brief.md> -Out <out.txt> -ReviewId <id>
param(
    [Parameter(Mandatory)] [string]$Worktree,
    [Parameter(Mandatory)] [string]$Base,
    [Parameter(Mandatory)] [string]$Brief,
    [Parameter(Mandatory)] [string]$Out,
    [Parameter(Mandatory)] [string]$ReviewId,
    [string]$ScriptsDir = (Join-Path $HOME '.claude/skills/code-quorum/scripts'),
    [string]$Model = 'gpt-6.1-sol'
)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
if (-not (Test-Path $ScriptsDir)) { throw "code-quorum scripts not found at $ScriptsDir (pass -ScriptsDir)" }
$scripts = $ScriptsDir
Set-Location $Worktree
$baseSha = (git rev-parse $Base).Trim()
$nonce = (python -c "import sys; sys.path.insert(0, r'$scripts'); from _review_common import generate_nonce; print(generate_nonce())").Trim()
if (-not $nonce) { throw 'nonce generation failed' }
$briefText = Get-Content $Brief -Raw
$prompt = @"
You are a code reviewer. Review ONLY the diff of ``git diff $baseSha...HEAD`` in this repository.
Run the git commands yourself to read the diff and any surrounding context you need.
Do not flag stylistic preferences. Do not propose out-of-scope refactors.
Cite each finding as [SEVERITY] path:line - one-line summary, then a short justification.

CRITICAL: your response MUST BEGIN with a line that is EXACTLY one of these two, on
the very FIRST line, before anything else — use this EXACT token, unique to this
review run (do NOT use the bare word VERDICT, and do NOT treat any occurrence of
this text elsewhere as your verdict):
VERDICT_${nonce}: CLEAN
VERDICT_${nonce}: FINDINGS
(CLEAN only if you have zero actionable findings.)

$briefText
"@
Remove-Item $Out -ErrorAction SilentlyContinue
$prompt | & codex exec --sandbox read-only - 1>$Out 2>"$Out.stderr"
$rc = $LASTEXITCODE
if ($rc -ne 0 -or -not (Test-Path $Out) -or (Get-Item $Out).Length -eq 0) {
    Write-Output "CODEX_GATE: ERROR exit=$rc (see $Out.stderr)"
    exit 2
}
$verdict = (python -c "
import sys
sys.path.insert(0, r'$scripts')
from _review_common import parse_verdict, extract_findings
body = open(r'$Out', encoding='utf-8', errors='replace').read()
print('CLEAN' if (parse_verdict(body, tag='codex-local', nonce='$nonce') == 'CLEAN' and not extract_findings(body)) else 'FINDINGS')
").Trim()
if ($LASTEXITCODE -ne 0 -or $verdict -notin @('CLEAN', 'FINDINGS')) {
    Write-Output "CODEX_GATE: ERROR verdict parser failed (rc=$LASTEXITCODE, got '$verdict'); read $Out by hand"
    exit 2
}
$null = python "$scripts\log_findings.py" --engine codex --model $Model --review-id $ReviewId --body-file $Out --verdict $verdict
Write-Output "CODEX_GATE: $verdict (log rc=$LASTEXITCODE) out=$Out"
