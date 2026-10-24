"""
表情包库 / 图片。

    data/stickers/
        index.json     {"文件名.png": {"tags": ["开心","猫"], "desc": "笑得很欠"}}
        *.png *.jpg *.gif *.webp

没有 index.json 也能用——文件名本身就是标签。
机器人挑图时按「标签重叠 + 随机」选，不会每次都是同一张。

save() 是「收图」的入口：群里看到好梗图就存进来，
文件名消毒、按 sha1 去重、index.json 原子写入，库满了直接拒绝。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re

EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_SAVE_BYTES = 5 * 1024 * 1024       # 表情包只收 5MB 以内的
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg",
            "image/gif": ".gif", "image/webp": ".webp"}


# 情绪矩阵的维度 → 挑表情包时用的标签（emotion.DIMS 同名）
EMOTION_TAGS = {
    "开心": ["开心", "笑", "快乐"], "兴奋": ["激动", "兴奋", "冲"],
    "好奇": ["疑惑", "问号", "好奇"], "烦躁": ["无语", "生气", "烦"],
    "低落": ["委屈", "难过", "哭"], "亲昵": ["贴贴", "抱抱", "撒娇"],
    "得意": ["得意", "骄傲", "臭美"], "疲惫": ["躺平", "困", "累"],
}


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

    # ---------------------------------------------------------- 收图
    def save(self, raw: bytes, name: str, tags: list[str] | None = None,
             desc: str = "") -> str:
        """存一张表情包，返回文件名（不含目录）。失败抛 StickerError。"""
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
        desc = str(desc or "").strip()[:120]

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
            merged = list(dict.fromkeys(list(meta.get("tags") or []) + labels))
            if merged != (meta.get("tags") or []) or (desc and not meta.get("desc")):
                meta["tags"] = merged
                if desc and not meta.get("desc"):
                    meta["desc"] = desc
                meta["sha1"] = digest
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

        idx[fname] = {"tags": labels, "desc": desc, "sha1": digest}
        self.save_index(idx)
        return fname

    # ---------------------------------------------------------- 挑图
    def pick(self, query: str = "", exclude: set[str] | None = None) -> str | None:
        """按标签挑一张，返回绝对路径。挑不到返回 None。"""
        files = self.files()
        if not files:
            return None

        exclude = exclude or set()
        pool = [f for f in files if f not in exclude] or files
        idx = self.index()
        q_tokens = _query_tokens(query)

        scored: list[tuple[int, str]] = []
        for name in pool:
            meta = idx.get(name) or {}
            tags = [str(t).lower() for t in (meta.get("tags") or [])]
            hay = " ".join([
                name,
                " ".join(tags),
                str(meta.get("desc") or ""),
            ]).lower()
            score = 0
            for tok in q_tokens:
                if any(tok in tag for tag in tags):
                    score += 2          # 标签比描述更能说明这张图是干嘛用的
                elif tok in hay:
                    score += 1
            if score:
                scored.append((score, name))

        if scored:
            top = max(s for s, _ in scored)
            best = [n for s, n in scored if s == top]
            weights = [self._preference(idx.get(n) or {}) for n in best]
            return os.path.join(self.dir, random.choices(best, weights=weights)[0])

        if query:
            # 排除之后没命中：说明这个标签的图已经发过了。
            # 与其硬发一张不相关的，不如再允许重复一次。
            if len(pool) != len(files):
                return self.pick(query, exclude=None)
            return None
        return os.path.join(self.dir, random.choice(pool))

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
                name = self.save(raw, f"{tag}_{digest[:6]}", tags=tags,
                                 desc=f"斗图网：{tag}")
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
        """先按标签挑库里的；挑不到且允许联网时，去斗图网抓一张再挑。

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

    # ---------------------------------------------------------- 发送
    # ---------------------------------------------------------- 偏好池
    @staticmethod
    def _preference(meta: dict) -> float:
        """偏好权重：用得越多越喜欢，收藏时手动标的 like 加倍。封顶避免一张霸屏。"""
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
                name = self.save(raw, f"{query}-网", tags=list(tags or [query]),
                                 desc=str(item.get("title") or "")[:60])
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
