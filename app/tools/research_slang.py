"""
研究「现在中文网上大家怎么说话」，结果写进 data/memory/slang.md。

为什么单独一个脚本，而不是让机器人在对话时去查：
  · 查一次要几十秒，聊天里等不起
  · 更重要的是——**词表进了上下文，模型就会照着说**，变成公式化复读。
    所以这里只负责「收集 + 整理」，注入时按 v3 口径「网络语是群聊日常语言、跟着用，
    不刻意学也不刻意避开」来框（见 pawbot/persona.py 的 ## 网络用词）。

用法：
    python tools/research_slang.py            # 先试配置里的研究接口，失败退回主模型
    python tools/research_slang.py --llm      # 直接用主模型（deepseek）
    python tools/research_slang.py --dry      # 只打印，不写文件

配置（data/config.json）：
    "slang": {
        "base_url": "https://9527.codes",
        "api_key": "sk-...",
        "model": "gemini-4-argon",
        "timeout": 180
    }
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pawbot.config import Config            # noqa: E402
from pawbot.llm import LLMClient            # noqa: E402

OUT = os.path.join(ROOT, "data", "memory", "slang.md")

# 注意：prompt 不能写太长。deepseek-flash 是思考型模型，
# 长 prompt 会让它把 max_tokens 全花在 reasoning 上，finish=length 而 content 是空串。
PROMPT = """整理 18 条当下中文互联网上真实在用的说法，一行一条，格式：
「说法」—— 什么意思、什么场合用
覆盖这几类：赞同 / 无语 / 敷衍 / 夸人 / 损人吐槽 / 自嘲丧 / 群聊起哄催更。
别写前言和总结；已经过气的在后面标「老了」；容易用错的说明一下。"""


def _cfg():
    try:
        return Config(os.path.join(ROOT, "data", "config.json"))
    except Exception:
        return None


def _chat(base: str, key: str, model: str, prompt: str,
          max_tokens: int = 4000, timeout: float = 180) -> tuple[str, str]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode()
    # base 可能已经带 /v1（deepseek）也可能不带（中转站），别拼出 /v1/v1
    root = base.rstrip("/")
    url = root + ("/chat/completions" if root.endswith("/v1")
                  else "/v1/chat/completions")
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "Chrome/140.0.0.0 Safari/537.36")})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        return "", f"HTTP {e.code} {detail}"
    except Exception as e:
        return "", f"{type(e).__name__} {str(e)[:100]}"
    ch = (data.get("choices") or [{}])[0]
    text = ((ch.get("message") or {}).get("content") or "")
    fin = ch.get("finish_reason")
    used = (data.get("usage") or {}).get("completion_tokens")
    note = f"{model} {time.time() - t0:.0f}s"
    if not text.strip():
        note += f"（空回复，finish={fin} tokens={used}）"
    return text, note


def ask_relay(cfg) -> tuple[str, str]:
    """打配置里的研究接口。返回 (结果, 说明)。"""
    base = str(cfg.get("slang.base_url", "") or "").rstrip("/")
    key = str(cfg.get("slang.api_key", "") or "")
    model = str(cfg.get("slang.model", "") or "")
    timeout = float(cfg.get("slang.timeout", 180) or 180)
    if not (base and key and model):
        return "", "没配置 slang.base_url / api_key / model"

    mt = int(cfg.get("slang.max_tokens", 4000) or 4000)
    models = [model]
    backup = str(cfg.get("slang.fallback_model", "") or "")
    if backup and backup != model:
        models.append(backup)
    last = ""
    for m in models:
        text, note = _chat(base, key, m, PROMPT, max_tokens=mt, timeout=timeout)
        if text.strip():
            return text, note
        last = note
        print(f"  {m} 没出内容（{note}），换下一个")
    return "", last


async def ask_llm(cfg) -> tuple[str, str]:
    """退回主模型。

    注意：不走 LLMClient.chat —— 它的 max_tokens 预算是 900→1800→3600，
    这种「一口气列二十条」的请求会被截断成空串（实测）。这里直接给 4000。
    """
    base = str(cfg.get("llm.base_url", "") or "")
    key = str(cfg.get("llm.api_key", "") or "")
    model = str(cfg.get("llm.model", "") or "")
    if not (base and key and model):
        return "", "主模型没配 api_key"
    return _chat(base, key, model, PROMPT, max_tokens=8000, timeout=200)


def clean(text: str) -> list[str]:
    """把模型输出整成条目。

    模型不一定按 `- ` 开头——常见是「说法」—— 意思，或者 说法：意思。
    所以按「有没有分隔符」判断，而不是按行首符号。
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        ln = ln.lstrip("-*・• 　").strip()
        # 去掉序号：1. / 1、/ (1)
        ln = ln.lstrip("0123456789").lstrip(".)、 　").strip()
        if len(ln) < 6:
            continue
        if ("——" not in ln and "：" not in ln and ":" not in ln):
            continue                      # 不像条目（多半是前言/总结）
        out.append(ln)
    return out


def main():
    dry = "--dry" in sys.argv
    force_llm = "--llm" in sys.argv
    cfg = _cfg()

    got, note = "", ""
    if not force_llm and cfg is not None:
        got, note = ask_relay(cfg)
        print(f"[研究接口] {note}")
        if not got.strip():
            print("[研究接口] 没拿到内容，退回主模型")

    if not got.strip():
        if cfg is None:
            print("读不到 data/config.json")
            return 1
        # deepseek-flash 是思考型模型，这种「一口气列二十条」的请求
        # 经常把 token 全花在 reasoning 上、content 返回空（finish=length）。
        # 多试几次，总有一次思考得短。
        for attempt in range(3):
            got, note = asyncio.run(ask_llm(cfg))
            print(f"[主模型] 第 {attempt + 1} 次：{note}")
            if got.strip():
                break

    lines = clean(got)
    if not lines:
        print("整理不出条目，原文前 200 字：")
        print((got or "")[:200])
        return 2

    body = ("# 别人可能这么说话\n"
            "# 说明：这些是「别人怎么说」，只用来听懂。**不要当成自己的词库去套用**。\n"
            "# 由 tools/research_slang.py 生成；想手改直接改这个文件，按 mtime 热加载。\n\n"
            + "\n".join("- " + x for x in lines) + "\n")

    if dry:
        print(body[:1200])
        print(f"\n（--dry，共 {len(lines)} 条，没写文件）")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, OUT)
    print(f"已写入 {OUT}（{len(lines)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
