#!/usr/bin/env bash
# ECS 一键：查看同步包与本机 content 状态（只读）
set -u
cd "$(dirname "$0")"
python3 scripts/scp_sync.py --mode local --action status --root "$PWD"
rc=$?
echo
if [ -t 0 ]; then read -r -p "按 Enter 关闭..." _; fi
exit "$rc"
