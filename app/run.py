#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PawBot 启动器。

    python run.py                 正常启动（协议端 6199 / 后台 6185）
    python run.py --port 6199     临时改协议端端口
    python run.py --webui 6185    临时改后台端口
    python run.py --check         只做自检，不起服务
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pawbot.core import Bot                      # noqa: E402
from pawbot.log import log                       # noqa: E402
from pawbot.webui import WebUI                   # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BANNER = r"""
  ___                 ___      _
 | _ \ __ _ __ __    | _ ) ___| |_
 |  _// _` \ V  V /  | _ \/ _ \  _|
 |_|  \__,_|\_/\_/   |___/\___/\__|   v1.0
 零依赖 OneBot v11 机器人 · 改就是了
"""


async def main(args):
    # 日志同时落盘：双击 start.bat 启动也能事后查（以前只有 > run.log 重定向才有）
    log.to_file(os.path.join(BASE_DIR, "data", "run.log"))
    bot = Bot(BASE_DIR)

    # 命令行覆盖，方便多开
    if args.port:
        bot.server.port = args.port
    if args.webui:
        bot.config.data["webui"]["port"] = args.webui

    webui = WebUI(
        bot,
        host=bot.config.get("webui.host", "127.0.0.1"),
        port=int(bot.config.get("webui.port", 6185)),
        token=bot.config.get("webui.token", ""),
        root=os.path.join(BASE_DIR, "web"),
    )

    print(BANNER)
    await bot.start()
    await webui.start()

    print(f"  后台   http://127.0.0.1:{bot.config.get('webui.port', 6185)}")
    print(f"  协议端 ws://0.0.0.0:{bot.server.port}{bot.server.path}")
    print("  把上面这个地址填进 NapCat → 网络配置 → WebSocket 客户端")
    print("  Ctrl+C 退出\n")

    if args.check:
        log.info("自检模式：服务已就绪，3 秒后退出")
        await asyncio.sleep(3)
        await webui.stop()
        await bot.stop()
        return

    stop = asyncio.Event()

    def _sig(*_):
        stop.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in ("SIGINT", "SIGTERM"):
            import signal
            if hasattr(signal, sig):
                try:
                    loop.add_signal_handler(getattr(signal, sig), _sig)
                except NotImplementedError:
                    pass
    except Exception:
        pass

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("正在关闭……")
        await webui.stop()
        await bot.stop()
        await asyncio.sleep(0.2)


def parse_args():
    p = argparse.ArgumentParser(description="PawBot 零依赖 QQ 机器人")
    p.add_argument("--port", type=int, help="OneBot 反向 WS 端口")
    p.add_argument("--webui", type=int, help="WebUI 端口")
    p.add_argument("--check", action="store_true", help="自检后退出")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
