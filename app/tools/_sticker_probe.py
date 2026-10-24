import asyncio, json, sys, time
sys.path.insert(0, ".")
from pawbot import wsproto
GROUP = 987654321

def mk(mid, text, uid=123456, nick="阿明"):
    return {"time": int(time.time()), "self_id": 10001, "post_type": "message",
            "message_type": "group", "sub_type": "normal", "message_id": mid,
            "group_id": GROUP, "user_id": uid, "anonymous": None,
            "message": [{"type": "at", "data": {"qq": "10001"}},
                        {"type": "text", "data": {"text": text}}],
            "raw_message": text, "font": 1,
            "sender": {"user_id": uid, "nickname": nick}}

async def turn(ws, ev, wait=150):
    t0 = time.time()
    await ws.send_text(json.dumps(ev))
    print(f"\n我 ▸ {ev['message'][-1]['data']['text']}", flush=True)
    sticker = False
    deadline = t0 + wait
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        if raw is None:
            break
        d = json.loads(raw); action = d.get("action")
        if not action:
            continue
        if action == "send_group_msg":
            kinds = [s["type"] for s in d["params"]["message"]]
            text = "".join(s["data"]["text"] for s in d["params"]["message"]
                           if s["type"] == "text").strip()
            if "image" in kinds:
                sticker = True
                print(f"[{time.time()-t0:5.1f}s] [表情包] {text!r}", flush=True)
            else:
                print(f"[{time.time()-t0:5.1f}s] 文字 {text!r}", flush=True)
        await ws.send_text(json.dumps({"status": "ok", "retcode": 0,
            "data": {"online": True} if action == "get_status" else {"message_id": 1},
            "echo": d.get("echo", "")}))
        if action == "send_group_msg" and not sticker:
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=9)
                    d = json.loads(raw); action = d.get("action")
                    if action == "send_group_msg":
                        kinds = [s["type"] for s in d["params"]["message"]]
                        if "image" in kinds:
                            sticker = True
                            print(f"[{time.time()-t0:5.1f}s] [表情包]", flush=True)
                    if action:
                        await ws.send_text(json.dumps({"status": "ok", "retcode": 0,
                            "data": {"message_id": 1}, "echo": d.get("echo", "")}))
            except asyncio.TimeoutError:
                pass
            break
    return sticker

async def main():
    ws = await wsproto.connect("127.0.0.1", 6199, "/ws",
                               {"X-Self-ID": "10001", "X-Client-Role": "Universal"})
    await ws.send_text(json.dumps({"time": int(time.time()), "self_id": 10001,
        "post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect"}))
    await asyncio.sleep(0.4)
    base = int(time.time()) % 10 ** 6 * 10
    lines = ["今天上班好累啊", "外卖凉了真烦", "笑死我了", "困死了想睡觉",
             "在吗", "周末想去海边", "我失恋了", "溜了溜了"]
    got = 0
    for i, line in enumerate(lines):
        got += 1 if await turn(ws, mk(base + i, line)) else 0
        await asyncio.sleep(3)
    print(f"\n=== {got}/{len(lines)} 条回复带表情包 ===")
    await ws.close()

asyncio.run(main())
