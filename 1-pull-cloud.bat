@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  云端：把同步包导入本机 PawBot（content）
echo ============================================
echo.
echo 前提：本机 PawBot 已停止；同步包已由本机 Windows 推上来。
echo.
py -3.13 scripts\scp_sync.py --mode local --action pull --root "%~dp0"
set RC=%ERRORLEVEL%
echo.
if %RC%==0 (
  echo [OK] 已导入；旧内容已备份到 data\backups\pre-pull-*.tgz
) else (
  echo [未完成] 退出码 %RC%，请看上面的提示。
)
echo.
pause
