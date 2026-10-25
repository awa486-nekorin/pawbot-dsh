#!/usr/bin/env bash
# 把「服务器上的插件改动」打包成一个 tgz，带回来在能连 GitHub 的机器上提交。
# 用法：bash collect-server-changes.sh [插件目录]
#   不给参数时自动在 ~/.dsh/profiles/*/node_modules/@local/pawbot 里找
# 产物：/tmp/pawbot-changes-<时间>.tgz  （同时打印大小与 sha256）
set -uo pipefail

find_plugin_dir() {
  local cand
  for cand in "$HOME"/.dsh/profiles/*/node_modules/@local/pawbot \
              "$HOME"/.dsh/profiles/*/node_modules/@local/pawbot-dsh; do
    [ -f "$cand/package.json" ] && { echo "$cand"; return 0; }
  done
  # 再退一步：整棵树里找
  find "$HOME" -maxdepth 8 -type d -path '*@local/pawbot' -name pawbot 2>/dev/null | head -n1
}

PLUGIN_DIR="${1:-$(find_plugin_dir)}"
if [ -z "${PLUGIN_DIR:-}" ] || [ ! -d "$PLUGIN_DIR" ]; then
  echo "✗ 找不到插件目录，请手动指定：bash $0 /path/to/@local/pawbot" >&2
  exit 1
fi
cd "$PLUGIN_DIR" || exit 1

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/tmp/pawbot-changes-${STAMP}.tgz"
WORK="$(mktemp -d /tmp/pawbot-collect.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

echo "插件目录: $PLUGIN_DIR"

# 运行时/依赖的东西一律不要（体积大且不是改动）
EXCLUDES=(
  --exclude='node_modules'
  --exclude='.venv'
  --exclude='__pycache__'
  --exclude='*.pyc'
  --exclude='*.before-*'
  --exclude='*.bak'
  --exclude='*.log'
  --exclude='app/data/sessions.json'
  --exclude='app/data/backup'
  --exclude='app/data/image_cache'
  --exclude='app/data/stickers'
  --exclude='app/data/browser'
  --exclude='app/data/napcat'
  --exclude='app/data/people/*.md'
  --exclude='app/data/diary/*.md'
  --exclude='app/data/memory/entities.jsonl'
)

if [ -d .git ]; then
  echo "模式: git 仓库（有 .git，差异最精确）"
  git status --porcelain=v1 > "$WORK/STATUS.txt" 2>&1
  git rev-parse HEAD > "$WORK/HEAD.txt" 2>&1
  git diff > "$WORK/tracked.patch" 2>&1
  git diff --cached >> "$WORK/tracked.patch" 2>&1
  # 未跟踪的新文件单独列出并原样打包
  git ls-files -o --exclude-standard > "$WORK/UNTRACKED.txt" 2>&1
  mkdir -p "$WORK/untracked"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      app/.venv/*|*/__pycache__/*|*.pyc) continue ;;
    esac
    mkdir -p "$WORK/untracked/$(dirname "$f")"
    cp -a "$f" "$WORK/untracked/$f" 2>/dev/null
  done < "$WORK/UNTRACKED.txt"
else
  echo "模式: 非 git（无 .git，只能整份代码打包，回来做三方对比）"
  # 只打「可能是代码改动」的路径，避开所有运行时数据
  for p in index.js client.js package.json cordis.patch.yml icon.svg README.md DEPLOY.md \
           lib bili-watch locale app/run.py app/requirements.txt \
           app/pawbot app/plugins app/web; do
    [ -e "$p" ] && cp -a "$p" "$WORK/" 2>/dev/null
  done
  # 配置和人设单独放，方便回来比对（这两个是服务器个性化，通常不该进 repo）
  mkdir -p "$WORK/_data-snapshot"
  [ -f app/data/config.json ] && cp -a app/data/config.json "$WORK/_data-snapshot/" 2>/dev/null
  [ -f app/data/persona.md ] && cp -a app/data/persona.md "$WORK/_data-snapshot/" 2>/dev/null
  find . -maxdepth 4 -type f -newermt '-30 days' \( -name '*.js' -o -name '*.mjs' -o -name '*.py' -o -name '*.json' -o -name '*.md' \) \
    ! -path './node_modules/*' ! -path './.venv/*' ! -path './app/.venv/*' ! -path '*/__pycache__/*' \
    ! -path './app/data/*' -printf '%TY-%Tm-%Td %TH:%TM  %10s  %p\n' 2>/dev/null \
    | sort > "$WORK/RECENT-FILES.txt"
fi

echo "$PLUGIN_DIR" > "$WORK/PLUGIN_DIR.txt"
tar -czf "$OUT" -C "$(dirname "$WORK")" "$(basename "$WORK")" 2>/dev/null
SZ="$(du -h "$OUT" | cut -f1)"
echo "✓ 产物: $OUT  ($SZ)"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$OUT"
else
  shasum -a 256 "$OUT"
fi
echo
echo "把这两行发回来即可（路径 + sha256）："
echo "  $OUT"
echo "  $( (command -v sha256sum >/dev/null 2>&1 && sha256sum "$OUT") || (command -v shasum >/dev/null 2>&1 && shasum -a 256 "$OUT") )"
