#!/usr/bin/env bash
# ECS 一键：把 /srv/pawbot-sync/pawbot-content.tgz 导入本机 PawBot 数据根
set -u
cd "$(dirname "$0")"
echo "============================================"
echo " 拉取云端同步包 -> 本机 PawBot（只同步 content）"
echo "============================================"
echo
echo "前提：本机 PawBot 已停止；同步包已由 Windows 推上来。"
echo
python3 scripts/scp_sync.py --mode local --action pull --root "$PWD"
rc=$?
echo
if [ "$rc" -eq 0 ]; then
  echo "[OK] 已导入；旧内容已备份到 data/backups/pre-pull-*.tgz"
else
  echo "[未完成] 退出码 $rc，请看上面的提示。"
fi
echo
if [ -t 0 ]; then read -r -p "按 Enter 关闭..." _; fi
exit "$rc"
