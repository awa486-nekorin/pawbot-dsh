#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包「正在运行的 PawBot 部署」为可迁移的 tar.gz，明确不含 Minecraft。

用法（在部署根目录或任意目录）：
    py -3.13 scripts/pack-no-mc.py [--out D:\\path\\dist]

安全边界：
- 不复制 app/data 的真实数据（人设/记忆/config/key/会话/日志）。
- 生成 app/data 的“空模板”，其中 config.json 来自运行配置但所有密钥/QQ/群号/私人路径已清空。
- 打包前做密钥扫描；发现疑似真实密钥则失败退出。
- 不含 .git/.venv/node_modules/dist/__pycache__/*.pyc/*.log。

产物：<out>/pawbot-server-no-mc-<版本>-<时间>.tar.gz + .sha256 + .manifest.txt
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT_FILES = [
    "index.js", "client.js", "package.json", "cordis.patch.yml", "icon.svg",
    "LICENSE", "README.md", "DEPLOY.md", ".gitignore", "AGENTS.md",
    "1-进入MC模式-拉取云端.bat", "2-结束MC模式-推回云端.bat", "3-查看同步状态.bat",
    "1-pull-cloud.bat", "2-push-cloud.bat", "3-sync-status.bat",
    "1-pull-cloud.sh", "2-push-cloud.sh", "3-sync-status.sh",
]
ROOT_DIRS = ["lib", "locale", "bili-watch", "scripts", "docs"]
APP_FILES = [
    "run.py", "requirements.txt", "requirements-bili.txt", "ARCHITECTURE.md",
    "README.upstream.md", "使用指南.md", "对话测试窗口.bat",
]
APP_DIRS = ["pawbot", "plugins", "web"]  # app/tools 是开发测试，不随运行包分发
APP_TOOL_FILES = ["cloud_sync.py"]       # 运维 CLI（云端同步），随包分发

SKIP_DIR_NAMES = {
    ".git", ".venv", "node_modules", "dist", "__pycache__", ".idea", ".vscode",
    ".data.pawbot-migration", ".pytest_cache", ".mypy_cache",
}
SKIP_FILE_SUFFIX = (".pyc", ".pyo", ".log")

SECRET_KEY_HINTS = (
    "api_key", "apikey", "access_token", "token", "secret", "password",
    "passphrase", "sessdata", "cookie", "credential", "cloud_key", "private_key",
    "decider_key", "judge_api_key",
)
SECRET_ALLOW = {
    "max_tokens", "tokens", "token_budget", "max_input_tokens",
    "max_output_tokens", "token_limit",
}
PRIVATE_LIST_PATHS = [
    ("account", "owner_ids"),
    ("qzone", "owner_ids"),
    ("proactive", "groups"),
    ("whitelist", "group_ids"),
    ("whitelist", "user_ids"),
    ("whitelist", "blacklist_group_ids"),
]

# 高频键的逐键说明（发言频率/行为）
KEY_DESC = {
    "chat.reply_mode": "回复策略：at_or_wake=被艾特/唤醒才回；all=全回",
    "chat.settle_sec": "一条消息后等多久再回（秒），越大越慢",
    "chat.settle_max": "等待上限（秒）",
    "chat.batch_enabled": "多条消息合并成一次回复",
    "chat.batch_msgs": "合并阈值区间 [最少, 最多] 条",
    "chat.batch_quiet": "安静多久后合并（秒区间）",
    "chat.batch_max_wait": "合并最长等待（秒区间）",
    "chat.batch_tick": "合并检查节拍（秒）",
    "reply.length_weights": "回复长短分布 [极短,短,中,长,超长] 的权重",
    "reply.quote": "是否引用原消息：auto/always/never",
    "reply.at_sender": "是否艾特发送者：auto/always/never",
    "reply.at_chance": "普通回复艾特概率",
    "reply.quote_chance": "普通回复引用概率",
    "reply.at_chance_addressed": "被点名时艾特概率",
    "reply.quote_chance_addressed": "被点名时引用概率",
    "safety.typing_cps": "打字速度（字/秒），影响“正在输入”时长",
    "safety.min_delay": "回复前最短延迟（秒）",
    "safety.max_delay": "回复前最长延迟（秒）",
    "safety.react_delay": "反应延迟区间（秒）",
    "safety.split_threshold": "超过多少字拆成多条发",
    "proactive.enabled": "主动发言总开关",
    "proactive.groups": "允许主动发言的群号列表（空=不主动说话）",
    "proactive.auto_groups": "自动纳入已知群",
    "proactive.idle_min": "群安静多少秒后考虑主动说话（下限）",
    "proactive.idle_max": "上限（实际按自适应 level 取区间）",
    "proactive.adaptive": "按群活跃度自适应频率",
    "proactive.adaptive_range": "自适应倍率区间",
    "proactive.idle_floor": "安静判定下限",
    "random_interject.enabled": "随机插话开关",
    "random_interject.probability": "每轮插话概率（0–1）",
    "random_interject.cooldown_sec": "插话冷却（秒）",
    "life.enabled": "生活作息/时段行为开关",
    "life.schedule": "一天内的作息表",
    "life.periods": "时段定义",
    "life.weekend_shift_hours": "周末作息平移小时",
    "events.recall_reply_chance": "看到撤回时的回应概率",
    "events.ban_reply_chance": "禁言事件的回应概率",
    "events.admin_reply_chance": "管理操作回应概率",
    "events.decrease_reply_chance": "降权/负面事件回应概率",
    "events.essence_reply_chance": "设精回应概率",
    "events.auto_accept_friend": "自动接受好友",
    "topic.enabled": "话题状态开关",
    "topic.min_idle": "话题冷启动最少空闲（秒）",
    "topic.busy_threshold": "繁忙阈值",
    "topic.chime_cooldown": "话题提醒冷却（秒）",
    "topic.history_size": "话题历史条数",
    "emotion.enabled": "情绪系统开关",
    "emotion.half_life_min": "情绪半衰期（分钟），越大残留越久",
    "emotion.turn_regress": "每轮向基线回落比例",
    "emotion.max_step": "单轮最大变化",
    "libido.half_life_min": "欲望半衰期（分钟）",
    "libido.max_step": "单轮最大变化",
    "reminders.enabled": "提醒开关",
    "reminders.poll_sec": "提醒轮询间隔（秒）",
    "reminders.quiet_hours": "免打扰时段",
    "longmem.enabled": "长期记忆开关",
    "longmem.half_life_days": "记忆衰减半衰期（天）",
    "longmem.top_k": "每次检索条数",
    "longmem.max_chars": "注入记忆最大字符",
    "longmem.keep_days": "保留天数",
    "longmem.max_episodes": "最大情景条数",
    "memory.engine": "记忆引擎",
    "memory.total_chars": "上下文记忆总预算（字符）",
    "memory.l0_limit": "L0 条数上限",
    "memory.l0_hours": "L0 时间窗（小时）",
    "memory.l0_chars": "L0 字符预算",
    "memory.l1_chars": "L1 字符预算",
    "memory.l2_chars": "L2 字符预算",
    "memory.l2_days": "L2 天级窗口",
    "tools.max_rounds": "工具调用最大轮数",
    "tools.time_budget": "工具总时间预算（秒）",
    "tools.search_count": "联网搜索条数",
    "tools.search_timeout": "搜索超时（秒）",
    "judge.timeout": "判官调用超时（秒）",
    "judge.reasoning_effort": "判官推理强度",
}



def is_secret_key(key: str) -> bool:
    low = key.lower()
    if low in SECRET_ALLOW:
        return False
    if low.endswith("_tokens") or low in {"tokens", "max_tokens"}:
        return False
    return any(h in low for h in SECRET_KEY_HINTS)


def sanitize_config(obj):
    """清空密钥/私密 ID/本机绝对路径，保留结构与行为参数。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_secret_key(k) and isinstance(v, (str, int, float)):
                out[k] = ""
            else:
                out[k] = sanitize_config(v)
        return out
    if isinstance(obj, list):
        return [sanitize_config(v) for v in obj]
    if isinstance(obj, str):
        s = obj
        if re.search(r"[A-Za-z]:[\\/]|^/home/|^/Users/|WorkBuddy", s):
            return ""
        return s
    return obj


def load_defaults():
    path = os.path.join(SRC, "app", "pawbot", "config.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "DEFAULT" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit("config.py 中找不到 DEFAULT")


def copytree(src, dst, skip_names, skip_suffix):
    count = 0
    for base, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_names]
        rel = os.path.relpath(base, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            if name.endswith(skip_suffix):
                continue
            s = os.path.join(base, name)
            if os.path.islink(s) or not os.path.isfile(s):
                continue
            t = os.path.join(target_dir, name)
            shutil.copy2(s, t)
            count += 1
    return count


def build_template_data(stage, defaults, running_config, include_persona=False):
    data = os.path.join(stage, "app", "data")
    state = os.path.join(data, "state")
    content = os.path.join(data, "content")
    for d in (state, content, os.path.join(content, "memory"),
              os.path.join(content, "people"), os.path.join(content, "diary"),
              os.path.join(content, "stickers"), os.path.join(content, "album")):
        os.makedirs(d, exist_ok=True)

    cfg = sanitize_config(running_config)
    for sec, key in PRIVATE_LIST_PATHS:
        if isinstance(cfg.get(sec), dict):
            cfg[sec][key] = []
    # 不含 Minecraft 功能
    cfg.pop("minecraft", None)
    with open(os.path.join(state, "config.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")

    if include_persona:
        src = os.path.join(SRC, "app", "data", "content", "persona.md")
        if not os.path.isfile(src):
            src = os.path.join(SRC, "app", "data", "persona.md")
        if not os.path.isfile(src):
            raise SystemExit("--include-persona：找不到真实 persona.md")
        shutil.copy2(src, os.path.join(content, "persona.md"))
    else:
        persona = (
            "# 大肥鱼人设（空模板）\n\n"
            "<!-- 这是空模板。迁移时把旧机器的 app/data/content/persona.md 复制过来；\n"
            "     不要把人设/密钥提交到公开仓库。 -->\n"
        )
        open(os.path.join(content, "persona.md"), "w", encoding="utf-8", newline="\n").write(persona)
    mem = {
        "facts.md": "<!-- PawBot 长期事实记忆。运行时由 PawBot 写入；这里留空。 -->\n",
        "impression.md": "<!-- PawBot 印象记忆。运行时由 PawBot 写入；这里留空。 -->\n",
        "slang.md": "<!-- PawBot 黑话/梗记忆。运行时由 PawBot 写入；这里留空。 -->\n",
    }
    for name, text in mem.items():
        open(os.path.join(content, "memory", name), "w", encoding="utf-8", newline="\n").write(text)
    json.dump({"items": []}, open(os.path.join(content, "hobbies.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    open(os.path.join(content, "hobbies.json"), "a", encoding="utf-8").write("\n")
    open(os.path.join(content, "people", ".gitkeep"), "w").close()
    open(os.path.join(content, "diary", ".gitkeep"), "w").close()
    open(os.path.join(state, "plugin_state.json"), "w", encoding="utf-8").write("{}\n")
    return cfg


def render_value(v):
    if isinstance(v, str):
        s = v.replace("|", "\\|")
        return f"`{s}`" if s else "`\"\"`（空）"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
        if len(s) > 60:
            s = s[:57] + "..."
        return f"`{s.replace('|', '\\|')}`"
    if isinstance(v, bool):
        return f"`{str(v).lower()}`"
    return f"`{v}`"


def build_checklist(stage, defaults, running_config, stats, include_persona=False):
    secret_rows = []
    for sec, opts in defaults.items():
        if not isinstance(opts, dict):
            continue
        for key, val in opts.items():
            if is_secret_key(key):
                secret_rows.append((f"{sec}.{key}", val))

    # 运行事实
    facts = [
        ("运行目录（部署根）", SRC),
        ("PawBot app 目录", os.path.join(SRC, "app")),
        ("数据根", stats["data_root"]),
        ("WebUI", f"http://127.0.0.1:{running_config.get('webui', {}).get('port', 6185)}"),
        ("OneBot 反向 WS", f"ws://0.0.0.0:{running_config.get('server', {}).get('port', 6199)}/ws"),
        ("模型提供方（当前）", str(running_config.get("llm", {}).get("provider") or "直连 API")),
        ("模型名（当前）", str(running_config.get("llm", {}).get("model") or "(见插件)")),
        ("讲解/判官模型", str(running_config.get("judge", {}).get("model") or "(未启用)")),
        ("主动发言是否启用", str(running_config.get("proactive", {}).get("enabled"))),
        ("插件数 / 处理器数（状态页）", "12 / 31"),
        ("Python 依赖", "pillow、websockets（requirements.txt）"),
        ("Node / DSH", "Node >= 22；dsh >= 0.1.7-rc.1"),
    ]

    lines = []
    A = lines.append
    A("# PawBot 服务端迁移：需要你填写的全部内容")
    A("")
    A(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　·　母本：`{SRC}`（当前正在运行的部署）")
    A("")
    A("> 本包**不含** `app/data` 的真实人设/记忆/密钥/会话/日志，也不含 Minecraft 功能。")
    A("> `app/data/state/config.json` 是消毒后的完整配置模板：结构齐全，密钥/QQ/群号/本机绝对路径已清空。")
    A("> 人设与记忆请从旧机器单独复制（见 §3），不要放进公开仓库。")
    if include_persona:
        A("")
        A("> ⚠️ **本包含真实人设 `app/data/content/persona.md`（记忆仍为空模板）。**")
        A("> 只走私有通道（scp/内网/私有网盘），**不要上传公开 GitHub、公共网盘或分享链接**；")
        A("> 服务器上 `chmod 600 app/data/content/persona.md`。")
    A("")
    A("## 0. 迁移前必须知道的三件事")
    A("")
    A("1. PawBot 以 **DSH 插件**形式运行：DSH 负责拉起 `app/run.py`，两端（插件与 PawBot）必须指向同一数据目录。")
    A("2. 服务器上 **6185（WebUI）与 6199（OneBot 反向 WS）只绑本地**；对外只暴露 DSH 的 Web 端口。")
    A("3. `app/data/config.json` 里的 key 可以留空，改用 DSH 凭证/环境变量提供；本清单会标出每个字段。")
    A("4. **数据/密钥必须外置**：插件 `dataDir` 指到 `/opt/pawbot-data`（程式目录外），首次启动自动迁移并建联接；")
    A("   升级只换程式，不会覆盖人设/记忆/key。随包脚本 `scripts/data_separation.py` 与 `scripts/update_code_safe.py`，")
    A("   完整说明见 `docs/DATA-SEPARATION.md`。")
    A("5. **两边实时同步人设/记忆**：见 `docs/SYNC-SETUP.md`；配置脚本 `scripts/sync_setup.py`，")
    A("   同步 CLI `app/tools/cloud_sync.py`。注意：只允许单一写入者；两边同时写会变成后写赢（不会合并）。")
    A("6. **本地 MC 模式**（云端常驻、玩 MC 时切本地）：见 `docs/LOCAL-MC-MODE.md`；")
    A("   本机双击 `1-进入MC模式-拉取云端.bat` / `2-结束MC模式-推回云端.bat`，或跑 `scripts/mc_sync.py`。")
    A("7. **最简单的一键同步（SCP，不需要 WebDAV）**：见 `docs/SCP-SYNC.md`；")
    A("   Windows 用三个 `.bat`，ECS 用 `1-pull-cloud.sh` / `2-push-cloud.sh`，共用 `scripts/scp_sync.py`。")
    A("")
    A("## 1. 当前运行事实（迁移对照用）")
    A("")
    A("| 项目 | 当前值 |")
    A("| --- | --- |")
    for k, v in facts:
        A(f"| {k} | {v} |")
    A("")
    A("## 2. 必须填写的字段（连不上/不能说话都卡在这里）")
    A("")
    A("### 2.1 DSH 插件 `@local/pawbot` 设置（装完插件后在设置页填）")
    A("")
    A("| 字段 | 迁移后填什么 | 说明 |")
    A("| --- | --- | --- |")
    plugin_rows = [
        ("projectDir", "`/opt/pawbot-dsh/app`", "run.py 所在目录；就是本包的 app/"),
        ("pythonExe", "`/opt/pawbot-dsh/app/.venv/bin/python`", "Linux 用 venv 内 python；先建 venv"),
        ("appDir", "`/opt/pawbot-dsh/app`", "与 projectDir 一致"),
        ("dataDir", "`/opt/pawbot-data`（建议独立盘）", "**必须放插件目录外**；首次启动自动迁移+联接，升级不覆盖数据"),
        ("autostart", "`true`", "DSH 启动时自动拉起 PawBot"),
        ("webuiHost / webuiPort", "`127.0.0.1` / `6185`", "必须与 config.json 的 webui 段一致"),
        ("webuiToken", "设置页生成/填写", "secret；留空则本地无需口令"),
        ("onebotToken", "与 NapCat 的 token 一致", "secret；NapCat 网络配置里填同一个"),
        ("restartOnCrash", "`true`（按需）", "崩溃自动重启"),
        ("gracefulStopMs", "`10000`", "优雅停止等待"),
        ("brainEnabled", "`true`", "使用 DSH 进程内大脑"),
        ("brainProvider / brainModel", "`opencode-go` / `deepseek-v4.1-flash`（按需）", "留空=跟随 DSH 默认"),
        ("brainSandbox", "`workspace-write`", "给全盘读写才改 danger-full-access"),
        ("brainCwd", "`/opt/pawbot-dsh/app/data/brain`（或空）", "运行用户必须可写"),
        ("brainCompletePersona / brainOwnerOnly", "`true` / `true`", "人设完整注入、仅主人可用"),
        ("dshBridgeEnabled", "`true`", "启用 DSH 工具桥"),
        ("dataGuardEnabled", "`true`", "数据守卫（升级前快照）"),
        ("dataBackupKeep", "`5`", "保留快照数"),
        ("napcatImage / napcatWebuiPort", "按你的 NapCat 部署填", "不用 Docker 可忽略"),
    ]
    for a, b, c in plugin_rows:
        A(f"| `{a}` | {b} | {c} |")
    A("")
    A("### 2.2 B 站看片插件 `@local/pawbot/bili-watch`（可选）")
    A("")
    A("| 字段 | 填什么 | 说明 |")
    A("| --- | --- | --- |")
    for a, b, c in [
        ("dataRoot", "`/opt/pawbot-bili-watch-data`", "共享目录；PawBot 的 tools.bili_watch_dir 要指向同一处"),
        ("model / allowedModel", "`deepseek-flash`", "v4-pro 会被白名单拦掉"),
        ("sessdata", "B 站 SESSDATA（secret）", "不填也能跑，只是字幕降级为 desc_comments"),
        ("autoStock / autoIntervalMin", "按需", "自动备货开关与间隔"),
    ]:
        A(f"| `{a}` | {b} | {c} |")
    A("")
    A("### 2.3 PawBot 配置里的密钥/账号（`app/data/state/config.json`）")
    A("")
    A("| 配置键 | 默认 | 说明 |")
    A("| --- | --- | --- |")
    curated = [
        ("llm.provider", "", "空=直连外部 API；`dsh`=用 DSH 大脑（当前运行是 dsh）"),
        ("llm.base_url", "", "OpenAI 兼容网关地址（provider 为空时必填）"),
        ("llm.api_key", "", "模型 key（可改用环境变量/DSH 凭证）"),
        ("llm.model", "", "模型名，例如 deepseek-chat / deepseek-flash"),
        ("llm.temperature / max_tokens / timeout", "", "采样与超时"),
        ("judge.enabled / base_url / api_key / model", "", "第二种判官模型（可复用主模型网关）"),
        ("emotion.judge_api_key / judge_base_url / judge_model", "", "情绪判官（独立 key 时填）"),
        ("speech.api_key / base_url / model", "", "语音识别（可选）"),
        ("tools.search_api_key", "", "联网搜索 key（可选）"),
        ("tools.image_api_key / pixiv_refresh_token", "", "Pixiv 抓图/识图 key（可选）"),
        ("tools.bili_watch_dir", "", "必须与 bili-watch 的 dataRoot 相同（绝对路径）"),
        ("tts.cloud_key / url", "", "云端 TTS key（可选）"),
        ("server.host / port / path / access_token", "", "OneBot 反向 WS 监听；access_token 与 NapCat 一致"),
        ("webui.host / port / token", "", "后台地址与口令；只绑 127.0.0.1"),
        ("account.owner_ids", "[]", "主人 QQ 号（完整数字），迁移后重填"),
        ("whitelist.group_ids / user_ids / blacklist_group_ids", "[]", "白名单/黑名单，按需重填"),
        ("proactive.groups", "[]", "允许主动发言的群号，按需重填"),
        ("qzone.owner_ids", "[]", "QQ 空间主人 QQ，按需"),
        ("sync.*（password/token/passphrase）", "", "云端同步凭证；不启用可留空"),
    ]
    for a, b, c in curated:
        A(f"| `{a}` | {render_value(b)} | {c} |")
    A("")
    A("### 2.4 环境变量（可选，优先级高于配置文件）")
    A("")
    A("| 变量 | 用途 |")
    A("| --- | --- |")
    for a, b in [
        ("PAWBOT_DATA_DIR", "数据根目录（建议指到插件目录外）"),
        ("PAWBOT_CONTROL_TOKEN", "宿主注入的优雅停止控制令牌（由 DSH 自动注入，通常不用手填）"),
        ("PAWBOT_DECIDER_KEY / PAWBOT_DECIDER_KEY_FILE", "决策模型（心情矩阵）key"),
        ("PAWBOT_JUDGE_API_KEY", "判官 key"),
        ("PAWBOT_BILI_CREDENTIAL_FILE", "B 站凭证文件路径"),
        ("BILI_SESSDATA", "B 站 SESSDATA（字幕）"),
        ("DSH_HOME / DSH_CREDENTIALS_FILE", "DSH 凭证位置"),
    ]:
        A(f"| `{a}` | {b} |")
    A("")
    A("### 2.5 最簡填寫順序（照這個順序做，最少必要項就能跑）")
    A("")
    A("1. 解壓 → `cd app && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`")
    A("2. DSH 安裝插件：`dsh plugin --profile <profile> add link:/opt/pawbot-dsh`，重啟 dsh")
    A("3. 插件设置：`projectDir=/opt/pawbot-dsh/app`、`pythonExe=/opt/pawbot-dsh/app/.venv/bin/python`、")
    A("   `dataDir=/opt/pawbot-data`、`autostart=true`、`webuiHost=127.0.0.1`、`webuiPort=6185`")
    A("4. 在 `app/data/state/config.json` 填：`account.owner_ids`（主人 QQ）、`whitelist`/`proactive.groups`（按需）")
    A("5. 模型二选一：`llm.provider=\"dsh\"`（用 DSH 大脑，推荐）或 `provider=\"\"`+`base_url`+`api_key`+`model`")
    A("6. NapCat：WebSocket 客户端连 `ws://127.0.0.1:6199/ws`；若填了 `server.access_token`/`onebotToken` 两边要一致")
    A("7. 从旧机器复制人设/记忆（§4），`chmod 600`")
    A("8. 重启 dsh → `pawbot_status` 应为 running，`pawbot_health` 为 200")
    A("9. 群里发一句话验证回复；再按 §3 微调发言频率")
    A("")
    A("## 3. 发言频率与行为参数（按你的口味改）")
    A("")
    A("> 这些都在 `app/data/state/config.json`，改完热生效或重启；默认值来自本部署的 `config.py`。")
    A("> 当前运行值可能已偏离默认（例如模型 provider=dsh）；下列默认仅作参考，迁移后以你填的为准。")
    A("")
    A("### 3.0 最常改的几个旋钮（先看这 8 个）")
    A("")
    A("| 想要的效果 | 改哪里 |")
    A("| --- | --- |")
    A("| 回得更快/更慢 | `chat.settle_sec`、`chat.settle_max`、`safety.min_delay`/`max_delay` |")
    A("| 多条消息合并成一次回 | `chat.batch_enabled`、`chat.batch_msgs`、`chat.batch_quiet` |")
    A("| 回复更长/更短 | `reply.length_weights` |")
    A("| 主动说话更勤/更少 | `proactive.idle_min`/`idle_max`、`proactive.adaptive`、`proactive.groups` |")
    A("| 偶尔随机插话 | `random_interject.enabled`/`probability`/`cooldown_sec` |")
    A("| 打字机节奏更自然 | `safety.typing_cps`、`safety.react_delay` |")
    A("| 情绪残留更久 | `emotion.half_life_min`、`emotion.turn_regress` |")
    A("| 记住更多/更久 | `memory.total_chars`、`longmem.top_k`、`longmem.half_life_days` |")
    A("")
    A("### 3.1 回复节奏 / 合并消息")
    A("")
    A("| 键 | 默认 | 作用 |")
    A("| --- | --- | --- |")
    for sec, keys, desc in [
        ("chat", ["reply_mode", "settle_sec", "settle_max", "batch_enabled",
                  "batch_msgs", "batch_quiet", "batch_max_wait", "batch_tick"],
         "reply_mode=回复策略；settle/batch 控制等多久再回、几条消息合并成一次"),
        ("reply", ["length_weights", "quote", "at_sender", "at_chance",
                   "quote_chance", "at_chance_addressed", "quote_chance_addressed"],
         "回复长短分布、引用/艾特概率"),
        ("safety", ["typing_cps", "min_delay", "max_delay", "react_delay", "split_threshold"],
         "打字速度、最短/最长延迟、分条发送阈值——直接影响“像不像真人”"),
    ]:
        for k in keys:
            val = defaults.get(sec, {}).get(k, "(无)")
            A(f"| `{sec}.{k}` | {render_value(val)} | {KEY_DESC.get(f'{sec}.{k}', desc)} |")
    A("")
    A("### 3.2 主动发言 / 随机插话")
    A("")
    A("| 键 | 默认 | 作用 |")
    A("| --- | --- | --- |")
    for sec, keys in [
        ("proactive", ["enabled", "groups", "auto_groups", "idle_min", "idle_max",
                       "adaptive", "adaptive_range", "idle_floor", "per_hour",
                       "quiet_hours", "span_min"]),
        ("random_interject", ["enabled", "probability", "cooldown_sec"]),
    ]:
        for k in keys:
            val = defaults.get(sec, {}).get(k, "(不在默认段，可手填)")
            A(f"| `{sec}.{k}` | {render_value(val)} | {KEY_DESC.get(f'{sec}.{k}', '主动发言/插话频率')} |")
    A("")
    A("> 注意：`proactive.groups` 为空＝不主动发言；每组实际频率还会被 `proactive_state.json` 的 level/idle 自适应。")
    A("")
    A("### 3.3 生活/事件/话题/情绪")
    A("")
    A("| 键 | 默认 | 作用 |")
    A("| --- | --- | --- |")
    for sec, keys in [
        ("life", ["enabled", "schedule", "periods", "weekend_shift_hours"]),
        ("events", ["recall_reply_chance", "ban_reply_chance", "admin_reply_chance",
                    "decrease_reply_chance", "essence_reply_chance", "auto_accept_friend"]),
        ("topic", ["enabled", "min_idle", "busy_threshold", "chime_cooldown", "history_size"]),
        ("emotion", ["enabled", "half_life_min", "turn_regress", "max_step"]),
        ("libido", ["half_life_min", "max_step"]),
        ("reminders", ["enabled", "poll_sec", "quiet_hours"]),
    ]:
        for k in keys:
            A(f"| `{sec}.{k}` | {render_value(defaults.get(sec, {}).get(k, '(无)'))} | {KEY_DESC.get(f'{sec}.{k}', '生活作息/事件反应/话题/情绪节奏')} |")
    A("")
    A("### 3.4 记忆/上下文/工具预算（影响“记不记得、聊多深”）")
    A("")
    A("| 键 | 默认 | 作用 |")
    A("| --- | --- | --- |")
    for sec, keys in [
        ("longmem", ["enabled", "half_life_days", "top_k", "max_chars", "keep_days", "max_episodes"]),
        ("memory", ["engine", "total_chars", "l0_limit", "l0_hours", "l1_chars", "l2_chars", "l2_days"]),
        ("tools", ["max_rounds", "time_budget", "search_count", "search_timeout"]),
        ("judge", ["enabled", "timeout", "reasoning_effort"]),
    ]:
        for k in keys:
            A(f"| `{sec}.{k}` | {render_value(defaults.get(sec, {}).get(k, '(无)'))} | {KEY_DESC.get(f'{sec}.{k}', '记忆衰减/上下文预算/工具轮数')} |")
    A("")
    A("## 4. 人设与记忆：需要从旧机器手工搬的文件")
    A("")
    A("| 旧机器路径 | 内容 | 是否必须 |")
    A("| --- | --- | --- |")
    for a, b, c in [
        ("app/data/content/persona.md", "人设正文（11 KB 左右）", "必须（否则是新人格）"),
        ("app/data/content/memory/*.md", "长期事实/印象/黑话", "必须"),
        ("app/data/content/people/", "对每个人的档案", "强烈建议"),
        ("app/data/content/diary/", "日记", "建议"),
        ("app/data/content/hobbies.json", "爱好", "建议"),
        ("app/data/content/relations.json", "关系网", "建议"),
        ("app/data/state/topic.json", "话题状态", "可选"),
        ("app/data/state/proactive_state.json", "主动发言自适应状态", "可选"),
        ("app/data/state/sessions.json", "最近会话", "可选"),
        ("app/data/state/brain/", "DSH 大脑会话记录", "可选"),
        ("app/data/content/stickers/、album/", "表情包/相册", "可选（较大）"),
    ]:
        A(f"| `{a}` | {b} | {c} |")
    A("")
    A("复制示例（在旧机器上打包，传到服务器再解压；不要放进公开仓库）：")
    A("")
    A("```bash")
    A("tar -czf pawbot-data-private.tgz -C app/data content state/config.json")
    A("scp pawbot-data-private.tgz root@<服务器>:/opt/")
    A("ssh root@<服务器> 'mkdir -p /opt/pawbot-data && tar -xzf /opt/pawbot-data-private.tgz -C /opt/pawbot-data'")
    A("chmod 600 /opt/pawbot-data/state/config.json /opt/pawbot-data/content/persona.md")
    A("```")
    A("")
    A("## 5. 依赖与安装")
    A("")
    A("```bash")
    A("# 1) 解压本包")
    A("tar -xzf pawbot-server-no-mc-*.tar.gz -C /opt && mv /opt/pawbot-dsh /opt/pawbot-dsh")
    A("# 2) Python 依赖（只有两个第三方包）")
    A("cd /opt/pawbot-dsh/app && python3 -m venv .venv && . .venv/bin/activate")
    A("pip install -r requirements.txt          # pillow、websockets")
    A("# 3) 安装 DSH 插件（本地目录）")
    A("dsh plugin --profile <profile> add link:/opt/pawbot-dsh")
    A("# 4) 重启 dsh 让 bundle 生效，然后在设置页按 §2.1/§2.2 填写")
    A("```")
    A("")
    A("服务器额外需要：Node.js >=22、DSH >=0.1.7-rc.1、NapCat（QQ）、可选 Chrome（Pixiv 抓图）/ffmpeg。")
    A("")
    A("## 6. 上传后的验收步骤")
    A("")
    A("1. `pawbot_status` → `running: true`、`ui_route: registered`")
    A("2. `pawbot_health` → `http_status: 200`、`plugins: 12`")
    A("3. DSH 设置 → PawBot → 状态页显示运行中；NapCat 连上后连接数 ≥ 1")
    A("4. 群里发一句话能回复；主动发言需要先填 `proactive.groups`")
    A("5. B 站插件：`bili_watch_status` 的 `model` 为 deepseek-flash、凭证状态符合预期")
    A("")
    A("## 7. 已知差异（Linux 云服务器）")
    A("")
    A("| 功能 | 状态 |")
    A("| --- | --- |")
    A("| Pixiv 本地浏览器抓图 | 不可用；建议 `tools.pixiv_use_browser=false` |")
    A("| 本地 TTS（CosyVoice） | 默认不可用；云端 TTS 不受影响 |")
    A("| 桌面通知/打开文件夹 | 不可用 |")
    A("| 优雅停止 | 反而更好（SIGTERM） |")
    A("| QQ 风控 | 服务器 IP 更容易触发验证，建议先小号试 |")
    A("")
    A("## 附录 A：完整配置键与默认值（`config.DEFAULT`）")
    A("")
    A("> 密钥类默认已标注；“需填”= 迁移后通常要填写/确认。")
    A("")
    for sec in sorted(defaults):
        opts = defaults[sec]
        if not isinstance(opts, dict):
            continue
        A(f"### {sec}")
        A("")
        A("| 键 | 默认 | 需填 |")
        A("| --- | --- | --- |")
        for k in sorted(opts):
            need = "是（密钥/账号）" if is_secret_key(k) else ""
            if sec in {"account", "proactive", "whitelist", "qzone"} and k in {
                    "owner_ids", "groups", "group_ids", "user_ids", "blacklist_group_ids"}:
                need = "是（QQ/群号）"
            A(f"| `{k}` | {render_value(opts[k])} | {need} |")
        A("")
    A("## 附录 B：本包内容与排除项")
    A("")
    A("包含：DSH 插件宿主文件、`app/`（run.py + pawbot + plugins + web + tools）、文档、部署脚本、")
    A("消毒后的 `app/data` 模板、本清单。")
    A("")
    A("不包含：`.git`、`.venv`、`node_modules`、`dist`、`app/data` 真实数据、`__pycache__`、`*.pyc`、`*.log`、")
    A("Minecraft 相关文件（本部署本身也没有）。")
    A("")
    with open(os.path.join(stage, "MIGRATION-CHECKLIST.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def scan_secrets(stage):
    patterns = [re.compile(p) for p in [
        r"sk-[A-Za-z0-9_\-]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"xox[baprs]-[A-Za-z0-9\-]{10,}",
        r"(?i)bearer\s+[A-Za-z0-9_.\-]{20,}",
    ]]
    hits = []
    benign = re.compile(r"placeholder|synthetic|example|dummy|fake|xxxx|unit-test|test-", re.I)
    for base, _dirs, files in os.walk(stage):
        for name in files:
            p = os.path.join(base, name)
            try:
                text = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for rx in patterns:
                for m in rx.finditer(text):
                    if benign.search(m.group(0)):
                        continue
                    hits.append(p)
                    break
                else:
                    continue
                break
    return hits


def verify(stage, defaults, include_persona=False):
    problems = []
    data = os.path.join(stage, "app", "data")
    cfg_path = os.path.join(data, "state", "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    if "minecraft" in cfg:
        problems.append("config 仍含 minecraft 段")
    for sec, opts in defaults.items():
        if not isinstance(opts, dict):
            continue
        for key in opts:
            if is_secret_key(key):
                node = cfg.get(sec, {})
                if isinstance(node, dict) and node.get(key):
                    problems.append(f"secret 未清空: {sec}.{key}")
    for sec, key in PRIVATE_LIST_PATHS:
        node = cfg.get(sec, {})
        if isinstance(node, dict) and node.get(key):
            problems.append(f"私密列表未清空: {sec}.{key}")
    persona_path = os.path.join(data, "content", "persona.md")
    persona = open(persona_path, encoding="utf-8").read()
    if not include_persona and len(persona) > 300:
        problems.append("persona 模板过长，疑似真实人设（未加 --include-persona）")
    if include_persona and len(persona) < 200:
        problems.append("--include-persona 但 persona 内容过短，疑似不是真实人设")
    # 记忆必须始终是空模板：不允许真实记忆/人物/日记/会话/关系
    memory_dir = os.path.join(data, "content", "memory")
    for base, _dirs, files in os.walk(memory_dir):
        for name in files:
            p = os.path.join(base, name)
            try:
                text = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                problems.append(f"无法读取记忆文件: {p}")
                continue
            if len(text) > 300:
                problems.append(f"记忆文件过长（疑似真实记忆）: {p}")
    for name in ("people", "diary"):
        d = os.path.join(data, "content", name)
        for base, _dirs, files in os.walk(d):
            for f in files:
                p = os.path.join(base, f)
                if f != ".gitkeep" or os.path.getsize(p) > 0:
                    problems.append(f"包含非空 {name} 数据: {p}")
    for forbidden in ("sessions.json", "relations.json", "longmem", "proactive_state.json",
                      "emotion.json", "mood.json", "libido.json", "topic.json"):
        p = os.path.join(data, "state", forbidden)
        if os.path.exists(p):
            problems.append(f"包含运行态数据: {p}")
        p2 = os.path.join(data, "content", forbidden)
        if os.path.exists(p2):
            problems.append(f"包含运行态数据: {p2}")
    for base, _dirs, files in os.walk(stage):
        for name in files:
            if "minecraft" in name.lower():
                problems.append(f"含 MC 文件名: {os.path.join(base, name)}")
    hits = scan_secrets(stage)
    if hits:
        problems.append(f"疑似密钥: {hits[:5]}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(SRC, "dist"))
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--include-persona", action="store_true",
                    help="把当前真实人设 persona.md 一并打包（记忆仍为空模板；包只能私有传输）")
    args = ap.parse_args()

    defaults = load_defaults()
    running = json.load(open(os.path.join(SRC, "app", "data", "state", "config.json"), encoding="utf-8"))
    try:
        version = json.load(open(os.path.join(SRC, "package.json"), encoding="utf-8")).get("version", "1.0.0")
    except Exception:
        version = "1.0.0"

    stage_root = tempfile.mkdtemp(prefix="pawbot-nomc-")
    stage = os.path.join(stage_root, "pawbot-dsh")
    os.makedirs(stage)

    copied = 0
    for name in ROOT_FILES:
        src = os.path.join(SRC, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(stage, name))
            copied += 1
    for name in ROOT_DIRS:
        src = os.path.join(SRC, name)
        if os.path.isdir(src):
            copied += copytree(src, os.path.join(stage, name), SKIP_DIR_NAMES, SKIP_FILE_SUFFIX)
    app_stage = os.path.join(stage, "app")
    os.makedirs(app_stage, exist_ok=True)
    for name in APP_FILES:
        src = os.path.join(SRC, "app", name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(app_stage, name))
            copied += 1
    for name in APP_DIRS:
        src = os.path.join(SRC, "app", name)
        if os.path.isdir(src):
            copied += copytree(src, os.path.join(app_stage, name), SKIP_DIR_NAMES, SKIP_FILE_SUFFIX)
    tools_stage = os.path.join(app_stage, "tools")
    for name in APP_TOOL_FILES:
        src = os.path.join(SRC, "app", "tools", name)
        if os.path.isfile(src):
            os.makedirs(tools_stage, exist_ok=True)
            shutil.copy2(src, os.path.join(tools_stage, name))
            copied += 1

    stats = {
        "data_root": os.path.join(SRC, "app", "data"),
    }
    build_template_data(stage, defaults, running, include_persona=args.include_persona)
    build_checklist(stage, defaults, running, stats, include_persona=args.include_persona)

    problems = verify(stage, defaults, include_persona=args.include_persona)
    if problems:
        print("PACKAGE VERIFY FAILED:")
        for p in problems:
            print(" -", p)
        shutil.rmtree(stage_root, ignore_errors=True)
        return 2

    # manifest
    try:
        head = subprocess.run(["git", "-C", SRC, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", SRC, "status", "--porcelain"], capture_output=True, text=True).stdout
    except Exception:
        head, dirty = "", ""
    manifest = [
        "PawBot server package (no Minecraft)",
        f"source: {SRC}",
        f"created: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"source_git_head: {head}",
        f"source_git_dirty_entries: {len(dirty.splitlines())}",
        f"copied_source_files: {copied}",
        "excluded: .git .venv node_modules dist __pycache__ *.pyc *.log app/data(real) .data.pawbot-migration",
        "included: sanitized app/data template (state/config.json + content placeholders)",
        "persona: " + ("REAL persona.md INCLUDED (PRIVATE - do not publish; memory NOT included)"
                       if args.include_persona else "template only"),
        "memory: empty templates only (never real memory)",
        "minecraft: absent by design",
    ]
    open(os.path.join(stage, "PACKAGE-MANIFEST.txt"), "w", encoding="utf-8").write("\n".join(manifest) + "\n")

    os.makedirs(args.out, exist_ok=True)
    base = f"pawbot-server-no-mc-{version}-{args.stamp}" + ("-persona" if args.include_persona else "")
    tar_path = os.path.join(args.out, base + ".tar.gz")
    def _tar_mode(ti):
        # Windows 上 .sh 没有可执行位，tar 里补成 755，解压后可直接 ./1-pull-cloud.sh
        if ti.name.endswith(".sh"):
            ti.mode = 0o755
        return ti
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(stage, arcname="pawbot-dsh", filter=_tar_mode)
    sha = hashlib.sha256(open(tar_path, "rb").read()).hexdigest()
    open(tar_path + ".sha256", "w", encoding="ascii").write(f"{sha}  {os.path.basename(tar_path)}\n")

    files = sum(len(f) for _b, _d, f in os.walk(stage))
    size_mb = os.path.getsize(tar_path) / 1024 / 1024
    print(f"OK -> {tar_path}")
    print(f"files={files} size={size_mb:.2f} MB sha256={sha}")
    if args.include_persona:
        print("!! 本包含真实人设（记忆仍为空模板）：只走私密通道，勿上传公开仓库/公共网盘。")
    print(f"checklist: {os.path.join(stage, 'MIGRATION-CHECKLIST.md')}")
    shutil.rmtree(stage_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
