"""
表情包库 / 图片。

    data/stickers/
        index.json     {"文件名.png": {"tags": ["开心","猫"], "desc": "猫娘叉腰得意，配字『就这』",
                                       "mood": "得意", "sha1": "...", "uses": 3,
                                       "desc_source": "vision"}}
        *.png *.jpg *.gif *.webp

index 里最关键的是 desc：一句**真实画面描述**（人物/动作/表情/配字），挑图时按它匹配，
而不是靠文件名瞎猜。mood 是固定词表里的一个情绪词，视觉通道拿不到就按标签猜。

desc 有两种来源（desc_source）：
    vision  真的让看图模型看过一眼（运行时由 media.sticker_vision 打开，或 tools/describe_stickers.py 补）
    tags    退回标签拼出来的人话，比如 困/躺平/摆烂 → 「困得躺平摆烂的样子」

没有 index.json 也能用——文件名本身就是标签。
机器人挑图时打分权重：标签 > 描述/mood > 文件名，不会每次都是同一张。

save() 是「收图」的入口：群里看到好梗图就存进来，
文件名消毒、按 sha1 去重、index.json 原子写入，库满了直接拒绝。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import random
import re
import urllib.error
import urllib.request

EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_SAVE_BYTES = 5 * 1024 * 1024       # 表情包只收 5MB 以内的
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg",
            "image/gif": ".gif", "image/webp": ".webp"}

# 表情包的固定情绪词表（和 tools/classify_stickers.py 的 MOODS 对齐：
# 那边是批处理脚本、这里是运行时，两边改动要一起改）。
MOODS = ("开心", "无语", "嫌弃", "震惊", "疑惑", "委屈", "哭哭", "得意",
         "贴贴", "晚安", "摸鱼", "害羞", "生气", "可爱")

# 标签里看不出情绪时按这些词猜 mood：先长词后短词（「翻白眼」别被「眼」抢了）
_MOOD_WORDS = (
    ("翻白眼", "嫌弃"), ("无语", "无语"), ("服了", "无语"), ("嫌弃", "嫌弃"),
    ("震惊", "震惊"), ("离谱", "震惊"), ("疑惑", "疑惑"), ("问号", "疑惑"),
    ("委屈", "委屈"), ("哭", "哭哭"), ("得意", "得意"), ("臭美", "得意"),
    ("贴贴", "贴贴"), ("抱抱", "贴贴"), ("撒娇", "贴贴"), ("亲亲", "贴贴"),
    ("晚安", "晚安"), ("睡", "晚安"), ("躺平", "摸鱼"), ("摆烂", "摸鱼"),
    ("摸鱼", "摸鱼"), ("困", "摸鱼"), ("累", "摸鱼"), ("害羞", "害羞"),
    ("脸红", "害羞"), ("生气", "生气"), ("气", "生气"), ("怒", "生气"),
    ("可爱", "可爱"), ("萌", "可爱"), ("开心", "开心"), ("笑", "开心"),
)


# 情绪矩阵的维度 → 挑表情包时用的标签（emotion.DIMS 同名）
EMOTION_TAGS = {
    "开心": ["开心", "笑", "快乐"], "兴奋": ["激动", "兴奋", "冲"],
    "惊讶": ["疑惑", "问号", "惊讶"], "烦躁": ["无语", "生气", "烦"],
    "低落": ["委屈", "难过", "哭"], "感动": ["哭哭", "贴贴", "抱抱"],
    "得意": ["得意", "骄傲", "臭美"], "害羞": ["害羞", "脸红", "捂脸"],
}


# ---------------------------------------------------------------- 描述元数据
# 视觉通道：OpenAI 兼容的 /chat/completions，图片用 base64 data URL。
# 运行时「收图」时会尽可能看一眼生成真实描述；没 key / 开关关着 / 调用失败就退回标签。
DESC_MODEL = "gemini-4-argon-medium"
DESC_BASE = "https://totokens.cc/v1"
DESC_TIMEOUT = 12.0
DESC_MAX_TOKENS = 1000
DESC_MAX_CHARS = 40                    # desc 上限（提示词和写回都按这个卡）
CRED_FILE = os.environ.get(
    "DSH_CREDENTIALS_FILE", r"C:\Users\Administrator\.dsh\.credentials.yaml")
CRED_KEY = "TOGEMINI_API_KEY"

# 测试 / 调用方可以注入一个假的看图函数：fn(raw: bytes) -> {"desc","mood","tags"} | None。
# 非 None 时优先于配置，测试里就靠它做到「真实写入 desc」而不联网。
VISION_DESCRIBER = None

DESC_PROMPT = (
    "你在给表情包库做标注。看这张表情包，只输出一个 JSON 对象，"
    "不要 markdown 代码块、不要任何解释：\n"
    '{"desc":"不超过40字的中文画面描述（人物/动作/表情/配字，不要泛泛说情绪）",'
    '"mood":"从固定词表里选一个","tags":["2-4个词"]}\n'
    "mood 只能从这些词里选一个：" + "、".join(MOODS)
)


def read_credential(name: str = CRED_KEY, path: str | None = None) -> str:
    """从环境变量或 DSH 凭证文件读一个 key。只读不打印，读不到返回空串。"""
    value = str(os.environ.get(name) or "").strip()
    if value:
        return value
    path = path or CRED_FILE
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = re.match(r"^\s*" + re.escape(name) + r"\s*:\s*(.+?)\s*$", line)
                if m:
                    value = m.group(1).strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        return ""
    return ""


def image_data_url(raw: bytes, max_side: int = 640, quality: int = 82) -> str | None:
    """把图片字节缩到最长边 max_side、转 JPEG，包成 data URL。动图只取第一帧。"""
    if not raw:
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            try:
                im.seek(0)
            except (EOFError, ValueError):
                pass
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[-1])
                im = canvas
            else:
                im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality)
    except Exception:
        return None
    return ("data:image/jpeg;base64,"
            + base64.b64encode(buf.getvalue()).decode("ascii"))


def _reply_text(obj) -> str:
    try:
        content = obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _first_json(text: str) -> dict | None:
    """从模型回复里挖出 JSON；容忍 ``` 围栏和前后废话。"""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    candidates = []
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    if fence:
        candidates.append(fence.group(1))
    left, right = raw.find("{"), raw.rfind("}")
    if left >= 0 and right > left:
        candidates.append(raw[left:right + 1])
    candidates.append(raw)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_desc_reply(text: str) -> dict | None:
    """解析看图模型的严格 JSON：{"desc","mood","tags"}。desc 为空就算失败。"""
    obj = _first_json(text)
    if obj is None:
        return None
    desc = " ".join(str(obj.get("desc") or "").split())[:DESC_MAX_CHARS]
    if not desc:
        return None
    mood = str(obj.get("mood") or "").strip()
    tags = obj.get("tags")
    if isinstance(tags, str):
        tags = re.split(r"[,，、\s]+", tags)
    out_tags = []
    if isinstance(tags, list):
        for tag in tags:
            tag = " ".join(str(tag or "").split())[:12]
            if tag and tag not in out_tags:
                out_tags.append(tag)
    return {"desc": desc, "mood": mood if mood in MOODS else "", "tags": out_tags[:4]}


def vision_describe(raw: bytes, *, base_url: str = "", api_key: str = "",
                    model: str = "", timeout: float = DESC_TIMEOUT,
                    prompt: str = DESC_PROMPT) -> dict | None:
    """看一眼这张图，返回 {"desc","mood","tags"}；任何失败都返回 None（不抛异常）。"""
    api_key = str(api_key or "").strip()
    if not api_key or not raw:
        return None
    data_url = image_data_url(raw)
    if not data_url:
        return None
    base = (str(base_url or "").strip().rstrip("/") or DESC_BASE)
    name = str(model or "").strip() or DESC_MODEL
    payload = {
        "model": name,
        "temperature": 0,
        "max_tokens": DESC_MAX_TOKENS,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(
            base + "/chat/completions", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
                "Accept": "application/json",
                "User-Agent": "PawBot/1.0",
            })
        try:
            with urllib.request.urlopen(req, timeout=max(1.0, float(timeout))) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt == 0:
                continue
            return None
        except Exception:
            if attempt == 0:
                continue
            return None
        parsed = parse_desc_reply(_reply_text(data))
        if parsed:
            return parsed
        return None
    return None


def make_describer(config):
    """按配置造一个看图函数；没开开关 / 没 key 就返回 None。

    配置项（默认值见 pawbot/config.py 的 media 段）：
        media.sticker_vision       开不开「收图时看一眼」
        media.sticker_vision_base  中转地址，默认 https://totokens.cc/v1
        media.sticker_vision_model 看图模型，默认 gemini-4-argon-medium
        media.sticker_vision_key   直接给 key；留空就读 TOGEMINI_API_KEY
    """
    def _get(key, default=None):
        try:
            return config.get(key, default)
        except Exception:
            return default

    if not bool(_get("media.sticker_vision", False)):
        return None
    base = str(_get("media.sticker_vision_base", "") or "").strip() or DESC_BASE
    model = str(_get("media.sticker_vision_model", "") or "").strip() or DESC_MODEL
    key = str(_get("media.sticker_vision_key", "") or "").strip() or read_credential()
    if not key:
        return None

    def _describe(raw: bytes):
        return vision_describe(raw, base_url=base, api_key=key, model=model)

    return _describe


def mood_from_tags(tags, fallback: str = "") -> str:
    """从标签猜一个固定词表里的 mood；猜不到返回 fallback（不在词表里就当空）。"""
    words = []
    for tag in tags or []:
        tag = " ".join(str(tag or "").split())
        if tag:
            words.append(tag)
    for word in words:
        if word in MOODS:
            return word
    joined = "".join(words)
    for word, mood in _MOOD_WORDS:
        if word in joined:
            return mood
    return fallback if fallback in MOODS else ""


def desc_from_tags(tags, mood: str = "") -> str:
    """标签 + 情绪拼一句人话描述，绝不写「斗图网：xxx」这种假描述。

    例：["困", "躺平", "摆烂"] → 「困得躺平摆烂的样子」；
        ["开心"] → 「开心的样子」；没标签 → 「<mood>的样子」/「一张表情包」。
    """
    words: list[str] = []
    for tag in tags or []:
        tag = " ".join(str(tag or "").split())[:12]
        if tag and tag not in words:
            words.append(tag)
    words = words[:4]
    if not words:
        base = str(mood or "").strip()
        return f"{base}的样子" if base else "一张表情包"
    if len(words) == 1:
        return f"{words[0]}的样子"
    # 单字状态词（困/累/气…）后面接动作，用「得」连起来才像人话
    if len(words[0]) == 1:
        return f"{words[0]}得{''.join(words[1:])}的样子"
    return "、".join(words) + "的样子"


class StickerError(ValueError):
    """存表情包被拒：不是图片 / 太大 / 库满。消息可以直接给模型看。"""


def _sniff_ext(raw: bytes) -> str | None:
    """只认真正的图片魔数；MIME 判断和 imagesearch 共用一套。"""
    if not (raw.startswith(b"\xff\xd8\xff")
            or raw.startswith(b"\x89PNG\r\n\x1a\n")
            or raw.startswith((b"GIF87a", b"GIF89a"))
            or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")):
        return None
    from .imagesearch import _sniff_mime
    return MIME_EXT.get(_sniff_mime(raw))


def _safe_name(name: str) -> str:
    """文件名消毒：只留文字/数字/CJK/连字符，挡掉路径分隔符和 emoji。"""
    base = re.split(r"[\\/]+", str(name or "").strip())[-1]
    base = os.path.splitext(base)[0]
    base = re.sub(r"[^\w\u4e00-\u9fff -]", "", base)
    base = re.sub(r"\s+", " ", base).strip(" ._-")
    return base[:40]


def _query_tokens(query: str) -> set[str]:
    """查询切词。中文再滑 2 字窗：单字标签（猫/困）和夹在句子里的情绪词才命得中。"""
    low = (query or "").lower()
    toks = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]+", low))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", low))
    toks.update(cjk[i:i + 2] for i in range(len(cjk) - 1))
    if len(cjk) == 1:
        toks.add(cjk)
    return toks


def _score_meta(name: str, meta: dict, tokens: set[str]) -> int:
    """给一个条目打分：标签 +2，描述 / mood / 文件名 +1。

    desc 与 mood 各算 1 分，所以它们能**单独**命中（不靠标签也能挑中），
    但标签仍然最高——标签决定这张图是干嘛用的，描述是补充。
    """
    if not tokens or not isinstance(meta, dict):
        return 0
    tags = [str(t).lower() for t in (meta.get("tags") or []) if str(t).strip()]
    desc = str(meta.get("desc") or "").lower()
    mood = str(meta.get("mood") or "").lower()
    name_low = str(name or "").lower()
    score = 0
    for tok in tokens:
        if any(tok in tag for tag in tags):
            score += 2
        elif tok in desc:
            score += 1
        elif tok in mood:
            score += 1
        elif tok in name_low:
            score += 1
    return score


class StickerBook:
    def __init__(self, base_dir: str, config):
        self.base_dir = base_dir
        self.config = config
        d = config.get("persona.stickers_dir", "data/stickers")
        self.dir = d if os.path.isabs(d) else os.path.join(base_dir, d)
        self.index_path = os.path.join(self.dir, "index.json")
        try:
            self.max_count = max(1, int(config.get("persona.stickers_max", 400) or 400))
        except (TypeError, ValueError):
            self.max_count = 400
        try:
            self.fetch_enabled = bool(config.get("persona.sticker_fetch", True))
        except Exception:
            self.fetch_enabled = True
        self._describer = None          # None=没算过；False=没有可用通道
        os.makedirs(self.dir, exist_ok=True)

    # ---------------------------------------------------------- 索引
    def index(self) -> dict:
        if not os.path.exists(self.index_path):
            return {}
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def files(self) -> list[str]:
        if not os.path.isdir(self.dir):
            return []
        return sorted(
            f for f in os.listdir(self.dir)
            if os.path.splitext(f)[1].lower() in EXTS
        )

    def save_index(self, data: dict):
        # 先写临时文件再替换，别让断电/并发截断成半个 JSON
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    def path_of(self, name: str) -> str | None:
        """index 里的文件名 → 磁盘上的绝对路径；找不到文件返回 None。"""
        path = os.path.join(self.dir, name)
        return path if os.path.isfile(path) else None

    # ---------------------------------------------------------- 看图（可选）
    def describer(self):
        """当前可用的看图函数；没有就是 None（调用方退回标签描述）。"""
        if VISION_DESCRIBER is not None:
            return VISION_DESCRIBER
        if self._describer is None:
            try:
                self._describer = make_describer(self.config) or False
            except Exception:
                self._describer = False
        return self._describer or None

    def describe(self, raw: bytes) -> dict | None:
        """看一眼图片字节，返回规整过的 {"desc","mood","tags"}；失败返回 None。"""
        fn = self.describer()
        if fn is None:
            return None
        try:
            info = fn(raw)
        except Exception as e:                  # 网络/解析/坏图，一律吞掉
            try:
                from .log import log
                log.debug(f"表情包看图失败：{type(e).__name__}")
            except Exception:
                pass
            return None
        if not isinstance(info, dict):
            return None
        desc = " ".join(str(info.get("desc") or "").split())[:DESC_MAX_CHARS]
        if not desc:
            return None
        mood = str(info.get("mood") or "").strip()
        tags: list[str] = []
        for tag in (info.get("tags") or []):
            tag = " ".join(str(tag or "").split())[:12]
            if tag and tag not in tags:
                tags.append(tag)
        return {"desc": desc, "mood": mood if mood in MOODS else "", "tags": tags[:4]}

    # ---------------------------------------------------------- 收图
    def save(self, raw: bytes, name: str, tags: list[str] | None = None,
             desc: str = "", mood: str = "", desc_source: str = "") -> str:
        """存一张表情包，返回文件名（不含目录）。失败抛 StickerError。

        desc / mood 没给齐时会「尽可能」看一眼生成（有看图通道的话），
        否则用标签 + 情绪拼一句人话，desc_source 标成 tags。
        """
        if not raw:
            raise StickerError("图片是空的")
        if len(raw) > MAX_SAVE_BYTES:
            raise StickerError(f"图太大了（{len(raw) // 1024}KB），5MB 以内的才收")
        ext = _sniff_ext(raw)
        if not ext:
            raise StickerError("这不是能识别的图片（只收 png/jpg/gif/webp）")

        labels: list[str] = []
        for t in (tags or []):
            t = str(t).strip()[:12]
            if t and t not in labels:
                labels.append(t)
        labels = labels[:8]
        desc = " ".join(str(desc or "").split())[:120]
        mood = str(mood or "").strip()[:12]
        source = str(desc_source or "").strip().lower()
        if source not in ("vision", "tags"):
            source = ""

        # 缺描述（或缺 mood）就去看一眼：视觉通道优先，拿不到再退回标签。
        info = None
        if not desc or not mood:
            info = self.describe(raw)
        if info:
            if not desc:
                desc = info["desc"]
                source = "vision"
            if not mood:
                mood = info["mood"]
            if not labels:
                labels = list(info.get("tags") or [])[:8]
        if not desc:
            desc = desc_from_tags(labels, mood)
            source = "tags"
        elif not source:
            source = "vision"          # 调用方给的描述，当成有人看过图
        if not mood:
            mood = mood_from_tags(labels)

        digest = hashlib.sha1(raw).hexdigest()
        idx = self.index()

        # 同一张图不重复存；顺手把新标签并进旧条目，pick_sticker 才找得到
        for fname in self.files():
            try:
                with open(os.path.join(self.dir, fname), "rb") as f:
                    same = hashlib.sha1(f.read()).hexdigest() == digest
            except OSError:
                continue
            if not same:
                continue
            meta = idx.get(fname) or {}
            old_tag_list = list(meta.get("tags") or [])
            merged = list(dict.fromkeys(old_tag_list + labels))
            changed = False
            if merged != old_tag_list:
                meta["tags"] = merged
                changed = True
            old_desc = str(meta.get("desc") or "")
            # 旧的假描述（「斗图网：xxx」）或空的，一律换成真描述
            if desc and (not old_desc or old_desc.startswith("斗图网：")
                         or (source == "vision" and old_desc != desc)):
                meta["desc"] = desc
                meta["desc_source"] = source
                changed = True
            if mood and not meta.get("mood"):
                meta["mood"] = mood
                changed = True
            if not meta.get("desc_source") and meta.get("desc"):
                meta["desc_source"] = source
                changed = True
            meta["sha1"] = digest
            if changed or fname not in idx:
                idx[fname] = meta
                self.save_index(idx)
            return fname

        if len(self.files()) >= self.max_count:
            raise StickerError(f"表情包库已到上限（{self.max_count} 张），先清理再存新图")

        base = _safe_name(name) or _safe_name(labels[0] if labels else "") or "sticker"
        fname = base + ext
        if os.path.exists(os.path.join(self.dir, fname)):
            fname = f"{base}-{digest[:8]}{ext}"      # 同名不同图：短 sha1 区分
        path = os.path.join(self.dir, fname)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)

        idx[fname] = {"tags": labels, "desc": desc, "mood": mood,
                      "sha1": digest, "uses": 0, "desc_source": source}
        self.save_index(idx)
        return fname

    # ---------------------------------------------------------- 挑图
    def pick_with_meta(self, query: str = "",
                       exclude: set[str] | None = None) -> tuple[str, dict] | None:
        """按描述挑一张，返回 (绝对路径, meta)。挑不到 / 空库返回 None。

        query 是「画面/情绪描述」，不是文件名：同时打标签、desc、mood、文件名。
        meta 里顺带塞了 name（文件名，不含目录），方便调用方回话。
        """
        files = self.files()
        if not files:
            return None

        exclude = exclude or set()
        pool = [f for f in files if f not in exclude] or files
        idx = self.index()
        q_tokens = _query_tokens(query)

        scored: list[tuple[int, str, dict]] = []
        for name in pool:
            meta = idx.get(name) or {}
            score = _score_meta(name, meta, q_tokens)
            if score:
                scored.append((score, name, meta))

        if scored:
            top = max(s for s, _, _ in scored)
            best = [(n, m) for s, n, m in scored if s == top]
            weights = [self._preference(m) for _, m in best]
            name, meta = random.choices(best, weights=weights)[0]
            return os.path.join(self.dir, name), self._with_name(name, meta)

        if query:
            # 排除之后没命中：说明这个描述的图已经发过了。
            # 与其硬发一张不相关的，不如再允许重复一次。
            if len(pool) != len(files):
                return self.pick_with_meta(query, exclude=None)
            return None
        name = random.choice(pool)
        return os.path.join(self.dir, name), self._with_name(name, idx.get(name) or {})

    def pick(self, query: str = "", exclude: set[str] | None = None) -> str | None:
        """按描述挑一张，返回绝对路径。挑不到返回 None。"""
        hit = self.pick_with_meta(query, exclude=exclude)
        return hit[0] if hit else None

    def top_meta(self, query: str = "", limit: int = 8) -> list[tuple[str, dict]]:
        """按 query 打分排好的前 N 条（给提示词注入清单用）。库小就全列。

        没有命中时按偏好铺满，清单不会因为心情不搭而空着。
        """
        files = self.files()
        if not files:
            return []
        try:
            count = max(1, int(limit))
        except (TypeError, ValueError):
            count = 8
        idx = self.index()
        q_tokens = _query_tokens(query)
        rows = []
        for name in files:
            meta = idx.get(name) or {}
            rows.append((_score_meta(name, meta, q_tokens), name, meta))
        rows.sort(key=lambda row: (-row[0], -self._preference(row[2]), row[1]))
        return [(name, self._with_name(name, meta)) for _, name, meta in rows[:count]]

    @staticmethod
    def _with_name(name: str, meta) -> dict:
        out = dict(meta) if isinstance(meta, dict) else {}
        out["name"] = name
        return out

    # ---------------------------------------------------------- 联网补充
    def fetch_from_doutu(self, tag: str, n: int = 1,
                         extra_tags: list[str] | None = None) -> list[str]:
        """去斗图网按 tag 抓 n 张存进库，返回存下的文件名（已按 sha1 去重）。

        同步函数，会阻塞网络；在事件循环里请用 pick_or_fetch 或 to_thread。
        """
        tag = str(tag or "").strip()
        if not tag:
            return []
        try:
            want = max(1, int(n))
        except (TypeError, ValueError):
            want = 1
        from . import doutu
        from . import animecheck
        from .log import log
        try:
            urls = doutu.search(tag)
        except Exception:
            return []
        if not urls:
            return []

        # 画风门槛：斗图网的搜索结果鱼龙混杂，真人照片 / 照片合成不能进库。
        # 判定 False 或 None 一律不入库；没有配置看图通道时没法判，按老路放过并记日志。
        anime_only = True
        try:
            anime_only = bool(self.config.get("persona.sticker_anime_only", True))
        except Exception:
            anime_only = True
        checked = anime_only and animecheck.configured(self.config)
        if anime_only and not checked:
            log.info("自动抓图：没配置看图模型，画风门槛本次跳过")

        random.shuffle(urls)
        saved: list[str] = []
        seen: set[str] = set()
        tags = [tag, *(extra_tags or [])]
        for url in urls:
            if len(saved) >= want:
                break
            raw = doutu.download(url)
            if not raw:
                continue
            digest = hashlib.sha1(raw).hexdigest()
            if digest in seen:          # 同一页里重复的图，只存一次
                continue
            seen.add(digest)
            if checked and animecheck.is_anime_cached(raw, self.config) is not True:
                log.info(f"自动抓图跳过（非二次元/无法判定）：{tag}")
                continue
            try:
                # desc/mood 交给 save：能看一眼就用视觉，看不到就标签兜底
                name = self.save(raw, f"{tag}_{digest[:6]}", tags=tags)
            except StickerError:
                continue
            if checked:
                idx = self.index()
                meta = idx.get(name)
                if isinstance(meta, dict) and meta.get("anime") is not True:
                    meta["anime"] = True
                    self.save_index(idx)
            saved.append(name)
        return saved

    async def pick_or_fetch(self, query: str = "",
                            exclude: set[str] | None = None) -> str | None:
        """先按描述挑库里的；挑不到且允许联网时，去斗图网抓一张再挑。

        网络下载放在 to_thread 里，不阻塞事件循环。返回绝对路径或 None。
        """
        path = self.pick(query, exclude=exclude)
        if path or not getattr(self, "fetch_enabled", True):
            return path
        tag = str(query or "").strip()
        if not tag:
            return None
        try:
            saved = await asyncio.to_thread(self.fetch_from_doutu, tag, 1)
        except Exception:
            return None
        path = self.pick(query, exclude=exclude)
        if path:
            return path
        if saved:
            return os.path.join(self.dir, saved[-1])
        return None

    def list_tags(self) -> list[str]:
        tags = set()
        for meta in self.index().values():
            tags.update(meta.get("tags") or [])
        return sorted(tags)

    def list_moods(self) -> list[str]:
        moods = set()
        for meta in self.index().values():
            mood = str(meta.get("mood") or "").strip()
            if mood:
                moods.add(mood)
        return sorted(moods)

    # ---------------------------------------------------------- 偏好池
    @staticmethod
    def _preference(meta: dict) -> float:
        """偏好权重：用得越多越喜欢，收藏时手动标的 like 加倍。封顶避免一张霸屏。"""
        if not isinstance(meta, dict):
            return 1.0
        uses = int(meta.get("uses") or 0)
        likes = int(meta.get("likes") or 0)
        return min(6.0, 1.0 + 0.4 * uses + 1.0 * likes)

    def mark_used(self, name: str):
        idx = self.index()
        meta = idx.get(name)
        if meta is None:
            return
        meta["uses"] = int(meta.get("uses") or 0) + 1
        idx[name] = meta
        self.save_index(idx)

    def fetch_remote(self, query: str, tags: list[str] | None = None) -> str | None:
        """库里没有合适的就去公开图片搜索找一张「<情绪> 表情包」，校验后存进库再返回路径。"""
        from . import imagesearch
        try:
            results = imagesearch.search_sogou(f"{query} 表情包", 8)
        except Exception:
            return None
        # 标题像表情包的排前面
        results.sort(key=lambda r: ("表情" not in str(r.get("title") or "")))
        for item in results[:5]:
            raw = imagesearch.download({"url": item.get("url")}, 12)
            # 太小多半是图标，太大多半是壁纸/照片
            if not raw or not (4 * 1024 <= len(raw) <= 2 * 1024 * 1024):
                continue
            try:
                name = self.save(raw, f"{query}-网", tags=list(tags or [query]))
            except StickerError:
                continue
            return os.path.join(self.dir, name)
        return None

    def to_segment(self, path_or_url: str) -> dict | None:
        """本地文件转 base64 段（NapCat 那边 enableLocalFile2Url 关着也能发）。"""
        if path_or_url.startswith(("http://", "https://")):
            return {"type": "image", "data": {"file": path_or_url}}
        if path_or_url.startswith("base64://"):
            return {"type": "image", "data": {"file": path_or_url}}

        path = path_or_url
        if not os.path.isabs(path):
            path = os.path.join(self.dir, path)
        if not os.path.exists(path):
            return None

        size = os.path.getsize(path)
        if size > 8 * 1024 * 1024:      # QQ 图片上限大概 10MB，留点余量
            return None
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        return {"type": "image", "data": {"file": "base64://" + data}}
