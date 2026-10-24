"""图库：带文字描述的图，专门给「不用再看一遍图」用。

跟表情包（`data/stickers/`）分开，因为两者的用途不一样：
    表情包  用来发，靠情绪标签挑，量大、不重要
    图库    用来「记得这张图是什么」，每条必须有描述，量小、要准

**核心规矩：入库必须带描述，取用时只读描述，不再调视觉模型。**
所以 `desc()` 是纯本地读，一个 token 都不花 —— 这是它跟 `search_image` 最大的区别。

存储：
    data/album/<id>.<ext>      图片文件
    data/album/index.json      [{id, desc, tags, path, bytes, added_at, source}]
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

from .log import log
from .memory import _tokens

索引名 = "index.json"
描述上限 = 200
最多条数 = 500


def _短id(种子: str) -> str:
    return hashlib.sha1(str(种子).encode("utf-8")).hexdigest()[:10]


class Album:
    def __init__(self, base_dir: str, config=None):
        self.base_dir = base_dir
        d = "data/album"
        if config is not None:
            d = config.get("album.dir", d) or d
        self.dir = d if os.path.isabs(d) else os.path.join(base_dir, d)
        self.index_path = os.path.join(self.dir, 索引名)
        self.items: list[dict] = []
        self._load()

    # ------------------------------------------------------------ 存储
    def _load(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.items = [x for x in data if isinstance(x, dict) and x.get("id")] if isinstance(data, list) else []
        except Exception:
            self.items = []

    def _save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items[-最多条数:], f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    # ------------------------------------------------------------ 写
    def add(self, path: str, desc: str, tags=None, *, source: str = "",
            copy: bool = True) -> str:
        """把一张图收进图库。**desc 必填** —— 没有描述就不该入库，否则以后还得再看一遍。

        返回图片 id；描述为空或文件不存在时抛 ValueError。
        """
        描述 = " ".join(str(desc or "").split()).strip()
        if not 描述:
            raise ValueError("图库必须带描述：以后要靠这句描述直接调用，不能留空")
        源 = str(path or "").strip()
        if not 源 or not os.path.isfile(源):
            raise ValueError(f"图片不存在：{源 or '（空路径）'}")

        标签 = [str(t).strip() for t in (tags or []) if str(t).strip()][:12]
        戳 = time.strftime("%Y%m%d-%H%M%S")
        图片id = _短id(f"{源}|{描述}|{戳}")
        扩展 = os.path.splitext(源)[1].lower() or ".jpg"
        目标 = os.path.join(self.dir, 图片id + 扩展)

        os.makedirs(self.dir, exist_ok=True)
        if copy:
            try:
                shutil.copyfile(源, 目标)
            except OSError as e:
                raise ValueError(f"复制图片失败：{e}") from e
        else:
            目标 = os.path.abspath(源)

        self.items.append({
            "id": 图片id,
            "desc": 描述[:描述上限],
            "tags": 标签,
            "path": 目标,
            "bytes": os.path.getsize(目标) if os.path.exists(目标) else 0,
            "added_at": int(time.time()),
            "source": str(source or ""),
        })
        self._save()
        log.info(f"图库收了一张：{图片id} {描述[:30]}")
        return 图片id

    def remove(self, 图片id: str) -> bool:
        留 = [x for x in self.items if x.get("id") != 图片id]
        if len(留) == len(self.items):
            return False
        目标 = self.get(图片id) or {}
        self.items = 留
        self._save()
        p = str(目标.get("path") or "")
        if p and os.path.isfile(p) and os.path.dirname(os.path.abspath(p)) == os.path.abspath(self.dir):
            try:
                os.remove(p)
            except OSError:
                pass
        return True

    # ------------------------------------------------------------ 读
    def get(self, 图片id: str) -> dict | None:
        for x in self.items:
            if x.get("id") == 图片id:
                return x
        return None

    def desc(self, 图片id: str) -> str:
        """取描述。**纯本地读，不调视觉模型** —— 这是图库存在的意义。"""
        item = self.get(图片id)
        return str(item.get("desc") or "") if item else ""

    def search(self, query: str, k: int = 3) -> list[dict]:
        """按描述/标签找图。返回带分数，分数为 0 的不返回。"""
        q = _tokens(str(query or ""))
        if not q:
            return []
        出 = []
        for x in self.items:
            hay = f"{x.get('desc') or ''} {' '.join(x.get('tags') or [])}"
            命中 = len(q & _tokens(hay))
            if not 命中:
                continue
            分 = 命中 + (1.0 if str(query).strip() and str(query).strip() in hay else 0)
            出.append({**x, "score": round(分, 2)})
        出.sort(key=lambda x: x["score"], reverse=True)
        return 出[:max(1, int(k or 3))]

    def all(self) -> list[dict]:
        return list(self.items)

    def stats(self) -> dict:
        return {"条数": len(self.items),
                "总字节": sum(int(x.get("bytes") or 0) for x in self.items),
                "目录": self.dir}
