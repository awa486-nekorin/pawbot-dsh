"""Mock OneBot 端到端测试：往群里发一句话，看机器人怎么回。

用法：python tools/mock_send.py "要发的话" [群号]
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, ".")
from pawbot import wsproto  # noqa: E402


async def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "在吗"
    group = int(sys.argv[2]) if len(sys.argv) > 2 else 987654321
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 140

    ws = await wsproto.connect("127.0.0.1", 6199, "/ws",
                               {"X-Self-ID": "10001", "X-Client-Role": "Universal"})
    await ws.send_text(json.dumps({
        "time": int(time.time()), "self_id": 10001,
        "post_type": "meta_event", "meta_event_type": "lifecycle",
        "sub_type": "connect"}))
    await asyncio.sleep(0.4)
    await ws.send_text(json.dumps({
        "time": int(time.time()), "self_id": 10001,
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "message_id": str(int(time.time() * 1000) % 10 ** 7),
        "group_id": group, "user_id": 123456, "anonymous": None,
        "message": [{"type": "at", "data": {"qq": "10001"}},
                    {"type": "text", "data": {"text": text}}],
        "raw_message": "x", "font": 1,
        "sender": {"user_id": 123456, "nickname": "阿明"}}))

    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(),
                                         timeout=max(0.1, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        if raw is None:
            break
        d = json.loads(raw)
        if d.get("action") == "send_group_msg":
            for s in d["params"]["message"]:
                if s["type"] == "text" and s["data"]["text"].strip():
                    print("  ", s["data"]["text"][:200])
                elif s["type"] == "image":
                    f = s["data"]["file"]
                    if f.startswith("base64://"):
                        print(f"    [图] base64 {len(f) // 1024}KB")
                    else:
                        print("    [图]", f[:70])
            await ws.send_text(json.dumps(
                {"status": "ok", "retcode": 0, "data": {"message_id": 1}}))
            break
    await ws.close()


asyncio.run(main())
