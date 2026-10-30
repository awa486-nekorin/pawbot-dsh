#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PawBot 启动器。

    python run.py                 正常启动（协议端 6199 / 后台 6185）
    python run.py --port 6199     临时改协议端端口
    python run.py --webui 6185    临时改后台端口
    python run.py --check         只做自检，不起服务

批次 2 生命周期（所有退出路径都走同一套清理）：
  1. 先解析数据根（PAWBOT_DATA_DIR > <BASE_DIR>/data），再拿单实例运行锁；
     两步都在 Bot 构造 / 日志文件 / 配置写入之前，第二个实例不会写任何业务数据。
  2. 运行锁由真实 Python 进程持有（Host 不持锁，避免 py.exe launcher 的 pid 差异）。
  3. Windows 上优雅停机走 HTTP 控制面（POST /api/control/shutdown，Host 注入
     PAWBOT_CONTROL_TOKEN）；POSIX 额外支持 SIGTERM/SIGINT。
  4. 退出码：0 正常 / 10 控制面 shutdown / 其它非 0 异常（2 数据根、3 锁被占用、
     4 锁状态未知、1 启动或运行异常）。
  5. atexit 兜底 force flush Store + 释放锁。
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pawbot.core import Bot                      # noqa: E402
from pawbot.log import log                       # noqa: E402
from pawbot.lifecycle import (                   # noqa: E402
    EXIT_CONTROL_SHUTDOWN,
    EXIT_DATA_ROOT,
    EXIT_ERROR,
    EXIT_LOCKED,
    EXIT_LOCK_UNKNOWN,
    EXIT_OK,
    HOST_PID_ENV,
    RUNTIME_VERSION_ENV,
    DataRootError,
    RuntimeLifecycle,
    RuntimeLocked,
    RuntimeLockUnknown,
    acquire_runtime_lock,
    take_control_token,
    resolve_data_root,
)
from pawbot.webui import WebUI                   # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BANNER = r"""
  ___                 ___      _
 | _ \ __ _ __ __    | _ ) ___| |_
 |  _// _` \ V  V /  | _ \/ _ \  _|
 |_|  \__,_|\_/\_/   |___/\___/\__|   v1.0
 零依赖 OneBot v11 机器人 · 改就是了
"""


def _env_int(name: str) -> int:
    try:
        return int(str(os.environ.get(name, "") or 0))
    except (TypeError, ValueError):
        return 0


def _run_version() -> str:
    version = str(os.environ.get(RUNTIME_VERSION_ENV, "") or "").strip()
    if version:
        return version
    try:
        import json
        with open(os.path.join(os.path.dirname(BASE_DIR), "package.json"), "r",
                  encoding="utf-8") as handle:
            return str(json.load(handle).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def _install_signal_handlers(life: RuntimeLifecycle) -> None:
    """POSIX 用 loop.add_signal_handler；Windows 退到 signal.signal（Ctrl+C）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def _stop(*_args):
        life.signal_stop(EXIT_OK)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _stop)
            continue
        except (NotImplementedError, RuntimeError, AttributeError):
            pass
        try:
            # Windows 上 add_signal_handler 不可用，只能用 signal.signal；必须把 stop
            # 投回事件循环（call_soon_threadsafe），否则 selector 可能一直睡着。
            signal.signal(sig, lambda *_a, _loop=loop, _life=life:
                          _loop.call_soon_threadsafe(_life.signal_stop, EXIT_OK))
        except (ValueError, OSError, RuntimeError):
            pass


async def _stop_services(bot, webui) -> bool:
    """按「先停入口、再停业务」的顺序收尾；任何一步失败都不影响后面的 flush/解锁。"""
    ok = True
    if webui is not None:
        try:
            await webui.stop()
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.warn(f"停止 WebUI 失败：{type(exc).__name__} {exc}")
    if bot is not None:
        try:
            await bot.stop()
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.warn(f"停止 Bot 失败：{type(exc).__name__} {exc}")
    return ok


async def main(args):
    # ── 1) 数据根：必须先于 Bot 构造 / 日志 / 配置写入 ─────────────────────
    try:
        data_root = resolve_data_root(BASE_DIR)
    except DataRootError as exc:
        print(f"[pawbot] 数据根错误：{exc}", file=sys.stderr)
        return EXIT_DATA_ROOT
    for warning in data_root.warnings:
        print(f"[pawbot] 数据根提示：{warning}", file=sys.stderr)

    # ── 2) 单实例锁：真实 Python 进程持有，位置 <dataRoot>/.runtime.lock ──
    host_pid = _env_int(HOST_PID_ENV)
    version = _run_version()
    try:
        lock = acquire_runtime_lock(data_root.root, host_pid=host_pid, version=version)
    except RuntimeLocked as exc:
        print(f"[pawbot] 单实例锁：{exc}。另一个 PawBot 正在使用该数据目录，本次不启动。",
              file=sys.stderr)
        return EXIT_LOCKED
    except RuntimeLockUnknown as exc:
        print(f"[pawbot] 单实例锁状态未知：{exc}", file=sys.stderr)
        return EXIT_LOCK_UNKNOWN

    # 控制 token 取出来立刻从 env 删除：Bot 拉起的业务子进程不应继承这个凭证
    # （与 app/pawbot/brain.py 对 brain token 的处理一致）。
    control_token = take_control_token()
    life = RuntimeLifecycle(
        data_root=data_root.root,
        lock=lock,
        control_token=control_token,
        version=version,
        host_pid=host_pid,
    )

    # atexit 兜底：解释器正常退出（含 sys.exit / 未捕获异常）时再 force flush 一次 + 解锁。
    def _atexit_cleanup():
        try:
            life.cleanup()
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_atexit_cleanup)

    # ── 3) 日志：落在数据根下，保持旧文件名 run.log ──────────────────────
    log.to_file(os.path.join(data_root.root, "run.log"))
    log.info(f"数据根：{data_root.root}（来源：{data_root.source}"
             f"{'，外部目录' if data_root.external else ''}）")
    if control_token:
        log.info("控制面已启用（仅 loopback + 独立 Bearer token）")
    else:
        log.info("控制面未启用（PAWBOT_CONTROL_TOKEN 为空；独立运行可显式设置该 env 开启）")

    bot = None
    webui = None
    exit_code = EXIT_ERROR
    try:
        bot = Bot(BASE_DIR)
        life.bind_bot(bot)
        life.install_event_gate(bot)

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
            control_token=control_token,
            lifecycle=life,
        )
        life.bind_webui(webui)

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
            exit_code = EXIT_OK
        else:
            _install_signal_handlers(life)
            try:
                await life.wait_stopped()
            except (KeyboardInterrupt, asyncio.CancelledError):
                life.signal_stop(EXIT_OK)
            exit_code = life.exit_code
    except (KeyboardInterrupt, asyncio.CancelledError):
        life.signal_stop(EXIT_OK)
        exit_code = life.exit_code
    except Exception as exc:  # noqa: BLE001
        log.error(f"启动/运行失败：{type(exc).__name__}: {exc}")
        exit_code = EXIT_ERROR
    finally:
        # 退出码必须在所有 stop/flush/cleanup 都**完成之后**再决定：try 里的 return
        # 会在 finally 之前求值，所以这里只写最终结果，不在中途修 exit_code。
        final_ok = True
        # Standalone signals and --check must drain the same writers as HTTP
        # shutdown, but retain their normal exit-code semantics.
        if bot is not None and not life.shutting_down:
            original_code = life.exit_code
            try:
                drained = await life.request_shutdown()
                if drained.get("ok"):
                    life.exit_code = original_code
                else:
                    final_ok = False
            except Exception as exc:  # noqa: BLE001
                final_ok = False
                log.warn(f"退出前排空失败：{type(exc).__name__}: {exc}")
        try:
            services_ok = await _stop_services(bot, webui)
        except Exception as exc:  # noqa: BLE001 —— _stop_services 已兜底，这里再加一层
            services_ok = False
            log.warn(f"停止服务异常：{type(exc).__name__} {exc}")
        if not services_ok:
            final_ok = False
        try:
            final_flush = life.flush_store()
        except Exception as exc:  # noqa: BLE001
            final_flush = {"ok": False, "flushed": False,
                           "error": f"{type(exc).__name__}: {exc}"}
        if not final_flush.get("ok"):
            final_ok = False
        try:
            # cleanup 内部还会 force flush 一次并释放运行锁；任何一步失败都算 cleanup 失败。
            cleanup = life.cleanup()
        except Exception as exc:  # noqa: BLE001
            cleanup = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not cleanup.get("ok"):
            final_ok = False
        # 正常 0 / 控制面成功停机 10 只在「所有收尾都成功」时保留；否则一律 1。
        if exit_code in (EXIT_OK, EXIT_CONTROL_SHUTDOWN):
            if final_ok and life.exit_code in (EXIT_OK, EXIT_CONTROL_SHUTDOWN):
                exit_code = life.exit_code
            else:
                exit_code = EXIT_ERROR
    return exit_code


def parse_args():
    p = argparse.ArgumentParser(description="PawBot 零依赖 QQ 机器人")
    p.add_argument("--port", type=int, help="OneBot 反向 WS 端口")
    p.add_argument("--webui", type=int, help="WebUI 端口")
    p.add_argument("--check", action="store_true", help="自检后退出")
    return p.parse_args()


if __name__ == "__main__":
    exit_code = EXIT_ERROR
    try:
        exit_code = asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        exit_code = EXIT_OK
    except Exception as exc:  # noqa: BLE001
        print(f"[pawbot] 未捕获异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        exit_code = EXIT_ERROR
    sys.exit(int(exit_code or EXIT_OK))
