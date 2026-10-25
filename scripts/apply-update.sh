#!/usr/bin/env bash
# 在「连不上 GitHub 的服务器」上，用离线包更新插件。
# 用法：bash apply-update.sh <更新包.tgz> [插件目录]
#   <更新包.tgz> 由有网的机器执行下面命令生成：
#     git archive --format=tar.gz -o pawbot-dsh-update.tgz --prefix=pawbot-dsh/ \
#       $(git rev-parse HEAD) -- . ':(exclude)app/data'
# 特性：先整份备份、保留服务器自己的 app/data、失败可回滚。
set -euo pipefail

TGZ="${1:-}"
if [ -z "$TGZ" ] || [ ! -f "$TGZ" ]; then
  echo "用法: bash $0 <更新包.tgz> [插件目录]" >&2
  exit 1
fi

find_plugin_dir() {
  local cand
  for cand in "$HOME"/.dsh/profiles/*/node_modules/@local/pawbot; do
    [ -f "$cand/package.json" ] && { echo "$cand"; return 0; }
  done
  find "$HOME" -maxdepth 8 -type d -path '*@local/pawbot' 2>/dev/null | head -n1
}
PLUGIN_DIR="${2:-$(find_plugin_dir)}"
[ -n "${PLUGIN_DIR:-}" ] && [ -d "$PLUGIN_DIR" ] || { echo "✗ 找不到插件目录，请作第二個参数传入" >&2; exit 1; }
PLUGIN_DIR="$(cd "$PLUGIN_DIR" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$(dirname "$PLUGIN_DIR")/pawbot-backup-${STAMP}"
TMP="$(mktemp -d /tmp/pawbot-update.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "插件目录: $PLUGIN_DIR"
echo "更新包  : $TGZ"
echo "备份到  : $BACKUP"

# 1. 解包（包内顶层目录可能是 pawbot-dsh/ 或直接是文件）
tar -xzf "$TGZ" -C "$TMP"
SRC="$TMP"
if [ "$(ls -1 "$TMP" | wc -l)" = "1" ] && [ -d "$TMP/$(ls -1 "$TMP" | head -n1)" ]; then
  SRC="$TMP/$(ls -1 "$TMP" | head -n1)"
fi
[ -f "$SRC/package.json" ] || { echo "✗ 包里没找到 package.json，可能不是本插件的包" >&2; exit 1; }

# 2. 整份备份（不含 node_modules/.venv，省空间）
echo "→ 备份中…"
mkdir -p "$BACKUP"
tar -C "$(dirname "$PLUGIN_DIR")" -cf - \
  --exclude='node_modules' --exclude='.venv' --exclude='__pycache__' \
  "$(basename "$PLUGIN_DIR")" 2>/dev/null | tar -C "$BACKUP" -xf -

# 3. 覆盖：只覆盖代码，绝不碰 app/data（服务器的配置/人设/记忆）
echo "→ 覆盖代码…"
(cd "$SRC" && find . -mindepth 1 -maxdepth 1 -printf '%f\n') | while IFS= read -r entry; do
  case "$entry" in
    app|data) ;;
    *) rm -rf "${PLUGIN_DIR:?}/$entry"; cp -a "$SRC/$entry" "$PLUGIN_DIR/" ;;
  esac
done
# app/ 单独处理：只更新代码子目录，跳过 app/data
if [ -d "$SRC/app" ]; then
  for sub in pawbot plugins tools web run.py requirements.txt; do
    if [ -e "$SRC/app/$sub" ]; then
      rm -rf "${PLUGIN_DIR:?}/app/$sub"
      cp -a "$SRC/app/$sub" "$PLUGIN_DIR/app/"
    fi
  done
  for f in README.upstream.md ARCHITECTURE.md 使用指南.md; do
    [ -f "$SRC/app/$f" ] && cp -a "$SRC/app/$f" "$PLUGIN_DIR/app/"
  done
fi

echo
echo "✓ 更新完成（app/data 未动）"
echo "  备份在: $BACKUP"
echo "  回滚  : rm -rf '$PLUGIN_DIR' && cp -a '$BACKUP/$(basename "$PLUGIN_DIR")' '$PLUGIN_DIR'"
echo
echo "接下来：重启 DSH（bundle 层要重启才生效）"
