#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预置梗 / 黑话知识库：data/memes.json。

每条都会先用 DeepSeek 官方搜索 + 萌娘百科查一次，确认能查到相关来源才写入；
查不到的跳过并在末尾列出来，不硬编。重复执行不会清掉已有的 hits。

    python tools/seed_memes.py

涉及真人（科比、蔡徐坤）的条目只写梗本身的出处和用法，不写侮辱性内容；
地狱梗只用一句带过，并在 usage 里注明慎用。
"""

from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pawbot import moegirl, websearch                      # noqa: E402
from pawbot.config import Config                          # noqa: E402
from pawbot.memes import MemeBook                         # noqa: E402

# name / triggers / meaning / usage / source 是最终写进库里的东西；
# query / moe / keys 是这次上网核对的线索。source 优先用我核对过的那一页，
# 若那次搜索没返回同一个站，就退到本次搜到的第一条相关结果。
ENTRIES = [
    {
        "name": "牢大",
        "triggers": ["牢大", "牢X"],
        "meaning": "科比·布莱恩特的黑称：粉丝叫他「老大」，他又曾卷入鹰郡事件被调侃「差点坐牢」，"
                   "合成「牢大」；后来「牢X」泛指翻车、被嘲的人物。",
        "usage": "看到有人拿旧事调侃时接梗；涉及真人、冒犯性强，慎用，容易冒犯人。",
        "query": "牢大 梗 科比 出处",
        "moe": "牢大",
        "keys": ["牢大", "牢X"],
        "preferred": "https://zh.moegirl.org.cn/牢大",
    },
    {
        "name": "曼巴",
        "triggers": ["曼巴", "曼巴精神", "曼巴运算", "man", "what can i say",
                     "肘击", "直升机"],
        "meaning": "科比的外号「黑曼巴」，曼巴精神指永不言弃；退役演讲"
                   "「Man! What can I say」被剪成梗，「肘击」「直升机」是地狱梗"
                   "（直升机源于 2020 年事故），冒犯性强。",
        "usage": "多用于自嘲；慎用，容易冒犯人。",
        "query": "曼巴精神 man what can i say 梗 出处",
        "moe": "曼巴",
        "keys": ["曼巴", "man", "mamba"],
        "preferred": "https://www.baike.com/wikiid/7374405049281953801",
    },
    {
        "name": "坤坤",
        "triggers": ["坤坤", "蔡徐坤"],
        "meaning": "蔡徐坤的昵称，源自《偶像练习生》的自我介绍和篮球舞片段被大量二创；"
                   "叫「坤坤」多带调侃意味。",
        "usage": "聊到相关鬼畜、篮球梗时用；别拿来嘲真人，慎用。",
        "query": "坤坤 蔡徐坤 梗 出处",
        "moe": "蔡徐坤",
        "keys": ["坤坤", "蔡徐坤"],
        "preferred": "https://zh.moegirl.org.cn/蔡徐坤",
    },
    {
        "name": "ikun",
        "triggers": ["ikun", "爱坤"],
        "meaning": "蔡徐坤粉丝的称呼「IKUN」，谐音「爱坤」；后来玩梗者常反串自称 ikun，"
                   "先猛夸再「露出鸡脚」。",
        "usage": "用于反串调侃，别真的吵架；慎用。",
        "query": "ikun 梗 意思 出处",
        "moe": "ikun",
        "keys": ["ikun", "爱坤"],
        "preferred": "https://baike.baidu.com/item/IKUN/8868238",
    },
    {
        "name": "小黑子",
        "triggers": ["小黑子", "露出鸡脚"],
        "meaning": "原指假装蔡徐坤粉丝、实际在黑人的网友，后来泛指反串玩梗的人；"
                   "配套说法是「露出鸡脚」（鸡脚＝破绽）。",
        "usage": "互相扣帽子玩梗时用，别对陌生人认真用；慎用。",
        "query": "小黑子 梗 意思 出处",
        "moe": "小黑子",
        "keys": ["小黑子", "鸡脚"],
        "preferred": "https://baike.baidu.com/item/小黑子/65407252",
    },
    {
        "name": "鸡你太美",
        "triggers": ["鸡你太美", "鸡哥"],
        "meaning": "歌曲《只因你太美》的空耳，出自蔡徐坤在《偶像练习生》的篮球舞自我介绍；"
                   "谐音「鸡」衍生出大量鬼畜二创。",
        "usage": "看到篮球、背带裤、鸡的画面时刷一句；带嘲讽意味，慎用。",
        "query": "鸡你太美 梗 出处",
        "moe": "鸡你太美",
        "keys": ["鸡你太美", "只因你太美"],
        "preferred": "https://zh.moegirl.org.cn/鸡你太美",
    },
    {
        "name": "只因",
        "triggers": ["只因"],
        "meaning": "原歌词「只因你太美」被空耳成「鸡你太美」，网友就用「只因」代指蔡徐坤或「鸡」，"
                   "是配套接梗词。",
        "usage": "看到「只因」接「你太美」就行；慎用。",
        "query": "只因 梗 蔡徐坤 出处",
        "moe": "只因",
        "keys": ["只因", "鸡你太美"],
        "preferred": "https://zh.wikipedia.org/zh-mo/蔡徐坤籃球影片事件",
    },
    {
        "name": "练习时长两年半",
        "triggers": ["练习时长两年半", "练习时长", "两年半"],
        "meaning": "蔡徐坤在《偶像练习生》自我介绍的原话「练习时长两年半的个人练习生蔡徐坤」，"
                   "因节奏魔性成梗。",
        "usage": "调侃别人练了很久、资历老时套用「练习时长X年半」；慎用。",
        "query": "练习时长两年半 梗 出处",
        "moe": "练习时长两年半",
        "keys": ["练习时长", "两年半"],
        "preferred": "https://ent.china.com/movie/newszh/11005281/20260818/49683231.html",
    },
    {
        "name": "你干嘛哎哟",
        "triggers": ["你干嘛哎哟", "干嘛哎哟"],
        "meaning": "蔡徐坤篮球舞视频里的台词「你干嘛～哎哟」，配合动作成为鬼畜名场面。",
        "usage": "有人做夸张动作或撒娇式抱怨时接一句；带调侃，慎用。",
        "query": "你干嘛哎哟 梗 出处",
        "moe": "你干嘛",
        "keys": ["你干嘛", "哎哟"],
        "preferred": "https://www.jinghooo.com/definition/403815",
    },
    {
        "name": "尊嘟假嘟",
        "triggers": ["尊嘟假嘟", "尊嘟", "假嘟"],
        "meaning": "「真的假的」的谐音，2023 年由微博博主「伯恩山bot」模仿宠物说话带火。",
        "usage": "装可爱、表示惊讶或不信时用；对不熟的人少用，容易显得幼态。",
        "query": "尊嘟假嘟 梗 意思 出处",
        "moe": "尊嘟假嘟",
        "keys": ["尊嘟假嘟"],
        "preferred": "https://baike.baidu.com/item/尊嘟假嘟/63289992",
    },
    {
        "name": "绝绝子",
        "triggers": ["绝绝子", "无语子"],
        "meaning": "出自百度贴吧「乡村天后吧」的「XX子」语缀玩法，把「子」当「了」用；"
                   "「绝绝子」＝太绝了，可夸可讽。",
        "usage": "夸人或反讽都行；现在偏过气，玩的时候带点自嘲。",
        "query": "绝绝子 梗 意思 出处",
        "moe": "绝绝子",
        "keys": ["绝绝子", "无语子"],
        "preferred": "https://baike.baidu.com/item/绝绝子/57918720",
    },
    {
        "name": "yyds",
        "triggers": ["yyds", "永远滴神", "永远的神"],
        "meaning": "「永远的神」的拼音缩写，出自英雄联盟主播山泥若夸 Uzi「乌兹，永远滴神」。",
        "usage": "夸人/物强到顶时用，别句句都 yyds。",
        "query": "yyds 梗 意思 出处 永远的神",
        "moe": "yyds",
        "keys": ["yyds", "永远滴神", "永远的神"],
        "preferred": "https://zh.moegirl.org.cn/Yyds",
    },
    {
        "name": "破防",
        "triggers": ["破防", "破防了"],
        "meaning": "原指游戏里打破防御，网络语境指心理防线被击穿，震惊、崩溃、感动都算；"
                   "B 站 2021 年度弹幕。",
        "usage": "被戳到痛处或感动到不行时说「破防了」。",
        "query": "破防 网络用语 意思 出处",
        "moe": "破防",
        "keys": ["破防"],
        "preferred": "https://zh.moegirl.org.cn/破防",
    },
    {
        "name": "下头",
        "triggers": ["下头", "下头男"],
        "meaning": "「上头」的反义，指某人或某事让人扫兴、反感、瞬间失去兴趣。",
        "usage": "形容油腻、扫兴的行为，比如「这话有点下头」。",
        "query": "下头 网络用语 意思 出处",
        "moe": "下头",
        "keys": ["下头"],
        "preferred": "https://baike.baidu.com/item/下头男/59870148",
    },
    {
        "name": "泰裤辣",
        "triggers": ["泰裤辣", "泰酷辣"],
        "meaning": "歌手小鬼王琳凯在演唱会喊话「太酷啦」的口音空耳，2023 年走红。",
        "usage": "夸张夸某事很酷，或反讽尬吹，模仿那个口音。",
        "query": "泰裤辣 梗 意思 出处",
        "moe": "泰裤辣",
        "keys": ["泰裤辣", "太酷啦"],
        "preferred": "https://zh.moegirl.org.cn/泰裤辣",
    },
    {
        "name": "依托答辩",
        "triggers": ["依托答辩", "一坨答辩"],
        "meaning": "「一坨大便」的谐音，用来骂作品、内容质量差、空洞。",
        "usage": "吐槽烂片烂活时用；攻击性强，别对着人用。",
        "query": "依托答辩 梗 意思 出处",
        "moe": "依托答辩",
        "keys": ["依托答辩", "一坨大便"],
        "preferred": "https://m.yyk.iask.sina.com.cn/q/q601Qoce4AP.html",
    },
    {
        "name": "遥遥领先",
        "triggers": ["遥遥领先"],
        "meaning": "华为发布会上余承东形容技术优势的口头禅，被剪成梗，常带调侃或反讽。",
        "usage": "夸某方面碾压时用，也常用来阴阳怪气地吹牛。",
        "query": "遥遥领先 梗 意思 出处",
        "moe": "遥遥领先",
        "keys": ["遥遥领先"],
        "preferred": "https://baike.baidu.com/item/遥遥领先/63435070",
    },
    {
        "name": "city不city",
        "triggers": ["city不city"],
        "meaning": "2024 年美国博主保保熊把英文 city 套进「A不A」句式，"
                   "用来表达「洋气不洋气、算不算城市」。",
        "usage": "问某地/某物够不够时髦，比如「这身穿搭 city 不 city」。",
        "query": "city不city 梗 意思 出处",
        "moe": "city不city",
        "keys": ["city", "保保熊"],
        "preferred": "http://news.cyol.com/gb/articles/2024-07/13/content_0zjNNWTvaj.html",
    },
    {
        "name": "哈基米",
        "triggers": ["哈基米", "哈基米南北绿豆"],
        "meaning": "出自《赛马娘》东海帝皇哼的「蜂蜜之歌」（はちみー），原指蜂蜜水；"
                   "现多用来亲昵称呼猫或软萌的东西。",
        "usage": "看到猫或可爱东西时喊「哈基米」，还能接「南北绿豆」。",
        "query": "哈基米 梗 意思 出处",
        "moe": "哈基米",
        "keys": ["哈基米"],
        "preferred": "https://zh.moegirl.org.cn/哈基米",
    },
    {
        "name": "电子榨菜",
        "triggers": ["电子榨菜"],
        "meaning": "吃饭时配着看的视频、短剧或有声书，像榨菜一样「下饭」，随独居青年流行。",
        "usage": "推荐下饭剧/视频时说「这是我的电子榨菜」。",
        "query": "电子榨菜 梗 意思 出处",
        "moe": "电子榨菜",
        "keys": ["电子榨菜"],
        "preferred": "https://baike.baidu.com/item/电子榨菜/62063038",
    },
    {
        "name": "显眼包",
        "triggers": ["显眼包", "现眼包"],
        "meaning": "2023 年流行词，指行为夸张、爱出风头、不怕出丑但能活跃气氛的人或物，"
                   "多带宠溺调侃。",
        "usage": "调侃群里爱整活的人「你个显眼包」。",
        "query": "显眼包 梗 意思 出处",
        "moe": "显眼包",
        "keys": ["显眼包", "现眼包"],
        "preferred": "https://baike.baidu.com/item/显眼包/63546651",
    },
    {
        "name": "我嘞个豆",
        "triggers": ["我嘞个豆", "我勒个豆"],
        "meaning": "河南方言感叹词的变体，相当于「我的天」，表惊讶、赞叹或无语；"
                   "2023 年因魔性视频翻红。",
        "usage": "看到离谱或震惊内容时来一句。",
        "query": "我嘞个豆 梗 意思 出处",
        "moe": "我嘞个豆",
        "keys": ["我嘞个豆", "我勒个豆"],
        "preferred": "https://finance.sina.cn/2023-08-30/detail-imzixwrf8227393.d.html",
    },
    {
        "name": "赢麻了",
        "triggers": ["赢麻了", "赢麻"],
        "meaning": "「赢到麻木」，谐音梗「秦始皇摸电线」（嬴政→赢，被电麻），"
                   "形容看似一直赢，多带反讽。",
        "usage": "反讽式庆祝，比如「这波我们赢麻了」。",
        "query": "赢麻了 梗 意思 出处",
        "moe": "赢麻了",
        "keys": ["赢麻了"],
        "preferred": "https://baike.baidu.com/item/赢麻了/59137509",
    },
    {
        "name": "典",
        "triggers": ["典中典", "太典了", "典中典中典"],
        "meaning": "「经典中的经典」的缩写；对线时用来嘲讽对方发言很典型、很套路，也可真心夸。",
        "usage": "看到教科书式发言时回「典中典」，一般带阴阳怪气。",
        "query": "典中典 梗 意思 出处",
        "moe": "典中典",
        "keys": ["典中典", "太典了"],
        "preferred": "https://baike.baidu.com/item/典中典/56954103",
    },
    {
        "name": "孝",
        "triggers": ["孝死", "孝子", "大孝子", "哄堂大孝", "带孝子"],
        "meaning": "网络上的「孝」是反讽，指对某人/某事物无底线吹捧的「孝子」，"
                   "如「米孝子」「农孝子」；「孝死我了」＝笑死＋讽刺。",
        "usage": "看到无脑护主、尬吹时用，攻击性较强，别对熟人认真用。",
        "query": "孝子 孝死 梗 意思 出处",
        "moe": "孝",
        "keys": ["孝子", "孝死", "孝"],
        "preferred": "https://tw.hinative.com/questions/23292710",
    },
    {
        "name": "芭比Q",
        "triggers": ["芭比q", "芭比Q了"],
        "meaning": "barbecue（BBQ）的谐音，原意烧烤，引申为「完蛋了 / 搞砸了」，带自嘲。",
        "usage": "事情翻车时说「芭比Q了」。",
        "query": "芭比Q 梗 意思 出处",
        "moe": "芭比Q",
        "keys": ["芭比q", "barbecue", "bbq"],
        "preferred": "https://baike.baidu.com/item/芭比Q了/59182231",
    },
    {
        "name": "绷不住了",
        "triggers": ["绷不住了", "蚌埠住了", "绷不住"],
        "meaning": "情绪受到冲击、快控制不住的样子，笑或哭都行；"
                   "谐音「蚌埠住了」在 2021 年爆火。",
        "usage": "看到太好笑或太离谱的东西时用。",
        "query": "绷不住了 梗 意思 出处",
        "moe": "绷不住了",
        "keys": ["绷不住了", "蚌埠住了"],
        "preferred": "https://baike.baidu.com/item/绷不住了/67834031",
    },
]


def _evidence(row: dict) -> str:
    parts = [str(row.get("title") or ""), str(row.get("snippet") or "")]
    for item in row.get("web") or []:
        parts.extend([str(item.get("title") or ""), str(item.get("snippet") or "")])
    moe = row.get("moe") or {}
    parts.extend([str(moe.get("title") or ""), str(moe.get("extract") or "")])
    return "\n".join(parts).casefold()


def _has_evidence(entry: dict, row: dict) -> bool:
    text = _evidence(row)
    return any(str(key).casefold() in text for key in entry["keys"])


def _pick_source(entry: dict, row: dict) -> str:
    preferred = str(entry.get("preferred") or "")
    urls = [str(item.get("url") or "") for item in row.get("web") or []]
    moe = row.get("moe") or {}
    if moe.get("url"):
        urls.append(str(moe["url"]))
    if preferred and any(
            preferred.rstrip("/") == url.rstrip("/").split("#")[0] or
            preferred.split("/")[2] in url for url in urls if url):
        return preferred
    # 退而求其次：本次结果里第一条沾边的，再不行就用萌娘百科 / 第一条
    for item in row.get("web") or []:
        blob = f"{item.get('title') or ''}{item.get('snippet') or ''}".casefold()
        if any(str(key).casefold() in blob for key in entry["keys"]):
            return str(item.get("url") or "")
    return str(moe.get("url") or (urls[0] if urls else ""))


def main() -> int:
    cfg = Config(os.path.join(BASE, "data", "config.json"))
    api_key = cfg.get("tools.search_api_key", "")
    book = MemeBook(os.path.join(BASE, "data", "memes.json"), cfg)
    added, skipped = [], []

    for i, entry in enumerate(ENTRIES, 1):
        row = {"web": [], "moe": {}}
        try:
            row["web"] = websearch.deepseek_search(
                entry["query"], api_key, count=4) if api_key else []
        except Exception as e:
            row["web_error"] = f"{type(e).__name__}: {e}"
        try:
            row["moe"] = moegirl.lookup(entry["moe"], chars=800)
        except Exception as e:
            row["moe"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        if not _has_evidence(entry, row):
            skipped.append(entry["name"])
            print(f"[skip] {entry['name']}：没查到能对上的来源"
                  f"（web {len(row['web'])} 条，萌百 {bool((row.get('moe') or {}).get('extract'))}）")
            continue

        source = _pick_source(entry, row)
        ok = book.learn(entry["name"], entry["triggers"], entry["meaning"],
                        entry["usage"], source=source,
                        learned_from="tools/seed_memes.py")
        if not ok:
            skipped.append(entry["name"])
            print(f"[skip] {entry['name']}：触发词不合法")
            continue
        added.append(entry["name"])
        top = ((row.get("web") or [{}])[0].get("title") or "").strip()
        print(f"[{i:02d}/{len(ENTRIES)}] {entry['name']} ← {source}"
              + (f"（搜到：{top[:40]}）" if top else ""))

    book.flush()
    print(f"\n写入 {len(added)} 条，跳过 {len(skipped)} 条"
          f"（库内共 {len(book.items)} 条）")
    if skipped:
        print("跳过：" + "、".join(skipped))
    return 0 if len(added) >= 25 else 1


if __name__ == "__main__":
    raise SystemExit(main())
