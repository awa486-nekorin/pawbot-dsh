@echo off
cd /d "%~dp0"
echo ============================================
echo  拉取云端数据到本机（只同步人设/记忆）
echo ============================================
echo.
echo 前提：本机 PawBot 已停止；ECS 上已有同步包。
echo.
py -3.13 scripts\scp_sync.py --mode remote --action pull --root "%~dp0"
set RC=%ERRORLEVEL%
echo.
if %RC%==0 (
  echo [OK] 已拉取并导入；旧内容已备份到 data\backups\pre-pull-*.tgz
) else (
  echo [未完成] 退出码 %RC%，请看上面的提示。
)
echo.
pause
