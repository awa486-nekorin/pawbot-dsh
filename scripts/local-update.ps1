# local-update.ps1 - apply a PawBot update on THIS machine without GitHub.
#
# Why this works without uploading anything:
#   The DSH profile installs this repo as a symlink
#   (link:D:/WorkBuddy/pawbot-dsh), so the files in this repo ARE the plugin.
#   There is nothing to copy: restart DSH + PawBot and the new code is live.
#
# What this script does:
#   1. Backs up app/data (memory / persona / config / memes) OUTSIDE the repo.
#   2. Syntax-checks the changed JavaScript and Python.
#   3. Runs the fast offline test subset (no network, no QQ, no DSH actions).
#   4. Prints the restart steps.
#
# What it never does: write to app/data, call git, or touch the network.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\local-update.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\local-update.ps1 -SkipTests
#   powershell -ExecutionPolicy Bypass -File scripts\local-update.ps1 -BackupRoot D:\pawbot-backups
#
# PS 5.1 note: keep this file ASCII-only; it is written as UTF-8 without BOM.

param(
    [switch]$SkipTests,
    [string]$BackupRoot = '',
    [string]$PythonExe = 'py'
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "   OK  $Text" -ForegroundColor Green
}

function Write-Warn2([string]$Text) {
    Write-Host "   !!  $Text" -ForegroundColor Yellow
}

$Root = Resolve-Path (Join-Path $PSScriptRoot '..')
$Root = $Root.Path
$Data = Join-Path $Root 'app\data'
$App = Join-Path $Root 'app'

Write-Host "PawBot local update (no GitHub)" -ForegroundColor White
Write-Host "repo : $Root"

if (-not (Test-Path $Data)) { throw "app/data not found: $Data" }

# ---------------------------------------------------------------- 1. backup
Write-Step 'Backing up app/data (memory, persona, config)'
if (-not $BackupRoot) {
    $BackupRoot = Join-Path (Split-Path -Parent $Root) 'pawbot-data-backup'
}
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Dest = Join-Path $BackupRoot $Stamp
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Copy-Item -Path (Join-Path $Data '*') -Destination $Dest -Recurse -Force

$srcCount = (Get-ChildItem -Path $Data -Recurse -File -Force | Measure-Object).Count
$dstCount = (Get-ChildItem -Path $Dest -Recurse -File -Force | Measure-Object).Count
if ($dstCount -lt $srcCount) {
    throw "backup incomplete: copied $dstCount of $srcCount files"
}
Write-Ok "backup at $Dest ($dstCount files)"

# ---------------------------------------------------------------- 2. syntax
Write-Step 'Syntax checks'

$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    foreach ($js in @('index.js', 'client.js', 'lib\dsh-tool-bridge.js')) {
        $p = Join-Path $Root $js
        if (-not (Test-Path $p)) { continue }
        & node --check $p
        if ($LASTEXITCODE -ne 0) { throw "node --check failed: $js" }
        Write-Ok "node --check $js"
    }
}
else {
    Write-Warn2 'node not found, skipped JavaScript syntax check'
}

$py = Get-Command $PythonExe -ErrorAction SilentlyContinue
if ($py) {
    Push-Location $App
    try {
        & $PythonExe -c "import ast, pathlib, sys; files=[p for p in pathlib.Path('.').rglob('*.py') if '__pycache__' not in str(p) and '.venv' not in str(p)]; bad=[]; [bad.append(str(p)) for p in files if not ast.parse(p.read_text(encoding='utf-8'), str(p))]; print('python parsed %d files' % len(files)); sys.exit(1 if bad else 0)"
        if ($LASTEXITCODE -ne 0) { throw 'python AST parse failed' }
        Write-Ok 'python AST parse'
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Warn2 "$PythonExe not found, skipped Python syntax check"
}

# ---------------------------------------------------------------- 3. tests
if (-not $SkipTests) {
    Write-Step 'Fast offline tests (no network, no QQ, no DSH actions)'

    if ($node) {
        Push-Location $Root
        try {
            & node test/client-meta-check.mjs
            if ($LASTEXITCODE -ne 0) { throw 'client-meta-check failed' }
            Write-Ok 'client-meta-check'
        }
        finally {
            Pop-Location
        }
    }

    if ($py) {
        Push-Location $App
        try {
            & $PythonExe -m unittest tools.test_dsh_bridge tools.test_qzone_security tools.test_bili_post -q
            if ($LASTEXITCODE -ne 0) { throw 'focused python tests failed' }
            Write-Ok 'focused python tests'
        }
        finally {
            Pop-Location
        }
    }
}
else {
    Write-Step 'Tests skipped (-SkipTests)'
}

# ---------------------------------------------------------------- 4. next
Write-Step 'Done. Restart to make the new code live'
Write-Host '   1) Restart DSH (the bundle layer: index.js / lib changed)'
Write-Host '   2) DSH settings -> PawBot -> restart PawBot (python side changed)'
Write-Host '   3) Check: settings panel status tab shows PawBot running'
Write-Host ''
Write-Host "   app/data was NOT modified. Rollback of data: copy files back from $Dest"
Write-Host "   Rollback of code: git -C $Root checkout -- .   (only if you want the old code)"
Write-Host ''
