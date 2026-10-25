# Collect plugin changes into a zip (works on Windows PowerShell 5.1+).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File collect-server-changes.ps1 [-PluginDir <path>]
# Output: %TEMP%\pawbot-changes-<timestamp>.zip  (excludes node_modules / .venv / runtime data)
param([string]$PluginDir = "")

$ErrorActionPreference = "Stop"

function Find-PluginDir {
  $cands = Get-ChildItem (Join-Path $env:USERPROFILE ".dsh\profiles\*\node_modules\@local\pawbot") -Directory -ErrorAction SilentlyContinue
  if (-not $cands) {
    $cands = Get-ChildItem (Join-Path $env:USERPROFILE ".dsh\profiles\*\node_modules\@local") -Directory -Filter "pawbot*" -ErrorAction SilentlyContinue
  }
  if ($cands) { return ($cands | Select-Object -First 1).FullName }
  return ""
}

$dir = if ($PluginDir) { $PluginDir } else { Find-PluginDir }
if (-not $dir -or -not (Test-Path (Join-Path $dir "package.json"))) {
  Write-Host "[FAIL] plugin dir not found (no package.json). Pass -PluginDir <path>." -ForegroundColor Red
  exit 1
}
Write-Host "plugin dir: $dir"

$gitDir = Join-Path $dir ".git"
if (Test-Path $gitDir) {
  Write-Host ""
  Write-Host "=== git status --short ===" -ForegroundColor Cyan
  git -C $dir status --short
  Write-Host ""
  Write-Host "=== git diff --stat ===" -ForegroundColor Cyan
  git -C $dir diff --stat
} else {
  Write-Host "(no .git here, will package the whole code tree for diffing)" -ForegroundColor Yellow
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tmp = Join-Path $env:TEMP "pawbot-collect-$stamp"
$zip = Join-Path $env:TEMP "pawbot-changes-$stamp.zip"
$xd = @("node_modules", ".venv", "__pycache__", ".git", "backup", "image_cache", "stickers", "browser", "napcat", "logs")
$xf = @("*.pyc", "*.log", "*.bak", "*.before-*", "sessions.json", "entities.jsonl")

Write-Host ""
Write-Host "packing (excluding deps and runtime data) ..."
robocopy $dir $tmp /E /XD $xd /XF $xf /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
  Write-Host "[FAIL] robocopy exit code $LASTEXITCODE" -ForegroundColor Red
  exit 1
}

# personal data must not leave the server
foreach ($p in @("app\data\people", "app\data\diary")) {
  $full = Join-Path $tmp $p
  if (Test-Path $full) {
    Get-ChildItem $full -File -Filter "*.md" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  }
}
foreach ($f in @("app\data\memory\entities.jsonl", "app\data\sessions.json", "app\data\run.log")) {
  $full = Join-Path $tmp $f
  if (Test-Path $full) { Remove-Item $full -Force -ErrorAction SilentlyContinue }
}

Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zip -CompressionLevel Optimal -Force
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 2)
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash
Write-Host ""
Write-Host "[OK] output: $zip  ($mb MB)" -ForegroundColor Green
Write-Host "     SHA256: $sha"
Write-Host ""
Write-Host "Send this file path to Lead (do not unzip it)." -ForegroundColor Cyan
