#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单独试一次决策模型（调试用，不影响运行中的 PawBot）。

    py tools/decider_once.py --text "群友：你今天怎么这么可爱" --context "群里在闲聊"

输出：answers（概率/置信度）、映射后的情绪增减、耗时、usage.cost。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import decider                                    # noqa: E402
from pawbot.config import Config                              # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="试一次心情矩阵的决策模型")
    parser.add_argument("--text", required=True, help="新消息")
    parser.add_argument("--context", default="", help="最近上下文（可空）")
    parser.add_argument("--addressed", action="store_true", help="这条消息是不是在叫她")
    parser.add_argument("--base", default=str(BASE_DIR), help="项目根目录（默认脚本上一级）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    config = Config(os.path.join(os.path.abspath(args.base), "data", "config.json"))
    values = {dim: 50 for dim in decider.EMOTIONS}

    async def run():
        t0 = time.perf_counter()
        state = decider.build_state(args.text, args.context, args.addressed, values)
        answers = await decider.decide(config, state, decider.mood_questions())
        deltas = decider.deltas_from_answers(answers)
        return answers, deltas, (time.perf_counter() - t0) * 1000

    try:
        answers, deltas, ms = asyncio.run(run())
    except decider.DeciderError as exc:
        if args.json:
            print(f'{{"ok":false,"code":"{exc.code}"}}')
        else:
            print(f"[FAIL] [{exc.code}] {exc.message}")
        return 1

    if args.json:
        print(json_dumps({"ok": True, "answers": answers, "deltas": deltas, "elapsed_ms": round(ms)}))
        return 0

    choice = (answers.get("emotion") or {}).get("choice") if isinstance(answers.get("emotion"), dict) else ""
    confidence = (answers.get("emotion") or {}).get("confidence") if isinstance(answers.get("emotion"), dict) else None
    shift = (answers.get("shift") or {}).get("noul") if isinstance(answers.get("shift"), dict) else None
    intensity = (answers.get("intensity") or {}).get("score") if isinstance(answers.get("intensity"), dict) else None
    print(f"模型：{decider.model(config)}")
    print(f"主导情绪：{choice}（置信度 {confidence}）")
    print(f"变化概率：{shift}   强度分：{intensity}")
    print(f"映射增减：{deltas or '{}（低于阈值/没变化，会走关键词兜底）'}")
    print(f"耗时：{ms:.0f}ms")
    return 0


def json_dumps(data) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
