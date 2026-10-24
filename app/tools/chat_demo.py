"""多轮对话演示：按顺序发几句话，把来回和时间打出来。

用法：
    python tools/chat_demo.py                     # 跑内置的几句
    python tools/chat_demo.py "在吗" "吃饭没"      # 自己指定
    python tools/chat_demo.py --gap 10 "..." "..."  # 每句之间隔多久

注意：机器人发来的每一条动作请求都要回执（带原样 echo）——不只
send_group_msg，还包括连接后的 get_login_info / get_status 探活。
「在吗」走的是 basic.ping，它先调 get_status 再回复；demo 不回这条
回执的话，处理器干等 8 秒超时，「在吗」就静默丢了。
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, ".")
from pawbot import wsproto  # noqa: E402

GROUP = 987654321
LINES = [
    "在吗",
    "今天上课被老师点名回答，结果我一句都答不上来，好尴尬",
    "你觉得我该不该报名下个月的比赛",
    "话说你是不是真的在吃白饭",
    "说句安慰我的话吧",
]
GAP = 8.0          # 每句之间隔多久（秒）。太小会被 safety 的 burst 节流吃掉


async def say(text, wait=100.0):
    ws = await wsproto.connect("127.0.0.1", 6199, "/ws",
                               {"X-Self-ID": "10001", "X-Client-Role": "Universal"})
    await ws.send_text(json.dumps({
        "time": int(time.time()), "self_id": 10001,
        "post_type": "meta_event", "meta_event_type": "lifecycle",
        "sub_type": "connect"}))
    await asyncio.sleep(0.35)
    t0 = time.time()
    await ws.send_text(json.dumps({
        "time": int(time.time()), "self_id": 10001,
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "message_id": str(int(time.time() * 1000) % 10 ** 7),
        "group_id": GROUP, "user_id": 123456, "anonymous": None,
        "message": [{"type": "at", "data": {"qq": "10001"}},
                    {"type": "text", "data": {"text": text}}],
        "raw_message": "x", "font": 1,
        "sender": {"user_id": 123456, "nickname": "阿明"}}))

    print(f"\n我  ▸ {text}")
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(),
                                         timeout=max(0.1, deadline - time.time()))
        except asyncio.TimeoutError:
            print("    （没回）")
            break
        if raw is None:
            break
        d = json.loads(raw)
        action = d.get("action")
        if not action:
            continue
        if action == "send_group_msg":
            dt = time.time() - t0
            for s in d["params"]["message"]:
                if s["type"] == "text" and s["data"]["text"].strip():
                    t = s["data"]["text"].strip()
                    if len(t) > 1:
                        print(f"鱼  ▸ {t}")
                elif s["type"] == "image":
                    f = s["data"]["file"]
                    print("    [图] " + ("base64" if f.startswith("base64://") else f[:60]))
            print(f"    （{dt:.1f}s）")
        # 所有动作请求都回执、带上原样 echo；漏一个，机器人那边就干等到超时
        await ws.send_text(json.dumps({
            "status": "ok", "retcode": 0,
            "data": {"online": True} if action == "get_status" else {"message_id": 1},
            "echo": d.get("echo", "")}))
        if action in ("send_group_msg", "send_private_msg"):
            await asyncio.sleep(0.5)      # 让机器人把回执消化完再断开
            break
    await ws.close()


def _parse_args():
    global LINES, GAP
    argv = sys.argv[1:]
    if not argv:
        return
    if argv[0] == "--gap" and len(argv) > 1:
        GAP = float(argv[1])
        argv = argv[2:]
    if argv:
        LINES = argv


async def main():
    for i, line in enumerate(LINES):
        await say(line)
        if i < len(LINES) - 1:
            await asyncio.sleep(GAP)


_parse_args()
asyncio.run(main())
