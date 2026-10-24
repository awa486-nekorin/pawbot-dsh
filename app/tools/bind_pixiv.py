"""
绑定 Pixiv：启动 PawBot 专用的 Google Chrome 配置，在里面登录，然后借这个会话找图。

该 Chrome 使用项目 data/browser/chrome-pixiv 下的独立配置，不会关闭或复用日常 Edge / Chrome。
首次需要在新窗口里重新登录 Google 和 Pixiv。

用法：python tools/bind_pixiv.py [超时秒数，默认 600]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

# Windows 控制台默认是 GBK。要输出中文必须：
#   1) bat 里 chcp 65001（控制台切 UTF-8）
#   2) 这里把 stdout 也切到 UTF-8
# 两个条件缺一个就是乱码。
def _setup_console():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _cn_ok() -> bool:
    """能不能用中文输出。

    两个条件：控制台码页是 65001，**而且** stdout 是真终端。
    被重定向到管道时即使码页是 UTF-8，读取方多半按 GBK 解 → 一样乱码，
    所以那种情况直接用英文。
    """
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleOutputCP() != 65001:
            return False
    except Exception:
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


_setup_console()
_CN = _cn_ok()


def T(cn: str, en: str) -> str:
    """控制台不是 UTF-8 就退回英文，免得满屏乱码。"""
    return cn if _CN else en

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pawbot.pixiv_session import PixivSession  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "data", "config.json")
LOGIN_URL = "https://www.pixiv.net/"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    timeout = float(args[0]) if args else 600
    sess = PixivSession()
    if sess.cdp_alive() and not sess.chrome_cdp_ready():
        print(T("9222 端口已被非 PawBot Chrome 会话占用；为避免干扰其他浏览器，本次不继续。",
                "CDP port 9222 is occupied by a browser other than the PawBot Chrome profile; aborting safely."))
        return 1

    if sess.chrome_cdp_ready():
        print("PawBot 专用 Chrome 已带着调试端口运行，直接使用。")
    else:
        print(T("正在启动 PawBot 专用 Chrome（不会关闭你现有的浏览器）…",
                "Starting the dedicated PawBot Chrome profile (existing browsers will stay open)..."))
        if not sess.launch_chrome(LOGIN_URL):
            print(T("Chrome 启动失败。请确认已安装 Google Chrome，且 9222 端口可用。",
                    "Chrome could not start. Check that Google Chrome is installed and port 9222 is available."))
            return 1
        time.sleep(4)

    print(T("首次使用需在新 Chrome 窗口重新登录 Google 和 Pixiv。",
            "Sign in to Google and Pixiv in the new Chrome profile; existing browser logins are not copied."))

    print(T("[3/3] 等你登录（最多 %d 秒）…" % int(timeout),
              "[3/3] waiting for you to log in (max %d s)..." % int(timeout)))
    print(T("      → 在 PawBot 专用 Chrome 窗口里登录 Pixiv（Google 账号也行）",
              "      -> log into Pixiv in the dedicated PawBot Chrome window (Google sign-in is supported)"))
    print(T("      → 登完这边自动就好了，不用管这个窗口",
              "      -> once logged in it is detected automatically"))
    print()

    ok = False
    try:
        ok = asyncio.run(sess.wait_login(timeout))
    except Exception as e:
        print(f"  出错：{type(e).__name__} {e}")

    if not ok:
        print(T("\n没等到登录。可能没登完，再跑一次。",
              "\nlogin not detected. Maybe it was not finished, run again."))
        print(T("（专用 Chrome 仍开着；登录完成后重新运行此脚本即可继续）",
              "(The dedicated Chrome window is still open; log in, then run this script again.)"))
        return 2

    print(T("\n登录成功！", "\nLogged in!"))
    try:
        with open(CFG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("tools", {})["pixiv_use_browser"] = True
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(T(f"已写进 {CFG}", f"written to {CFG}"))
    except Exception as e:
        print(T(f"写配置失败（不影响用）：{e}", f"config write failed: {e}"))

    print(T("\n** PawBot 专用 Chrome 保持开启 **，机器人需要借用该登录会话。",
            "\n**Keep the dedicated PawBot Chrome open** so the bot can use that session."))
    print(T("以后找图机器人会借这个会话，R-18 也能拿（发出前自动打码）。",
            "The bot will borrow this session for image search (R-18 is auto-censored)."))

    time.sleep(2)
    if sess.chrome_cdp_ready():
        print(T("自检：专用 Chrome 及调试端口正常，可以关掉这个黑窗口。",
              "self-check: dedicated Chrome and CDP are ready. You can close this console."))
    else:
        print(T("⚠ 自检：专用 Chrome 调试端口未响应；请保持 Chrome 开启并重新检查状态。",
              "WARN: dedicated Chrome CDP is not responding; keep Chrome open and check status again."))

    return 0


if __name__ == "__main__":
    sys.exit(main())
