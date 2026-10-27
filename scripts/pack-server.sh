#!/usr/bin/env bash
# 打包 PawBot 插件 → 上传阿里云用的 tar.gz（不含运行时数据/虚拟环境/依赖）。
#
# 用法（Git Bash / WSL / Linux）：
#   bash scripts/pack-server.sh                # 输出到 ./dist/
#   bash scripts/pack-server.sh /tmp/out       # 指定输出目录
#
# 产物：pawbot-dsh-<版本>-<时间>.tar.gz + .sha256
# 不包含：app/data（人设/记忆/config/key）、app/.venv、node_modules、.git、备份、日志
# 说明：运行时数据到服务器后用「云同步 pull」或手工 scp 迁移（见 DEPLOY.md §8）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
VERSION="$(node -e "console.log(require('$ROOT/package.json').version)" 2>/dev/null || echo 1.0.0)"
STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="pawbot-dsh-${VERSION}-${STAMP}.tar.gz"

mkdir -p "$OUT"
cd "$(dirname "$ROOT")"

tar -czf "$OUT/$NAME" \
  --exclude='pawbot-dsh/.git' \
  --exclude='pawbot-dsh/node_modules' \
  --exclude='pawbot-dsh/app/.venv' \
  --exclude='pawbot-dsh/app/data' \
  --exclude='pawbot-dsh/app/data/backups' \
  --exclude='pawbot-dsh/app/__pycache__' \
  --exclude='pawbot-dsh/app/**/__pycache__' \
  --exclude='pawbot-dsh/dist' \
  --exclude='pawbot-dsh/*.log' \
  --exclude='pawbot-dsh/**/*.pyc' \
  "$(basename "$ROOT")"

cd "$OUT"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$NAME" > "$NAME.sha256"
  cat "$NAME.sha256"
else
  shasum -a 256 "$NAME" > "$NAME.sha256"
  cat "$NAME.sha256"
fi
ls -lh "$NAME"
echo "OK -> $OUT/$NAME"
