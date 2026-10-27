# 打包 PawBot 插件 -> 上传阿里云用的 tar.gz（Windows PowerShell 版）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\pack-server.ps1
#       # 默认：不含任何运行时数据（安全，推荐首次部署）
#   powershell -ExecutionPolicy Bypass -File scripts\pack-server.ps1 -IncludeSecrets
#       # 含 app/data/config.json + persona.md，以及 secrets/（DSH 凭证、decider key）
#       # 不含记忆/会话/表情包；适合「服务器已有记忆、只要人设和 key」的场景
#   powershell -ExecutionPolicy Bypass -File scripts\pack-server.ps1 -IncludeAllData
#       # 含整个 app/data（人设+记忆+config+key+表情包索引…），包会很大且敏感
#
# 产物：pawbot-dsh-<版本>-<时间>[-full|-alldata].tar.gz + .sha256
# 安全：含 key 的包请用 scp/内网 OSS 传输，服务器上 chmod 600，传完删掉本地与服务器上的包。
param(
    [string]$OutDir = "",
    [switch]$IncludeSecrets,
    [switch]$IncludeAllData
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pkg = Join-Path $root "package.json"
$version = "1.0.0"
try { $version = (Get-Content $pkg -Raw -Encoding UTF8 | ConvertFrom-Json).version } catch {}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$suffix = if ($IncludeAllData) { "-alldata" } elseif ($IncludeSecrets) { "-full" } else { "" }
$name = "pawbot-dsh-$version-$stamp$suffix.tar.gz"
if (-not $OutDir) { $OutDir = Join-Path $root "dist" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$outPath = Join-Path $OutDir $name

$stageRoot = Join-Path $env:TEMP "pawbot-pack-$stamp"
$stage = Join-Path $stageRoot "pawbot-dsh"
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$xd = @(".git", "node_modules", "dist", "__pycache__", ".venv")
$xf = @("*.pyc", "*.log")
if (-not $IncludeAllData) {
    $xd += "app\data"
}

# 复制代码树（robocopy 返回码 0~7 都算正常）
$xdPaths = @($xd | ForEach-Object { Join-Path $root $_ })
$rc = robocopy $root $stage /E /XD $xdPaths /XF $xf /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) { throw "robocopy 失败（exit $LASTEXITCODE）" }

# 选择性数据：config.json（完整设置）+ persona.md；不含记忆/会话
if ($IncludeSecrets -and -not $IncludeAllData) {
    $dataDir = Join-Path $stage "app\data"
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    foreach ($f in @("config.json", "persona.md", "hobbies.json")) {
        $src = Join-Path $root "app\data\$f"
        if (Test-Path $src) { Copy-Item $src (Join-Path $dataDir $f) -Force }
    }

    # secrets/：DSH 凭证（完整 + PawBot 子集）+ decider key
    $secrets = Join-Path $stage "secrets"
    New-Item -ItemType Directory -Force -Path $secrets | Out-Null
    $cred = Join-Path $env:USERPROFILE ".dsh\.credentials.yaml"
    if (Test-Path $cred) {
        Copy-Item $cred (Join-Path $secrets "credentials.full.yaml") -Force
        $want = "TOGEMINI_API_KEY|PAWBOT_DECIDER_KEY|OPENROUTER_DECISION_API_KEY|OPENROUTER_API_KEY|BILI_SESSDATA|PAWBOT_.*_KEY"
        $lines = Get-Content $cred -Encoding UTF8 | Where-Object { $_ -match "^\s*($want)\s*:" }
        if ($lines) {
            $lines | Set-Content (Join-Path $secrets "credentials.pawbot.yaml") -Encoding UTF8
        }
    }
    $deciderKey = Join-Path $env:USERPROFILE ".pawbot\decider.key"
    if (Test-Path $deciderKey) {
        Copy-Item $deciderKey (Join-Path $secrets "decider.key") -Force
    }

    $readme = @(
        "本目录是 PawBot 在服务器上要用的 Key（明文，传完请删包）。",
        "",
        "app/data/config.json 已包含完整配置（含所有选项）；如果里面 api_key 为空，",
        "说明本机是用 DSH 凭证/环境变量供 key 的，按下面放：",
        "",
        "1) DSH 凭证（vision / sticker / 判官 / decider 等）：",
        "   mkdir -p ~/.dsh",
        "   # 全新服务器：直接当凭证文件用",
        "   cp secrets/credentials.full.yaml ~/.dsh/.credentials.yaml",
        "   # 已有凭证：把需要的键追加进去（TOGEMINI_API_KEY 等）",
        "   cat secrets/credentials.pawbot.yaml >> ~/.dsh/.credentials.yaml",
        "   chmod 600 ~/.dsh/.credentials.yaml",
        "",
        "2) 决策模型 key（心情矩阵）：",
        "   mkdir -p ~/.pawbot && cp secrets/decider.key ~/.pawbot/decider.key",
        "   chmod 600 ~/.pawbot/decider.key",
        "",
        "3) 配置文件权限：",
        "   chmod 600 app/data/config.json app/data/persona.md",
        "",
        "4) 或者用环境变量（systemd Environment=...）代替凭证文件：",
        "   TOGEMINI_API_KEY=...  PAWBOT_DECIDER_KEY=...",
        "",
        "安全提醒：这些是明文 key；只走 scp/内网，不要把 tar.gz 放公共 OSS 桶，传完删掉。"
    )
    $readme | Set-Content (Join-Path $secrets "README.txt") -Encoding UTF8
}

# 打 tar.gz（顶层目录 pawbot-dsh/）
Push-Location $stageRoot
try {
    & tar.exe -czf $outPath "pawbot-dsh"
    if ($LASTEXITCODE -ne 0) { throw "tar 失败（exit $LASTEXITCODE）" }
} finally {
    Pop-Location
    Remove-Item $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$hash = (Get-FileHash $outPath -Algorithm SHA256).Hash.ToLower()
"$hash  $name" | Set-Content -Path "$outPath.sha256" -Encoding ASCII
Write-Host "OK -> $outPath"
Write-Host ("大小: {0:N1} MB" -f ((Get-Item $outPath).Length / 1MB))
if ($IncludeSecrets -or $IncludeAllData) {
    Write-Host "!! 这个包含明文 config/key（可能还有记忆），只走 scp/内网，传完立刻删除本地副本。" -ForegroundColor Yellow
}
Get-Content "$outPath.sha256"
