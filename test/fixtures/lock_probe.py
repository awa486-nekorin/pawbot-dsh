"""离线测试夹具：在独立进程里持有运行锁 / guard，验证并发、崩溃与超时边界。

只用临时数据根；不碰真实 app/data。stdout 每行一个 JSON（Node 侧解析）。

用法：
    py -3.13 test/fixtures/lock_probe.py <dataRoot>
环境变量：
    PAWBOT_HOLD_SECONDS=20        取得运行锁后持有 N 秒再释放
    PAWBOT_HOLD_GUARD_SECONDS=20  只持有 guard（不写正式锁）N 秒
"""

import json
import os
import sys
import time

APP_DIR = os.environ.get("PAWBOT_APP_DIR", "")
DATA_ROOT = os.environ.get("PAWBOT_DATA_ROOT", "")
if APP_DIR:
    sys.path.insert(0, APP_DIR)

from pawbot import lifecycle  # noqa: E402


def emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    data_root = sys.argv[1] if len(sys.argv) > 1 else DATA_ROOT
    guard_hold = float(os.environ.get("PAWBOT_HOLD_GUARD_SECONDS", "0") or 0)
    if guard_hold > 0:
        try:
            with lifecycle.guard_lock(data_root, timeout=10):
                emit({"ok": True, "guard": True, "pid": os.getpid()})
                time.sleep(guard_hold)
        except Exception as exc:  # noqa: BLE001
            emit({"ok": False, "code": "GUARD_FAILED", "message": f"{type(exc).__name__}: {exc}"})
            return 5
        emit({"ok": True, "guard_released": True, "pid": os.getpid()})
        return 0

    try:
        lock = lifecycle.acquire_runtime_lock(data_root, version="python-probe")
    except lifecycle.RuntimeLocked as exc:
        emit({"ok": False, "code": "LOCKED", "message": str(exc)})
        return 2
    except lifecycle.RuntimeLockUnknown as exc:
        emit({"ok": False, "code": "LOCK_UNKNOWN", "message": str(exc)})
        return 4
    emit({"ok": True, "pid": os.getpid(), "nonce": lock["nonce"]})
    hold = float(os.environ.get("PAWBOT_HOLD_SECONDS", "0") or 0)
    if hold > 0:
        time.sleep(hold)
    lifecycle.release_runtime_lock(data_root, lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
