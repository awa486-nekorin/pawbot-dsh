"""讀外部 DSH 插件寫好的 B 站影片報告。

PawBot 端只管三件事：讀報告、發「去看」請求、記已分享。插件那頭負責
真的去看、寫報告、維護 index.json。兩邊靠共享目錄對接：

    <root>/requests/<id>.json   PawBot 寫，插件消費（看完移到 requests/done/）
    <root>/reports/<bvid>.json  插件寫，PawBot 讀（原子：先 .tmp 再 rename）
    <root>/reports/index.json   插件維護
    <root>/shared.json          PawBot 寫，插件只讀（避免重複推薦）

原則：只說真的看到的。watched_level 低於 subtitle 時，不假裝看過畫面/聽過內容。
本模組純標準庫，不發任何 QQ / B 站消息、不發評論。憑證只在
save_sessdata / has_credential 兩個小入口寫入 / 查詢，絕不回傳值。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from .log import log

# 相对路径：相对 PawBot 项目根（app/）。DSH 插件端会用绝对路径覆盖它。
DEFAULT_DIR = "pawbot-bili-watch-data"

# shared.json 的 read-modify-write 不是原子操作：兩個執行緒同時「讀→改→寫」
# 會互相覆蓋（甚至寫出半截 JSON）。整個模組共用一把鎖，成本可忽略。
_WRITE_LOCK = threading.RLock()

# 壞掉的 shared.json 只囉嗦一次，別每輪推薦都刷一屏。
_warned_bad_shared = False

# watched_level 優先序：subtitle（真的看完字幕）> desc_comments（只看簡介+評論）
# > title_only（只看到標題）。數字越小越優先。
_LEVEL_RANK = {"subtitle": 0, "desc_comments": 1, "title_only": 2}

_LEVEL_HINT = {
    "subtitle": "你读完整字幕，真的看过这个视频了，可以聊里面的具体内容。",
    "desc_comments": "你只看了简介和评论，没看到影片画面内容，别假装看过画面或听过内容。",
    "title_only": "你只看到标题，没看过任何内容，别假装看过，只能说标题给的感觉。",
}


def _resolve(dir=None) -> str:
    return str(dir) if dir else DEFAULT_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value):
    """解析 ISO8601 時間。不含時區當 UTC；解析失敗返回 None。"""
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _write_atomic(path: str, data) -> None:
    """先寫同目錄的臨時檔再 os.replace。

    tmp 名必須唯一：固定的 path + ".tmp" 在多執行緒下會互踩
    （Windows 上第二個 open 直接 [WinError 32]）。臨時檔無論成敗都清掉，
    免得 reports/ 目錄裡留一堆 .tmp。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _read_report(path: str):
    """讀單個報告檔。壞 JSON / 不是 v1 / 沒 bvid 都回 None，不拋。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            rep = json.load(f)
    except Exception:
        return None
    if not isinstance(rep, dict):
        return None
    if rep.get("version") != 1:
        return None
    if not rep.get("bvid"):
        return None
    return rep


def load_reports(dir=None) -> list[dict]:
    """讀 reports/*.json。略過 .tmp、index.json、壞 JSON、version!=1，容錯不拋。"""
    d = os.path.join(_resolve(dir), "reports")
    out: list[dict] = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if name == "index.json":
            continue
        if name.endswith(".tmp") or not name.endswith(".json"):
            continue
        rep = _read_report(os.path.join(d, name))
        if rep is not None:
            out.append(rep)
    return out


def _load_shared(dir=None) -> dict:
    """讀 shared.json；缺檔回 {}，壞檔回 {} 但會警告一次。"""
    global _warned_bad_shared
    path = os.path.join(_resolve(dir), "shared.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        if not _warned_bad_shared:
            _warned_bad_shared = True
            log.warn(f"shared.json 读坏了，当作空（可能导致重复推荐）："
                     f"{type(e).__name__} {e} {path}")
        return {}
    if not isinstance(data, dict):
        if not _warned_bad_shared:
            _warned_bad_shared = True
            log.warn(f"shared.json 内容不是对象，当作空（可能导致重复推荐）：{path}")
        return {}
    return data


def _sort_key(rep: dict):
    rank = _LEVEL_RANK.get(rep.get("watched_level"), 99)
    created = _parse_iso(rep.get("created_at"))
    ts = created.timestamp() if created is not None else 0.0
    return (rank, -ts)


def pick_report(session_id, keyword=None, max_age_hours=72, dir=None, author=None):
    """挑一條可分享的報告：share_worthy、未在 shared.json、未過期。

    author 給定時只留作者匹配（UP 主定向）；keyword 給定時只留
    標題/summary/tname 匹配；否則按 watched_level 優先序
    subtitle > desc_comments > title_only，同級取最新（created_at）在前。

    **給了 author / keyword 但 0 命中時回 None**。以前是「沒命中就退化成
    那一輪隨便一條」，於是 `up="目標UP"` 會回別人家的片、keyword 也常常
    文不對題；而且因為「有報告」就不再投遞「去看」請求，定向需求永遠
    送不出去。回 None 讓呼叫端照常 request_watch。
    """
    reports = load_reports(dir)
    shared = _load_shared(dir)
    kw = (keyword or "").strip().lower()
    au = (author or "").strip().lower()
    now = time.time()
    max_sec = None
    if max_age_hours and max_age_hours > 0:
        max_sec = float(max_age_hours) * 3600.0

    candidates: list[dict] = []
    for rep in reports:
        bvid = str(rep.get("bvid") or "")
        if not bvid or bvid in shared:
            continue
        if not rep.get("share_worthy"):
            continue
        if max_sec is not None:
            created = _parse_iso(rep.get("created_at"))
            if created is not None and now - created.timestamp() > max_sec:
                continue
        candidates.append(rep)

    if not candidates:
        return None

    if au:
        hits = [r for r in candidates
                if au in str(r.get("author") or "").lower()]
        if not hits:
            return None                      # 指定的 UP 一條都沒有，別拿別人充數
        candidates = hits

    if kw:
        hits = [r for r in candidates
                if kw in str(r.get("title") or "").lower()
                or kw in str(r.get("summary") or "").lower()
                or kw in str(r.get("tname") or "").lower()]
        if not hits:
            return None                      # 關鍵字 0 命中就是不命中
        candidates = hits

    candidates.sort(key=_sort_key)
    return candidates[0]


def mark_shared(bvid, session_id, dir=None) -> dict:
    """把 bvid 記進 shared.json（原子：tmp + os.replace），避免重複推薦。

    讀-改-寫整段在 _WRITE_LOCK 裡：多個執行緒同時記不同的片，否則後寫的
    會用讀到的舊資料覆蓋前面的人，shared.json 也會寫壞。
    """
    path = os.path.join(_resolve(dir), "shared.json")
    with _WRITE_LOCK:
        shared = _load_shared(dir)
        shared[str(bvid)] = {
            "session": str(session_id or ""),
            "shared_at": _now_iso(),
        }
        _write_atomic(path, shared)
    return shared


# request 檔 extra 允許的鍵（對應插件側的排序/日期/條數）。其它鍵一律丟棄。
_ALLOWED_EXTRA = ("order", "days", "num")


def _clean_extra(extra) -> dict | None:
    """只保留 _ALLOWED_EXTRA 裡的鍵，丟掉空字串/None；結果為空回 None。

    days/num 的「正整數」語義由插件側把關，這裡只做鍵的過濾與去空，
    不擅自轉型，避免意外改寫舊契約。
    """
    if not isinstance(extra, dict):
        return None
    out: dict = {}
    for k in _ALLOWED_EXTRA:
        v = extra.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[k] = v
    return out or None


def request_watch(kind, value, session_id, dir=None, extra=None) -> dict:
    """寫一條「去看」請求到 requests/<uuid>.json（原子寫），插件消費。

    kind ∈ bvid / keyword / auto / up。extra 是可選 dict，只有非空才會寫進
    檔案（鍵限 order / days / num）；舊呼叫 request_watch(kind, value, sid,
    dir=...) 行為完全不變。
    """
    d = os.path.join(_resolve(dir), "requests")
    with _WRITE_LOCK:
        os.makedirs(d, exist_ok=True)
        req = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "value": str(value or ""),
            "requested_at": _now_iso(),
            "session": str(session_id or ""),
        }
        clean = _clean_extra(extra)
        if clean:
            req["extra"] = clean
        _write_atomic(os.path.join(d, req["id"] + ".json"), req)
    return req


def _credential_path(dir=None, config=None) -> str:
    """算出 credential.json 的路徑。

    優先序：
      1. 明確給 dir → <dir>/credential.json（舊行為，測試也靠這個）
      2. 給 config → 讀 tools.bili_watch_credential_file。這個鍵的語意是
         **檔案**（不是目錄），所以直接拿來當路徑；留空才退回
         <tools.bili_watch_dir>/credential.json
      3. 都沒有 → <DEFAULT_DIR>/credential.json
    """
    if dir is not None:
        return os.path.join(_resolve(dir), "credential.json")
    if config is not None:
        raw = ""
        try:
            raw = str(config.get("tools.bili_watch_credential_file", "") or "").strip()
        except Exception as e:
            log.warn(f"读 B 站凭证路径配置失败（用默认）：{type(e).__name__} {e}")
        if raw:
            return raw
        try:
            d = str(config.get("tools.bili_watch_dir", "") or "").strip()
        except Exception:
            d = ""
        if d:
            return os.path.join(d, "credential.json")
    return os.path.join(_resolve(None), "credential.json")


def save_sessdata(value, dir=None, config=None) -> dict:
    """主人貼 B 站 SESSDATA 的小入口（不是模型工具，webui 也不碰它）。

    驗值非空且像 SESSDATA（長度 ≥ 20、不含空白與分號），通過才原子寫
    credential.json = {"sessdata": ..., "updated_at": ISO}。

    落點見 _credential_path：給 dir 就用該目錄；只給 config 時讀
    tools.bili_watch_credential_file（**檔案**語意）→ tools.bili_watch_dir。
    用法（主人自己在 Python console / 一次性腳本裡跑，不經模型）：

        from pawbot import bili_watch, config
        bili_watch.save_sessdata("貼上來的 SESSDATA",
                                 config=config.Config("data/config.json"))

    回傳只有 {ok, path, value_len}，絕不回傳值本身。
    """
    path = _credential_path(dir=dir, config=config)
    raw = value if isinstance(value, str) else ""
    s = raw.strip()
    ok = len(s) >= 20 and not any(ch.isspace() for ch in s) and ";" not in s
    if ok:
        with _WRITE_LOCK:
            _write_atomic(path, {"sessdata": s, "updated_at": _now_iso()})
    return {"ok": ok, "path": path, "value_len": len(s) if ok else 0}


def has_credential(dir=None, config=None) -> bool:
    """只看 env BILI_SESSDATA 或 credential.json 是否存在且非空。

    落點解析和 save_sessdata 共用 _credential_path，兩邊才不會各寫各的。
    不讀出值、不回傳值，只回 bool。
    """
    if (os.environ.get("BILI_SESSDATA") or "").strip():
        return True
    path = _credential_path(dir=dir, config=config)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    v = data.get("sessdata")
    return isinstance(v, str) and bool(v.strip())


def _text(value) -> str:
    """任何型別 → 去掉首尾空白的字串；None 回空字串，其它都 str()，絕不拋。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:
        return ""


def _items(value) -> list:
    """報告裡的列表欄位：不是 list/tuple 就當空（highlights 曾經是 dict/5）。"""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _format_body(rep: dict) -> str:
    """format_for_model 的本體：報告欄位一律先過型別防禦。"""
    level = _text(rep.get("watched_level")) or "title_only"
    hint = _LEVEL_HINT.get(level, _LEVEL_HINT["title_only"])

    lines: list[str] = []
    title = _text(rep.get("title"))
    lines.append(f"B站视频《{title or '（无标题）'}》")
    author = _text(rep.get("author"))
    if author:
        lines.append(f"UP主：{author}")
    url = _text(rep.get("url"))
    if url:
        lines.append(f"链接：{url}")
    lines.append(f"你的真实观看程度：{hint}")

    summary = _text(rep.get("summary"))
    if summary:
        lines.append(f"讲的是什么：{summary}")

    highlights = []
    for h in _items(rep.get("highlights"))[:5]:
        t = _text(h)
        if t:
            highlights.append(t)
    if highlights:
        lines.append("具体细节：")
        for h in highlights:
            lines.append(f"- {h}")

    vibe = _text(rep.get("comment_vibe"))
    if vibe:
        lines.append(f"评论区反应：{vibe}")

    take = _text(rep.get("take"))
    if take:
        lines.append(f"你自己的看法：{take}")

    lines.append("摘要和细节只写报告里真有的；报告里没讲清楚的地方就说「这个没细说」，"
                 "不要拿你已有的背景知识去补。")
    lines.append("用自己的话说，别照抄上面原文，别用「唔…」开头，像真人随口聊一句。")
    return "\n".join(lines)


def format_for_model(report) -> str:
    """把報告轉成給模型看的上下文。明確寫出 watched_level 的含義，附上
    summary / highlights / comment_vibe / take，並提醒模型用自己的話說、
    別念報告、別用「唔…」開頭。

    報告是外部插件寫的，欄位型別不可信（highlights={} / 5、summary=123）。
    這裡整段包起來：出任何意外都退回「標題 + 連結」的最小可用文字，不要讓
    工具層拿到「工具执行失败」——那時報告其實已經 mark_shared 了。
    """
    rep = report if isinstance(report, dict) else {}
    try:
        return _format_body(rep)
    except Exception as e:
        log.warn(f"格式化 B 站观看报告失败（退回最小文字）：{type(e).__name__} {e}")
        lines = [f"B站视频《{_text(rep.get('title')) or '（无标题）'}》"]
        url = _text(rep.get("url"))
        if url:
            lines.append(f"链接：{url}")
        return "\n".join(lines)
