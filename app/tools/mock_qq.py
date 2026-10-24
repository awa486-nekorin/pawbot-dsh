#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
假 NapCat —— 用来在没有 QQ 的情况下验证整条链路。

它做的事和真 NapCat 在反向 WS 模式下完全一样：
  1. 作为 WebSocket 客户端连到 ws://127.0.0.1:6199/ws
  2. 带上 X-Self-ID / X-Client-Role 头
  3. 发 meta_event/lifecycle/connect + 定时心跳
  4. 发一条群 @ 消息
  5. 收到 send_group_msg 就打印出来并回执

用法：
    python tools/mock_qq.py
    python tools/mock_qq.py --text "大肥鱼在干嘛" --user 123456
"""

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import wsproto           # noqa: E402

SELF_ID = 10001
GROUP_ID = 987654321


async def main(args):
    print(f"连接 ws://{args.host}:{args.port}{args.path} …")
    ws = await wsproto.connect(args.host, args.port, args.path, {
        "X-Self-ID": str(SELF_ID),
        "X-Client-Role": "Universal",
    })
    print("已连上，握手成功。")

    async def heartbeat():
        while True:
            await asyncio.sleep(3)
            await ws.send_text(json.dumps({
                "post_type": "meta_event", "meta_event_type": "heartbeat",
                "time": int(time.time()), "self_id": SELF_ID,
                "status": {"online": True, "good": True},
                "interval": 3000,
            }))

    hb = asyncio.create_task(heartbeat())

    # lifecycle connect
    await ws.send_text(json.dumps({
        "post_type": "meta_event", "meta_event_type": "lifecycle",
        "sub_type": "connect", "time": int(time.time()), "self_id": SELF_ID,
    }))

    await asyncio.sleep(0.6)

    # 群消息：@ 机器人 + 文本
    segments = [{"type": "at", "data": {"qq": str(SELF_ID)}},
                {"type": "text", "data": {"text": " " + args.text}}]
    await ws.send_text(json.dumps({
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "time": int(time.time()), "self_id": SELF_ID,
        "message_id": int(time.time()) % 100000,
        "group_id": GROUP_ID,
        "user_id": args.user,
        "message": segments,
        "raw_message": f"[CQ:at,qq={SELF_ID}] {args.text}",
        "sender": {"user_id": args.user, "nickname": args.nickname, "card": args.nickname},
    }, ensure_ascii=False))
    print(f"已发送群消息：@{args.nickname} → {args.text}")

    # 等回执
    deadline = time.time() + args.wait
    got = 0
    try:
        while time.time() < deadline:
            text = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
            if text is None:
                print("连接被关闭")
                break
            data = json.loads(text)
            if "action" in data:
                got += 1
                action = data["action"]
                params = data.get("params", {})
                print(f"\n← 收到动作 {action}")
                print(f"  {json.dumps(params, ensure_ascii=False, indent=2)[:500]}")
                await ws.send_text(json.dumps({
                    "status": "ok", "retcode": 0,
                    "data": {"message_id": int(time.time()) % 100000},
                    "echo": data.get("echo"),
                }))
    except asyncio.TimeoutError:
        pass
    finally:
        hb.cancel()
        await ws.close()

    print(f"\n共收到 {got} 次动作调用。" if got else "\n一条动作都没收到——检查插件是否加载、唤醒词/前缀是否匹配。")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6199)
    p.add_argument("--path", default="/ws")
    p.add_argument("--text", default="在吗，测试一下")
    p.add_argument("--user", type=int, default=55555)
    p.add_argument("--nickname", default="阿明")
    p.add_argument("--wait", type=float, default=12)
    asyncio.run(main(p.parse_args()))
