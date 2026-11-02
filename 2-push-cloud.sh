#!/usr/bin/env bash
# ECS 一键：把本机 PawBot 的 content/ 打包到 /srv/pawbot-sync/pawbot-content.tgz
set -u
cd "$(dirname "$0")"
echo "============================================"
echo " 本机 PawBot content -> 云端同步包"
echo "============================================"
echo
echo "前提：本机 PawBot 已停止。"
echo
python3 scripts/scp_sync.py --mode local --action push --root "$PWD"
rc=$?
echo
if [ "$rc" -eq 0 ]; then
  echo "[OK] 已导出到 /srv/pawbot-sync/pawbot-content.tgz"
else
  echo "[未完成] 退出码 $rc，请看上面的提示。"
fi
echo
if [ -t 0 ]; then read -r -p "按 Enter 关闭..." _; fi
exit "$rc"
