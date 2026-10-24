"""原型 v2：按龄降分辨率。只验证「骨架长什么样」，不接进主流程。

v1 暴露的三个问题，这里逐个修：
  ① 背景句和事件句挤在同一个「。」里 → 删背景把事件一起删了（第 1 条骨架选错成「卡片打不开」）
  ② 「略糊」档只去时间/括注，保留 96%，等于没压
  ③ 200 天只比 60 天多 8 字，不叫「更模糊」
"""
import re

# ① 背景句只删到第一个逗号：`群里仍以图片闲聊为主，炸金美墨发糊图` → 只删前半
_背景头 = re.compile(r"^(?:群里|大家|这轮|本轮|话题|群内)[^，,。]{0,24}(?:仍|继续|还|在|围绕|以|主要)")
# 精确时间
_时间 = re.compile(
    r"\d{1,2}[:：]\d{2}|"
    r"(?:今天|昨天|前天|明天|后天|大后天|今早|昨晚|今儿|明儿|刚才|刚刚|上周|下周|这周|"
    r"上个月|下个月|去年|前年|\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{1,2}\s*[日号]|"
    r"周[一二三四五六日天]|星期[一二三四五六日天])"
)
_括注 = re.compile(r"[（(][^）)]{0,20}[）)]")
_程度 = re.compile(r"(非常|特别|有点|挺|很|太|超|极其|相当|稍微|略微|格外|十分)")
# 人名：中文 2~4 字 + 可选尾号括注（先抓再删括注，否则抓不到）
_人名 = re.compile(r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{1,11})\s*(?:[（(]\s*(?:尾号)?\d{3,12}\s*[）)])?")


def 去背景(t: str) -> str:
    """删掉每句开头的背景语，但只删到第一个逗号，别把后面的正事一起带走。"""
    出 = []
    for 句 in t.split("。"):
        s = 句.strip()
        if not s:
            continue
        m = _背景头.match(s)
        if m:
            # 从匹配结尾往后找第一个逗号，把逗号前的背景去掉
            头 = m.end()
            逗 = s.find("，", 头)
            if 逗 != -1 and 逗 - 头 < 12:
                s = s[逗 + 1:].strip()
            else:
                s = s[头:].lstrip("，, ").strip()
        if s:
            出.append(s)
    return "。".join(出)


def _清理(t: str, *, 抹时间: bool = True) -> str:
    t = _括注.sub("", t)
    if 抹时间:
        t = _时间.sub("那阵子", t)
        t = re.sub(r"(那阵子[，、]?)+", "那阵子", t)
    t = _程度.sub("", t)
    return re.sub(r"\s{2,}", " ", t).strip("，。、； ")


def _句子(t: str) -> list[str]:
    出 = []
    for 段 in re.split(r"[。；]", t):
        for x in re.split(r"[，,]", 段):
            x = x.strip("，、； ")
            if len(x) >= 5:
                出.append(x)
    return 出


def _有人名(s: str) -> int:
    """这句里有多少个像人名的词（用「大肥鱼」以外的连续中文块粗略估）。"""
    return len(re.findall(r"[\u4e00-\u9fa5]{2,4}(?=\s*(?:[（(]\s*(?:尾号)?\d{3,12}|[，,。；]|$))", s))


def 骨架(文本: str, 上限: int = 55) -> str:
    """一句话骨架：挑「最像事件」的那一句 —— 有人名、有动词、够长。规则版，兜底用。"""
    t = _清理(去背景(str(文本 or "").strip()))
    if not t:
        return ""
    句 = _句子(t)
    if not 句:
        return t[:上限]

    def 分(s: str) -> float:
        # 有人名 +2；长度 10~45 最合适；带「发/说/问/答/要/给/骂/夸/笑」这类动作 +1
        分 = 0.0
        if _有人名(s):
            分 += 2.0
        L = len(s)
        if 10 <= L <= 45:
            分 += 1.0
        elif L > 55:
            分 -= 1.0
        if re.search(r"发|说|问|答|要|给|骂|夸|笑|喊|叫|问|吐槽|接|认|道歉|催", s):
            分 += 1.0
        return 分

    最好 = max(句, key=分)
    if len(最好) > 上限:
        块 = re.split(r"[，,]", 最好)
        累 = ""
        for x in 块:
            if len(累) + len(x) + 1 > 上限:
                break
            累 = (累 + "，" + x) if 累 else x
        最好 = 累 or 最好[:上限]
    return 最好


def 略糊(文本: str, 上限: int = 110) -> str:
    """略糊：抹掉精确时间，保留前两个要点（不是全文）。"""
    t = _清理(去背景(str(文本 or "").strip()))
    if not t:
        return ""
    句 = _句子(t)
    if not 句:
        return t[:上限]
    # 取最像事件的当主句，再补一个不同的点
    主 = 骨架(t)
    次 = next((x for x in 句 if x != 主 and not x.startswith(主[:6]) and len(x) > 6), "")
    出 = 主 + ("。" + 次 if 次 else "")
    return 出[:上限]


档位 = [
    ("清晰", 7, 21),
    ("略糊", 30, 90),
    ("骨架", 180, 365),
    ("影影绰绰", 10**9, 10**9),
]


def 取档(age_days: float, importance: int = 3) -> str:
    重要 = int(importance or 3) >= 4
    for 名, 普, 重 in 档位:
        if age_days <= (重 if 重要 else 普):
            return 名
    return "影影绰绰"


def 渲染(文本: str, age_days: float, importance: int = 3) -> str:
    档 = 取档(age_days, importance)
    if 档 == "清晰":
        return str(文本 or "")
    if 档 == "略糊":
        return 略糊(文本)
    s = 骨架(文本)
    if 档 == "骨架":
        return s
    return f"（过去挺久了，细节记不清）{s}" if s else ""
