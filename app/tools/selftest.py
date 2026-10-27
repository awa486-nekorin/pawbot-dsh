#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
自检：不连 QQ、不连大模型，验证人设 / 记忆 / 表情包 / 工具循环是不是通的。

    python tools/selftest.py

它会用一个假的模型，故意发出工具调用，看你接不接得住。
"""

import asyncio
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import message as msg                     # noqa: E402
from pawbot.context import Context                    # noqa: E402
from pawbot.core import Bot                           # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK, FAIL = "[ok]  ", "[FAIL]"


def check(title, cond, detail=""):
    print(f"{OK if cond else FAIL} {title}" + (f"  {detail}" if detail else ""))
    return cond


class FakeEvent:
    post_type = "message"
    type = "message"
    message_type = "group"
    message_id = 111
    user_id = 55555
    group_id = 987654321
    self_id = 10001
    sender = {"nickname": "阿明", "card": "阿明"}
    plain = "你会用 web_search 吗"
    to_me = True
    is_group = True
    session_id = "group_987654321"
    sender_name = "阿明"
    role = "member"
    matched = None


def make_ctx(bot):
    """用真正的 Context，这样引用/@ 的组装顺序才是真被测到的。"""
    ctx = Context(event=FakeEvent(), bot=bot, config=bot.config)
    ctx.captured = []

    async def fake_send(message, **kw):
        ctx.captured.append(msg.normalize(message))
        return {}

    ctx.send = fake_send
    return ctx


async def main():
    bot = Bot(BASE)
    # 自检别把测试会话和去重记录写进真实的 data/media_ledger.json
    from types import SimpleNamespace
    from pawbot.media import MediaLedger
    # 自检固定表情包骰子为必发，否则 media.sticker_chance=0.85 会让这两条检查偶发失败
    bot.media_ledger = MediaLedger(None, config=SimpleNamespace(
        get=lambda key, default=None: 1.0 if key == "media.sticker_chance" else default))
    print("\n== 人设 ==")
    p = bot.persona.build(group_id=987654321, sender="阿明")
    check("persona.md 读到了", "大肥鱼" in p, f"{len(p)} 字")
    from unittest.mock import patch
    with patch.object(bot.persona, "facts", return_value="- 自检偏好：喜欢安静的键盘"):
        memory_prompt = bot.persona.build(group_id=987654321, sender="阿明")
    check("记忆注入进来了", "自检偏好：喜欢安静的键盘" in memory_prompt)
    # 网络用词是群聊的日常语言：跟着用，不刻意学也不刻意避开（v3 人设口径）
    check("网络用词注入进来了", "别人可能这么说话" in p or "网络用词" in p)
    check("网络用词标明「跟着用、不用刻意学」",
          "跟着用" in p or "日常语言" in p,
          "如果这句变回「别学着说」，模型会把群里的网络语当外语躲开")

    print("\n== 记忆 ==")
    hits = bot.memory.recall("熬夜")
    check("recall 能搜到", bool(hits), str(hits[:1]))

    # 写入测试用临时目录，别把真记忆搞脏
    import tempfile
    real_dir = bot.memory.dir
    bot.memory.dir = tempfile.mkdtemp(prefix="pawbot-mem-")
    added = bot.memory.remember("自检写入：这是一条测试记忆")
    check("remember 能写", added)
    hit2 = bot.memory.recall("测试记忆")
    check("写完能搜回来", any("自检写入" in h for h in hit2))
    dup = bot.memory.remember("自检写入：这是一条测试记忆")
    check("重复内容会去重", not dup)
    bot.memory.dir = real_dir

    print("\n== 表情包 ==")
    files = bot.stickers.files()
    check("表情包库非空", len(files) > 0, f"{len(files)} 张")
    picked = bot.stickers.pick("开心")
    picked_tags = (bot.stickers.index().get(os.path.basename(picked or "")) or {}).get("tags") or []
    check("按标签能挑到", picked is not None and "开心" in picked_tags, str(picked))
    seg = bot.stickers.to_segment(picked) if picked else None
    check("能转成 image 段", bool(seg and seg["data"]["file"].startswith("base64://")))
    again = bot.stickers.pick("开心", exclude={os.path.basename(picked or "")})
    check("同标签重复挑也能拿到", again is not None, str(again))

    print("\n== 工具 ==")
    names = bot.toolbox.available()
    check("工具已注册", "web_search" in names and "pick_sticker" in names, str(names))
    check("schema 合法", all("function" in s for s in bot.toolbox.schemas()))

    ctx = make_ctx(bot)
    result, _ = await bot.toolbox.execute(ctx, "recall_memory", {"query": "熬夜"})
    check("recall_memory 可执行", "记忆" in result, result.replace("\n", " ")[:40])

    ctx2 = make_ctx(bot)
    result, media = await bot.toolbox.execute(ctx2, "pick_sticker", {"query": "开心"})
    check("pick_sticker 返回媒体段", len(media) == 1, f"{len(media)} 段")

    print("\n== 工具循环（假模型）==")
    ctx3 = make_ctx(bot)
    calls = {"n": 0}

    async def fake_chat_message(messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "pick_sticker", "arguments": '{"query":"开心"}'},
            }]}
        return {"role": "assistant", "content": "会用。刚才那张图就是证据。"}

    original = bot.llm.chat_message
    bot.llm.chat_message = fake_chat_message
    try:
        answer, media = await bot.agent.run(ctx3, "你会用工具吗", [])
    finally:
        bot.llm.chat_message = original

    check("模型回答了", bool(answer), answer[:40])
    check("工具调用被接住", len(media) == 1, f"{len(media)} 段媒体")
    check("循环轮数正确", calls["n"] == 2, f"{calls['n']} 次调用")

    print("\n== 引用 / @ ==")
    ctx4 = make_ctx(bot)
    await ctx4.reply("测试一句话")
    types = [s["type"] for s in ctx4.captured[0]]
    check("auto：引用在最前", types[0] == "reply", str(types))
    check("auto：@ 在正文前", types[1:3] == ["at", "text"], str(types))
    check("auto：正文还在", types[-1] == "text" and len(ctx4.captured[0]) == 4, str(types))

    bot.config.data["reply"]["at_sender"] = "off"
    bot.config.data["reply"]["quote"] = "off"
    ctx5 = make_ctx(bot)
    await ctx5.reply("测试")
    check("off：只有正文", [s["type"] for s in ctx5.captured[0]] == ["text"],
          str([s["type"] for s in ctx5.captured[0]]))

    print("\n== 分条（只按 ‖，保留引用和 @）==")
    long_text = "".join("第%d句话在这里测试长度。" % i for i in range(1, 30))
    cases = {
        "纯正文": [msg.Text(long_text)],
        "引用+正文": [msg.Reply("1"), msg.Text(long_text)],
        "引用+@+正文": [msg.Reply("1"), msg.At("555"),
                            msg.Text(" "), msg.Text(long_text)],
    }
    for name, segs in cases.items():
        parts = bot.safety._split(segs)
        check(f"{name} 无标记时保持一条", len(parts) == 1
              and msg.to_plain(parts[0]).endswith(long_text))
    quoted = bot.safety._split([
        msg.Reply("1"), msg.At("555"), msg.Text(" "),
        msg.Text("第一句说完。‖第二句继续！")])
    check("引用与 @ 只挂在第一条上",
          [s["type"] for s in quoted[0]][:2] == ["reply", "at"]
          and [s["type"] for s in quoted[1]] == ["text"])
    marked = bot.safety._split([msg.Text("第一句说完。‖第二句继续！")])
    marked_texts = [msg.to_plain(part) for part in marked]
    check("‖ 标记拆成两条", marked_texts == ["第一句说完。", "第二句继续！"],
          str(marked_texts))
    check("分段标记不会发到 QQ", all("‖" not in t for t in marked_texts))
    code = "a || b"
    double_marked = bot.safety._split([msg.Text(code)])
    check("代码中的 || 原样保留", len(double_marked) == 1
          and msg.to_plain(double_marked[0]) == code)
    too_many = bot.safety._split([msg.Text("一。‖二。‖三。‖四。‖五。")])
    check("超出上限的内容并入最后一条而不丢失",
          len(too_many) <= min(3, max(1, int(bot.config.get("safety.burst", 3))))
          and "".join(msg.to_plain(p) for p in too_many).replace("\n", "")
          == "一。二。三。四。五。")
    check("纯标记不会生成空消息", not bot.safety._split([msg.Text("‖‖")]))

    class EmptyReceiptConn:
        async def send_message(self, *args, **kwargs):
            return {}

    safety = bot.safety
    original_take = safety._take_token
    original_quiet = safety._in_quiet_hours
    original_delay = safety._think_delay
    try:
        safety._in_quiet_hours = lambda: False
        safety._think_delay = lambda _parts: 0
        safety._take_token = lambda _key, _n=1: False
        suppressed = await safety.send(EmptyReceiptConn(), "safety_test", [msg.Text("test")],
                                       group_id=1)
        safety._take_token = lambda _key, _n=1: True
        sent_empty_receipt = await safety.send(
            EmptyReceiptConn(), "safety_test", [msg.Text("test")], group_id=1)
    finally:
        safety._take_token = original_take
        safety._in_quiet_hours = original_quiet
        safety._think_delay = original_delay
    check("限流明确返回未发送", suppressed is None)
    check("空 OneBot 回执仍视为成功", isinstance(sent_empty_receipt, dict)
          and sent_empty_receipt == {})

    print("\n== 空间 ==")
    ok, why = bot.qzone.check_permission(make_ctx(bot))
    check("默认拒绝发空间", not ok, why)

    print("\n== 地址解析 ==")
    from pawbot.websearch import _unwrap_bing
    wrapped = ("https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9h"
               "P2I9MQ&ntb=1")
    check("Bing 跳转壳能解开", _unwrap_bing(wrapped).startswith("https://example.com"),
          _unwrap_bing(wrapped))

    print("\n== 找图 ==")
    from pawbot import imagesearch
    from pawbot.tools import SCHEMAS
    check("search_image 无需 key 也在工具列表里",
          "search_image" in bot.toolbox.available(),
          str(bot.toolbox.available()))
    props = SCHEMAS["search_image"]["function"]["parameters"]["properties"]
    check("search_image 有 n 参数", "n" in props)
    check("search_image 能指定图源", "source" in props, str(list(props)))
    check("图源选项含 pixiv",
          "pixiv" in props.get("source", {}).get("enum", []),
          str(props.get("source", {}).get("enum")))

    # 全网搜：搜狗 / 360
    out = await asyncio.get_event_loop().run_in_executor(
        None, lambda: imagesearch.image_search("猫", bot.config, 2, "web", 0))
    imgs = out.get("images") or []
    check("全网能搜到图", len(imgs) >= 1,
          f"provider={out.get('provider')} n={len(imgs)} err={out.get('error')}")
    check("图片都是 http(s) 直链",
          all(str(x.get("url", "")).startswith(("http://", "https://")) for x in imgs))
    check("结果不是占位图",
          all("placeholder" not in x["url"].lower() for x in imgs))
    check("全网源不是内置图库",
          all(x.get("source") not in ("openverse", "wikimedia") for x in imgs),
          str([x.get("source") for x in imgs]))

    # Pixiv 链路单元验证使用合成结果；真实登录/网络另跑诊断脚本。
    # 这两段覆盖专用 Chrome 路径，默认已是 DS/免登录，这里显式打开浏览器开关。
    _browser_flag = bot.config.get("tools.pixiv_use_browser", False)
    bot.config.data["tools"]["pixiv_use_browser"] = True
    from unittest.mock import AsyncMock
    from pawbot.pixiv_session import PixivSession
    fake_pixiv = {"url": "https://i.pximg.net/img-original/test.jpg",
                  "rating": "sfw", "illust_id": "123", "source": "pixiv",
                  "page": "https://www.pixiv.net/artworks/123", "tags": "風景",
                  "width": 1600, "height": 1200, "bookmark_count": 1500,
                  "need_referer": True, "referer": imagesearch.PIXIV_REFERER}
    with patch.object(PixivSession, "search_report", new=AsyncMock(return_value={
            "provider": "pixiv(浏览器)", "images": [fake_pixiv], "error": ""})),             patch.object(imagesearch, "judge_images",
                         new=AsyncMock(side_effect=lambda items, cfg: items)):
        px = await asyncio.get_event_loop().run_in_executor(
            None, lambda: imagesearch.image_search("風景", bot.config, 2, "pixiv", 0))
    pxi = px.get("images") or []
    check("Pixiv 专用搜索结果可传回工具", len(pxi) >= 1)
    check("Pixiv 结果带作品链接", all("pixiv.net/artworks" in x["page"] for x in pxi))
    check("Pixiv 标记了要 Referer", all(x.get("need_referer") for x in pxi))
    check("Pixiv 品质元数据保留", pxi[0]["bookmark_count"] == 1500
          and pxi[0]["width"] == 1600)
    import base64 as _b64
    with patch.object(imagesearch, "download", return_value=b"\xff\xd8\xfftest"):
        seg = imagesearch.to_segment(pxi[0])
    check("Pixiv 图转成了 base64 段", bool(seg and seg["data"]["file"].startswith("base64://")))
    check("base64 图片内容完整", _b64.b64decode(seg["data"]["file"][9:]) == b"\xff\xd8\xfftest")

    fake_web = {"url": "https://example.com/cat.jpg", "rating": "sfw", "title": "猫"}
    offline = (
        patch.object(imagesearch, "search_sogou", return_value=[fake_web]),
        patch.object(imagesearch, "search_360", return_value=[]),
        patch.object(imagesearch, "search_wallhaven", return_value=[]),
        patch.object(imagesearch, "download", return_value=b"\xff\xd8\xffcat"),
        patch.object(imagesearch, "judge_images",
                     new=AsyncMock(side_effect=lambda items, cfg: items)),
    )
    for item in offline:
        item.start()
        ctx_cleanup = getattr(bot, "_selftest_cleanup", [])
    txt, segs = await bot.toolbox.execute(make_ctx(bot), "search_image",
                                          {"query": "猫", "n": 2})
    check("工具返回单独下发的图片标记",
          bool(segs) and all(x["type"] == "text" and x["data"]["text"].startswith("[IMG:")
                             for x in segs),
          f"{len(segs)} 段 / {txt[:60]}")
    for item in offline:
        item.stop()
    with patch.object(PixivSession, "home_report", new=AsyncMock(return_value={
            "provider": "pixiv(浏览器)", "images": [fake_pixiv], "error": ""})), \
            patch.object(imagesearch, "download", return_value=b"\xff\xd8\xfftest"), \
            patch.object(imagesearch, "judge_images",
                         new=AsyncMock(side_effect=lambda items, cfg: items)):
        empty = await bot.toolbox.execute(make_ctx(bot), "search_image", {"query": ""})
    check("空关键词走主页不带标签", isinstance(empty[0], str) and "单独一条消息" in empty[0],
          empty[0][:50])
    bot.config.data["tools"]["pixiv_use_browser"] = _browser_flag

    print("\n== 感知层 ==")
    from pawbot import perceive, vision
    from pawbot.event import MessageEvent

    class _E(MessageEvent):
        pass

    e = _E(post_type="message", message_type="group", group_id=1, user_id=2,
           self_id=99, message_id=5)
    e.quoted_text = "他昨天说要去上海"
    e.image_desc = "[图片：一只虎斑猫]"
    e.person_card = "爱问技术问题"
    rendered = perceive.render(e)
    check("引用内容进得了上下文", "上海" in rendered, rendered[:60])
    check("看图结果进得了上下文", "虎斑猫" in rendered)
    check("没有 key 时不炸", vision.describe([{"type": "image", "data": {}}],
                                            "", "http://x", "m") == "")

    print("\n== 读空气 ==")
    from pawbot.topic import Mood, TopicTracker
    tt = TopicTracker()
    sid_t = "group_test"
    for who, txt in [("阿明", "今天数学考试好难"), ("小红", "数学第三题不会"),
                     ("阿明", "数学老师说可能要重考")]:
        tt.seen(sid_t, who, txt)
    kws = tt.keywords(sid_t)
    check("能抽出话题词", bool(kws), str(kws[:4]))
    check("高频词排在前面", kws and kws[0] == "数学", str(kws[:3]))
    solid = tt.solid_keywords(sid_t)
    check("搜索只用稳固词", solid == ["数学"], str(solid))
    check("摘要能拿到", "数学" in tt.digest(sid_t))
    ok_c, why_c = tt.should_chime_in("group_test", {
        "topic.min_idle": 120, "topic.chime_cooldown": 300,
        "topic.busy_threshold": 12})
    check("刚有人说话时不插嘴", not ok_c, why_c)
    tt._node(sid_t)["last_human_at"] -= 9999
    ok_c2, why_c2 = tt.should_chime_in(sid_t, {
        "topic.min_idle": 120, "topic.chime_cooldown": 300,
        "topic.busy_threshold": 12})
    check("冷场后会想说话", ok_c2, why_c2)
    tt.mark_chime(sid_t)
    ok_c3, why_c3 = tt.should_chime_in(sid_t, {
        "topic.min_idle": 0, "topic.chime_cooldown": 300,
        "topic.busy_threshold": 12})
    check("冷却期内闭嘴", not ok_c3, why_c3)

    m = Mood()
    check("心情有标签", bool(m.label), m.label)
    m.score = 5
    check("低落时语气变短", "懒得" in m.hint(), m.hint())

    print("\n== 记忆压缩 ==")
    import tempfile
    from pawbot.store import Store
    st = Store(os.path.join(tempfile.mkdtemp(), "s.json"), max_turns=2,
               max_messages=4)
    for i in range(60):
        st.append("g", "user", f"第{i}句")
    check("溢出进 archive 不丢", st.archive_size("g") > 0, str(st.archive_size("g")))
    check("攒够了要压缩", st.needs_compress("g"))
    rows = st.take_archive("g")
    check("取走后清空", st.archive_size("g") == 0)
    st.restore_archive("g", rows)
    check("压缩失败能还原", st.archive_size("g") == len(rows))
    st.set_summary("g", "他们聊了数学")
    check("摘要存得下", st.summary("g") == "他们聊了数学")

    print("\n== B 站 ==")
    from pawbot import bilibili as bl
    check("播放数格式化", bl._fmt_play(123456) == "12.3万", bl._fmt_play(123456))
    check("时长解析 mm:ss", bl._dur("12:34") == 754, str(bl._dur("12:34")))
    check("时长解析秒", bl._dur(90) == 90)
    fake = {"bvid": "BV1xx", "title": "测试视频", "author": "UP主",
            "url": "https://www.bilibili.com/video/BV1xx", "pic": "", "play": 1000}
    card = bl.card(fake)
    check("卡片结构对", card["type"] == "json" and "BV1xx" in card["data"]["data"])
    check("brief 带链接", "BV1xx" in bl.brief(fake))

    print("\n== 活跃度自适应 ==")
    import time as _t

    def _mk(gaps):
        tk = TopicTracker()
        s = "group_act"
        for g in gaps:
            tk._node(s)["gaps"].append(g)
        return tk, s

    dead, s1 = _mk([1800] * 10)
    live, s2 = _mk([20] * 10)
    mid, s3 = _mk([180] * 10)
    lo_d, hi_d, info_d = dead.adaptive_span(s1, 900, 2400)
    lo_l, hi_l, info_l = live.adaptive_span(s2, 900, 2400)
    lo_m, hi_m, _ = mid.adaptive_span(s3, 900, 2400)
    check("死群算得出低活跃", info_d["level"] < 0.2, f"{info_d['level']:.2f}")
    check("热闹群算得出高活跃", info_l["level"] > 0.8, f"{info_l['level']:.2f}")
    check("死群阈值更短", lo_d < lo_m < lo_l,
          f"{lo_d:.0f} < {lo_m:.0f} < {lo_l:.0f}")
    check("热闹群阈值更长", hi_l > hi_m > hi_d,
          f"{hi_l:.0f} > {hi_m:.0f} > {hi_d:.0f}")
    check("区间仍然有效", hi_d > lo_d and hi_m > lo_m and hi_l > lo_l)

    few, s4 = _mk([100])
    lo_f, hi_f, info_f = few.adaptive_span(s4, 900, 2400)
    check("样本不足不乱猜", not info_f["known"] and 0.3 < info_f["level"] < 0.7,
          f"level={info_f['level']} n={info_f['samples']}")

    # 交易间隔实录：seen() 要真的把间隔记下来
    tk2 = TopicTracker()
    base = _t.time()
    for i in range(4):
        tk2.seen("gx", "阿明", f"第{i}句")
        tk2._node("gx")["last_human_at"] -= 120     # 假装上一句是 2 分钟前
    check("seen 会记录说话间隔", len(tk2._node("gx")["gaps"]) >= 2,
          str(list(tk2._node("gx")["gaps"])[:3]))

    # 持久化：重启后活跃度不能归零，否则自适应永远在猜
    tpath = os.path.join(tempfile.mkdtemp(), "topic.json")
    tk3 = TopicTracker(path=tpath)
    for g in [1800] * 6:
        tk3._node("g_keep")["gaps"].append(g)
    tk3._node("g_keep")["last_human_at"] = _t.time() - 60
    tk3.save(force=True)
    tk4 = TopicTracker(path=tpath)
    act4 = tk4.activity("g_keep")
    check("节奏能落盘并恢复", act4["known"] and abs(act4["median_gap"] - 1800) < 1,
          f"known={act4['known']} gap={act4['median_gap']}")
    lo_keep, _, _ = tk4.adaptive_span("g_keep", 900, 2400)
    check("恢复后阈值仍按死群算", lo_keep < 900, f"{lo_keep:.0f}s")

    clear_dir = tempfile.mkdtemp(prefix="pawbot-clear-")
    clear_sid = "group_clear_test"
    from pawbot.store import Store
    clear_store = Store(os.path.join(clear_dir, "sessions.json"))
    clear_store.append(clear_sid, "user", "旧聊天")
    clear_topic_path = os.path.join(clear_dir, "topic.json")
    clear_topic = TopicTracker(path=clear_topic_path)
    clear_topic.seen(clear_sid, "阿明", "旧话题内容")
    clear_topic._node(clear_sid)["gaps"].append(30)
    clear_topic.mark_chime(clear_sid)
    clear_topic.save(force=True)
    bot_stub = object.__new__(Bot)
    bot_stub.store = clear_store
    bot_stub.topic = clear_topic
    bot_stub.inbound = {clear_sid: 4}
    bot_stub.last_msg_id = {clear_sid: 99}
    bot_stub.last_msg_text = {clear_sid: "旧回复"}
    bot_stub.clear_session(clear_sid)
    clear_topic_reloaded = TopicTracker(path=clear_topic_path)
    check("会话清除同时移除聊天与话题状态",
          not clear_store.history(clear_sid) and clear_sid not in clear_topic.groups
          and clear_sid not in clear_topic_reloaded.groups)
    check("会话清除也移除短期状态",
          clear_sid not in bot_stub.inbound and clear_sid not in bot_stub.last_msg_id
          and clear_sid not in bot_stub.last_msg_text)

    print("\n== 主动行为 ==")
    from pawbot.proactive import Proactive
    pr = Proactive(bot)
    check("按配置识别活跃群", 987654321 in pr._groups(), str(pr._groups()))
    lo, hi = pr._span()
    check("冷场阈值是区间", hi > lo, f"{lo}~{hi}")
    n1 = pr._roll("group_x")
    check("下次到点在未来", n1 > __import__("time").time())
    pr._fail_streak["group_x"] = 3
    n2 = pr._roll("group_x")
    check("连续失败会退避", n2 > n1)
    act = pr._fun_search([])
    check("找乐子有方向", act["kind"] == "search" and bool(act["query"]))

    print("\n== 人物档案 ==")
    from pawbot.people import PersonBook
    pb_dir = tempfile.mkdtemp()
    pb = PersonBook(BASE, {"people.dir": pb_dir})
    check("一开始没档案", not pb.has(123456))
    okp = pb.remember(123456, "阿明", "说话方式：短句多，爱用「草」")
    check("能记进个人档案", okp and pb.has(123456))
    check("档案按 QQ 号分文件", os.path.exists(os.path.join(pb_dir, "123456.md")))
    check("两个人的档案互不干扰",
          not pb.has(999) and not pb.card(999))
    pb.set_section(123456, "阿明", "性格", "嘴硬")
    pb.set_section(123456, "阿明", "性格", "嘴硬但人不坏")
    card = pb.card(123456)
    check("同项覆盖而不是堆叠", card.count("性格：") == 1, card[:80])
    check("覆盖后内容是最新的", "嘴硬但人不坏" in card, card[:80])
    check("档案带着名字", "名字：阿明" in card, card[:60])
    # 字面形式（口头禅）不能进 prompt，否则模型会照着复读
    pb.set_section(123456, "阿明", "说话方式", "短句多，爱用「草」开头")
    full = pb.card(123456)
    check("档案里保留字面记录", "「草」" in full, full[:120])
    prompt_card = pb.card_for_prompt(123456)
    check("注入 prompt 的不含字面", "草" not in prompt_card, prompt_card[:120])
    check("同一行的其他分句保留", "短句多" in prompt_card, prompt_card[:120])
    # observe / should_update
    for _ in range(29):
        pb.observe(123456)
    check("没攒够不提炼", not pb.should_update(123456))
    pb.observe(123456)
    check("攒够了才提炼", pb.should_update(123456))
    pb.mark_updated(123456)
    check("提炼后要等冷却", not pb.should_update(123456))

    print("\n== 时间加权上下文 ==")
    from pawbot import ctxwin
    import time
    now = time.time()
    rows = []
    for i in range(300):
        # 越靠前越久远：0 是 20 小时前，299 是刚才
        age = (299 - i) * 240          # 每条隔 4 分钟
        if i < 10:
            # 老消息是长篇大论（会被截短），新消息是随手一句
            body = f"第{i}条消息" + "，这人在长篇大论" * 12
        else:
            body = f"第{i}条消息，随便聊聊"
        rows.append({"role": "user", "content": body,
                     "who": "阿明", "ts": now - age})
    cfg_c = {"chat.context_messages": 300, "chat.time_decay_hours": 6,
             "chat.context_chars": 12000, "chat.gap_mark_minutes": 10}
    text, stats = ctxwin.build(rows, cfg_c)
    check("取满 300 条", stats["lines"] == 300, str(stats["lines"]))
    check("权重随时间衰减", stats["oldest_weight"] < 0.05 < stats["newest_weight"],
          f"{stats['oldest_weight']} ~ {stats['newest_weight']}")
    # 2026-10-24：每一行现在都带时间前缀，所以「比原文短」这条总长度判据不再可靠
    # （前缀本身也占字数）。改成两条更准的：① 老消息确实被截短 ② 每一行都带时间。
    body_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("——")]
    stamped = [ln for ln in body_lines
               if re.match(r"^\[\d{1,2}-\d{1,2} \d{1,2}:\d{2}\] ", ln)
               or re.match(r"^\[\d{1,2}:\d{2}\] ", ln)]
    check("每一行都带时间发问人前缀", len(stamped) == len(body_lines),
          f"{len(stamped)}/{len(body_lines)} 行带前缀")
    # 近的完整保留
    check("最近那条完整保留", "第299条消息，随便聊聊" in text, text[-120:])
    # 远的长消息被砍短：那一行明显短于原文
    line0 = next((ln for ln in text.splitlines() if "第0条消息" in ln), "")
    check("最老那条被砍短", 0 < len(line0) < 80, f"{len(line0)} 字: {line0[:50]}")
    check("间隔够久才有标记", text.count("——") <= 2, f"{text.count('——')} 个标记")
    # 预算裁剪
    tight = dict(cfg_c); tight["chat.context_chars"] = 2000
    t2, s2 = ctxwin.build(rows, tight)
    check("超预算会丢老的", s2["dropped"] > 0 and s2["chars"] <= 2000,
          f"dropped={s2['dropped']} chars={s2['chars']}")
    check("裁剪后仍保留最近的", "第299条消息" in t2, t2[-100:])
    check("裁剪后有省略说明", "省略" in t2, t2[:60])
    # 权重函数本身
    check("权重是指数衰减",
          abs(ctxwin.weight(6 * 3600, 6 * 3600) - 0.3679) < 0.01,
          str(ctxwin.weight(6 * 3600, 6 * 3600)))
    check("1 小时前权重还有 0.85",
          abs(ctxwin.weight(3600, 6 * 3600) - 0.8465) < 0.01,
          str(ctxwin.weight(3600, 6 * 3600)))

    print("\n== 尺度档位 ==")
    from pawbot.spice import Spice
    sp_path = os.path.join(tempfile.mkdtemp(), "spice.json")
    sp = Spice(sp_path, default=0)
    check("默认是素聊", sp.get("g1") == 0)
    check("素聊的提示是「别主动提」", "别主动提" in sp.hint("g1"), sp.hint("g1")[:40])

    class _FakeCtx:
        def __init__(self, sid):
            self.session_id = sid

    bot.spice = sp
    check("0 档时 agent 根本不注入尺度提示",
          bot.agent._spice_hint(_FakeCtx("g1")) == "",
          repr(bot.agent._spice_hint(_FakeCtx("g1")))[:60])
    sp.set("g1", 2)
    check("2 档时才注入", "这个群的调调" in bot.agent._spice_hint(_FakeCtx("g1")))
    sp.set("g1", 2)
    check("能调到 2 档", sp.get("g1") == 2)
    check("2 档提示里说能撩", "撩" in sp.hint("g1"), sp.hint("g1")[:40])
    sp.set("g1", 99)
    check("越界会夹紧", sp.get("g1") == 2, str(sp.get("g1")))
    sp.set("g2", 1)
    check("不同群互不影响", sp.get("g1") == 2 and sp.get("g2") == 1)
    check("落盘了", os.path.exists(sp_path))
    sp2 = Spice(sp_path, default=0)
    check("重载后还在", sp2.get("g1") == 2, str(sp2.get("g1")))

    print("\n== 找图分级 / 底线 ==")
    sfw = {"rating": "sfw", "tags": "猫", "title": "a"}
    r18 = {"rating": "r18", "tags": "原创, 女の子", "title": "b"}
    nsfw = {"rating": "nsfw", "tags": "", "title": "c"}
    minor = {"rating": "r18", "tags": "ロリ, 原创", "title": "d"}
    check("0 档：SFW 放行", imagesearch._allowed(sfw, False))
    check("0 档：R-18 / nsfw 都挡", not imagesearch._allowed(r18, False)
          and not imagesearch._allowed(nsfw, False))
    check("1 档：R-18 放行", imagesearch._allowed(r18, True))
    check("1 档：带未成年标签的 R-18 照样挡", not imagesearch._allowed(minor, True))
    check("查询词沾边能识别", imagesearch.mentions_minor("找张萝莉色图")
          and imagesearch.mentions_minor("LOLI") and not imagesearch.mentions_minor("猫娘"))
    # 词表被整块覆盖过一次（只剩 "福瑞"），这条守着别再发生
    check("底线词表没被清空", len(imagesearch._MINOR_TERMS) >= 20,
          f"只有 {len(imagesearch._MINOR_TERMS)} 条：{imagesearch._MINOR_TERMS}")
    check("底线词表覆盖中英日三种写法",
          all(imagesearch.mentions_minor(x) for x in
              ("萝莉", "ロリ", "loli", "幼女", "未成年")),
          str([x for x in ("萝莉", "ロリ", "loli", "幼女", "未成年")
               if not imagesearch.mentions_minor(x)]))
    # 查询词沾边要真的把 R18 降掉（原来那行是短路，等于没做）
    class _CfgStub:                     # _Cfg 定义在后面的段落，这里先给个默认全开的壳
        def get(self, k, d=None):
            return d

    _seen2 = {}

    def _no_sogou(q, n):
        return []

    def _wh(q, n, a):
        _seen2["r18"] = a
        return []

    _so, _o3, _ow = (imagesearch.search_sogou, imagesearch.search_360,
                     imagesearch.search_wallhaven)
    try:
        imagesearch.search_sogou = _no_sogou
        imagesearch.search_360 = _no_sogou
        imagesearch.search_wallhaven = _wh
        imagesearch.image_search("萝莉 插画", _CfgStub(), 1, "web", 2, True)
        check("查询词沾边 → 强制降为 SFW", _seen2.get("r18") is False,
              str(_seen2.get("r18")))
        _seen2.clear()
        _refused = imagesearch.image_search("萝莉 色图", _CfgStub(), 1, "web", 2, True)
        check("未成年 + 性化词 → 直接不搜",
              not _refused["images"] and "不提供" in _refused["error"]
              and _seen2.get("r18") is None, _refused["error"])
        _seen2.clear()
        imagesearch.image_search("猫 色图", _CfgStub(), 1, "web", 2, True)
        check("查询词干净 → 仍按 explicit 放行", _seen2.get("r18") is True,
              str(_seen2.get("r18")))
    finally:
        imagesearch.search_sogou, imagesearch.search_360, \
            imagesearch.search_wallhaven = _so, _o3, _ow
    check("Pixiv 允许 R-18 时用 mode=all",
          "mode=all" in imagesearch.pixiv_search_url("猫", True))
    check("Pixiv 不允许时用 mode=safe",
          "mode=safe" in imagesearch.pixiv_search_url("猫", False))

    class _Cfg(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    _orig = imagesearch.search_sogou, imagesearch.search_360, imagesearch.search_wallhaven
    seen = {}
    try:
        imagesearch.search_sogou = lambda q, n: [dict(r18), dict(nsfw)]
        imagesearch.search_360 = lambda q, n: [dict(sfw)]
        imagesearch.search_wallhaven = lambda q, n, a: seen.setdefault("wh", a) and []
        res = imagesearch.image_search("猫", _Cfg(), 2, "web", 0)
        check("0 档：全是 R-18 的图源会被跳过，换下一个",
              res["provider"] == "360" and res["images"] == [sfw], res["provider"])
        imagesearch.search_360 = lambda q, n: []
        res = imagesearch.image_search("猫", _Cfg(), 2, "web", 0)
        check("0 档：只剩 R-18 时宁可不发", res["images"] == [], str(res["images"])[:60])
        imagesearch.search_wallhaven = lambda q, n, a: seen.__setitem__("wh", a) or []
        imagesearch.image_search("萝莉", _Cfg(), 2, "web", 2)
        check("2 档 + 查询词沾边：下游拿到的是不许 R-18", seen.get("wh") is False,
              str(seen.get("wh")))
    finally:
        imagesearch.search_sogou, imagesearch.search_360, imagesearch.search_wallhaven = _orig

    from pawbot import pixiv_session as ps

    print("\n== 專用 Chrome 啟動（假進程，不碰真瀏覽器）==")
    _orig_chrome, _orig_profile = ps.CHROME_EXE, ps.CHROME_PROFILE
    _orig_isfile, _orig_makedirs = ps.os.path.isfile, ps.os.makedirs
    _orig_spawn, _orig_run = ps._spawn_detached, ps.subprocess.run

    class _FakeChrome(ps.PixivSession):
        port_wait = 0.02
        kill_poll = 0.001

        def __init__(self, browser="", profile=False):
            super().__init__()
            self.browser, self.profile = browser, profile

        def cdp_browser(self):
            return self.browser

        def chrome_profile_running(self):
            return self.profile

    chrome_args, process_calls = [], []

    def _chrome_spawn(args):
        chrome_args.append(args)
        fake.browser = "Chrome/155.0"
        fake.profile = True

    def _record_process_call(*args, **kwargs):
        process_calls.append((args, kwargs))

    try:
        ps.CHROME_EXE = r"C:\fake\chrome.exe"
        ps.CHROME_PROFILE = ps.os.path.join(ps.PROJECT_ROOT, "data", "browser", "fake-profile")
        ps.os.path.isfile = lambda path: path == ps.CHROME_EXE
        ps.os.makedirs = lambda *a, **k: None
        ps._spawn_detached = _chrome_spawn
        ps.subprocess.run = _record_process_call

        fake = _FakeChrome()
        ok = fake.launch_chrome("https://www.pixiv.net/")
        check("专用 Chrome：启动参数含隔离 profile、loopback CDP 和 Pixiv URL",
              ok and f"--user-data-dir={ps.CHROME_PROFILE}" in chrome_args[-1]
              and "--remote-debugging-port=9222" in chrome_args[-1]
              and "--remote-debugging-address=127.0.0.1" in chrome_args[-1]
              and chrome_args[-1][-1] == "https://www.pixiv.net/")
        check("专用 Chrome：启动不调用 taskkill、PowerShell 或注册表策略",
              not process_calls)

        def _profile_query(*args, **kwargs):
            process_calls.append((args, kwargs))
            return ps.subprocess.CompletedProcess(args[0], 0, "yes\r\n", "")

        ps.subprocess.run = _profile_query
        profile_ok = ps.PixivSession().chrome_profile_running()
        profile_command = process_calls[-1][0][0][-1]
        check("专用 Chrome：归属查询使用原生 Windows profile 路径和端口",
              profile_ok and ps.CHROME_PROFILE.lower() in profile_command.lower()
              and "--remote-debugging-port=9222" in profile_command
              and "OrdinalIgnoreCase" in profile_command)

        chrome_args.clear()
        # 登录状态仅由 Pixiv 同源页面可读状态判定，绝不以 R18 查询探测。
        original_status = ps.PixivSession.status

        async def _fake_status_logged_in(self):
            return {"ready": True, "page_on_pixiv": True, "logged_in": True, "error": ""}

        async def _fake_status_anonymous(self):
            return {"ready": True, "page_on_pixiv": True, "logged_in": False, "error": ""}

        ps.PixivSession.status = _fake_status_logged_in
        try:
            is_logged = await ps.PixivSession().logged_in()
        finally:
            ps.PixivSession.status = _fake_status_anonymous
        try:
            is_anonymous = await ps.PixivSession().logged_in()
        finally:
            ps.PixivSession.status = original_status
        check("登录判据使用同源页面状态，不搜索成人内容", is_logged and not is_anonymous)

        fake = _FakeChrome(browser="Chrome/155.0", profile=False)
        check("专用 Chrome：不接管其他 Chrome profile",
              not fake.launch_chrome() and not chrome_args)
        fake = _FakeChrome(browser="Microsoft Edge/155.0", profile=False)
        check("专用 Chrome：拒绝非 Chrome 的 CDP 端口占用",
              not fake.launch_chrome() and not chrome_args)

        ps.CHROME_EXE = ""
        check("专用 Chrome：找不到浏览器时安全失败", not _FakeChrome().launch_chrome())

        ps.CHROME_EXE = r"C:\fake\chrome.exe"
        ps._spawn_detached = lambda args: None
        fake = _FakeChrome()
        check("专用 Chrome：端口启动超时后失败", not fake.launch_chrome())
    finally:
        ps.CHROME_EXE, ps.CHROME_PROFILE = _orig_chrome, _orig_profile
        ps.os.path.isfile, ps.os.makedirs = _orig_isfile, _orig_makedirs
        ps._spawn_detached, ps.subprocess.run = _orig_spawn, _orig_run

    print("\n== 找图两档尺度（默认擦边 / 明说要 R18 才给）==")
    _default_says = ["找些色图", "来点涩图", "找点擦边的", "来张性感的",
                     "不露点的色图", "不露但很色的", "来张图看看"]
    _r18_says = ["找张R18的图", "有没有18+的", "来点R-18的", "十八禁的图",
                 "找点露点的", "无修正的来一张"]
    check("默认说法不会被当成要 R18",
          not any(imagesearch.wants_explicit(t) for t in _default_says),
          str([t for t in _default_says if imagesearch.wants_explicit(t)]))
    check("明说要 R18 的说法能识别",
          all(imagesearch.wants_explicit(t) for t in _r18_says),
          str([t for t in _r18_says if not imagesearch.wants_explicit(t)]))
    check("否定句不算要 R18",
          not imagesearch.wants_explicit("不要R18")
          and not imagesearch.wants_explicit("别搞R18"))
    check("「有没有18+」是问句，算要",
          imagesearch.wants_explicit("有没有18+的"))
    # image_search 的 allow_r18 参数
    _orig_s, _orig_3, _orig_w = (imagesearch.search_sogou,
                                 imagesearch.search_360,
                                 imagesearch.search_wallhaven)
    _seen = {}
    try:
        def _fake_sogou(q, n):
            _seen["sogou"] = q
            return []

        def _fake_360(q, n):
            return []

        def _fake_wh(q, n, a):
            _seen["wh_r18"] = a
            return []

        imagesearch.search_sogou = _fake_sogou
        imagesearch.search_360 = _fake_360
        imagesearch.search_wallhaven = _fake_wh
        imagesearch.image_search("test", _Cfg(), 1, "web", 2, False)
        check("allow_r18=False 时下游拿到不许 R18", _seen.get("wh_r18") is False,
              str(_seen.get("wh_r18")))
        _seen.clear()
        imagesearch.image_search("test", _Cfg(), 1, "web", 2, True)
        check("allow_r18=True 时下游才允许 R18", _seen.get("wh_r18") is True,
              str(_seen.get("wh_r18")))
        _seen.clear()
        imagesearch.image_search("test", _Cfg(), 1, "web", 0, True)
        check("0 档即使传 allow_r18 也不给 R18", _seen.get("wh_r18") is False,
              str(_seen.get("wh_r18")))
    finally:
        imagesearch.search_sogou, imagesearch.search_360, \
            imagesearch.search_wallhaven = _orig_s, _orig_3, _orig_w

    print("\n== NSFW 打码 ==")
    # 打码改成「先定位露点再盖」，不再是固定比例
    ev = imagesearch._CENSOR_EVAL
    check("打码用肤色行检测，不是固定比例",
          "isSkin" in ev and "rows[y]" in ev and "30" not in ev.split("y0")[0][:400],
          "找不到肤色检测逻辑")
    check("打码会跳过脸部那一段", "SH * 0.42" in ev)
    check("打码兜底时盖到图底", "b = SH - 1" in ev)
    check("有视觉框就用视觉框", "boxIn" in ev and "boxIn[0]" in ev)
    check("视觉定位结果要过合理性检查",
          "bot - top < 0.05" in io.open(
              imagesearch.__file__, encoding="utf-8").read())
    check("打码后复查残留肤色", "residual" in ev)
    check("支持 bar / crop / blur 三档",
          ("'crop'" in ev and "'blur'" in ev), "模式判断不全")

    check("配置默认开启 NSFW 打码", bot.config.get("tools.nsfw_censor") == "bar")
    check("R-18 / nsfw / sketchy 结果需要打码",
          imagesearch._needs_censor({"rating": "r18"})
          and imagesearch._needs_censor({"rating": "nsfw"})
          and imagesearch._needs_censor({"rating": "sketchy"}))
    check("SFW 结果不打码", not imagesearch._needs_censor({"rating": "sfw"}))

    _orig_dl, _orig_ci = imagesearch.download, imagesearch.censor_image
    censor_calls = []
    try:
        imagesearch.download = lambda item, timeout=25: b"\xff\xd8\xff" + b"x" * 32

        def _fake_censor(raw, mode="bar", box=None):
            censor_calls.append((mode, raw[:3]))
            return b"\xff\xd8\xffCENSORED", "image/jpeg"

        imagesearch.censor_image = _fake_censor
        seg = imagesearch.to_segment(
            {"url": "https://example.com/a.jpg", "rating": "r18",
             "need_referer": False})
        check("成人图（免下载源）也先打码再转 base64",
              bool(seg) and seg["data"]["file"].startswith("base64://")
              and censor_calls == [("bar", b"\xff\xd8\xff")], str(censor_calls))
        seg = imagesearch.to_segment(
            {"url": "https://example.com/a.jpg", "rating": "nsfw",
             "need_referer": False}, censor="crop")
        check("crop 模式会传给打码函数", bool(seg) and censor_calls[-1][0] == "crop")
        censor_calls.clear()
        seg = imagesearch.to_segment(
            {"url": "https://example.com/a.jpg", "rating": "sfw",
             "need_referer": False})
        check("SFW 图保持 URL 原样",
              bool(seg) and seg["data"]["file"] == "https://example.com/a.jpg"
              and not censor_calls)
        # 没盖干净（残留肤色行）→ 宁可不发
        imagesearch.censor_image = _orig_ci      # 换回真的实现
        _orig_cvc = imagesearch._censor_via_chrome
        _orig_local_req = imagesearch._local_req
        _orig_ready = imagesearch.PixivSession.chrome_cdp_ready \
            if hasattr(imagesearch, "PixivSession") else None
        try:
            import pawbot.pixiv_session as _psx
            _psx.PixivSession.chrome_cdp_ready = lambda self: True
            imagesearch._local_req = lambda url, method="GET", timeout=5.0: (
                {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/test"}
                if "/json/new" in url else {})
            async def _fake_cvc(port, path, data_url, mode, box=None):
                return (b"\xff\xd8\xffX", {"detected": 1, "residual": 12})
            imagesearch._censor_via_chrome = _fake_cvc
            _loop = asyncio.get_event_loop()
            # 真实现里用了 asyncio.run，必须放到线程里调（不能在运行中的循环里调）
            _r1 = await _loop.run_in_executor(
                None, lambda: imagesearch.censor_image(b"\xff\xd8\xffraw"))
            check("没盖干净宁可不发", _r1 is None)

            async def _fake_cvc2(port, path, data_url, mode, box=None):
                return (b"\xff\xd8\xffX", {"detected": 0, "residual": 0})
            imagesearch._censor_via_chrome = _fake_cvc2
            _r2 = await _loop.run_in_executor(
                None, lambda: imagesearch.censor_image(b"\xff\xd8\xffraw", "crop"))
            check("没检到露点时用兜底位置照常出图", _r2 is not None)
        finally:
            imagesearch._censor_via_chrome = _orig_cvc
            imagesearch._local_req = _orig_local_req
            imagesearch.censor_image = _orig_ci
            if _orig_ready is not None:
                _psx.PixivSession.chrome_cdp_ready = _orig_ready

        imagesearch.censor_image = lambda raw, mode="bar", box=None: None
        check("打码失败宁可不发",
              imagesearch.to_segment(
                  {"url": "https://example.com/a.jpg", "rating": "r18"}) is None)
        seg = imagesearch.to_segment(
            {"url": "https://example.com/a.jpg", "rating": "r18",
             "need_referer": False}, censor="off")
        check("censor=off 时不打码",
              bool(seg) and seg["data"]["file"] == "https://example.com/a.jpg")
    finally:
        imagesearch.download, imagesearch.censor_image = _orig_dl, _orig_ci

    # 「不碰日常浏览器」守卫：模块里不该再有 Edge 相关代码
    check("模块没有 Edge 残留（不碰用户日常浏览器）",
          not any(k in io.open(ps.__file__, encoding="utf-8").read()
                  for k in ("msedge", "EdgePolicy", "EDGE_EXE", "kill_edge", "launch_edge")),
          "pixiv_session.py 里还有 Edge 代码")

    print(f"\n完成。会话数 {len(bot.store.sessions())}")


if __name__ == "__main__":
    asyncio.run(main())
