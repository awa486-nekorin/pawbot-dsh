#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PawBot 数据分离：检查 / 设置外部数据根 / 验证更新安全。

用法：
    py -3.13 scripts/data_separation.py --mode plan
    py -3.13 scripts/data_separation.py --mode apply --data-dir D:/PawBotData
    py -3.13 scripts/data_separation.py --mode verify [--json]

设计目标：
- 程式目录可整包替换；数据（人设/记忆/设定）放在程式目录外；
- 密钥不写进人设/记忆档；config 内敏感字段应留空或用环境/DSH 凭证；
- DSH 插件在 dataDir 指向外部时，下次载入自动迁移 app/data → dataDir 并建立目录联接。

本脚本只改 DSH profile 的 dataDir 行（apply），不动数据；迁移由插件在重启时完成。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

SECRET_HINTS = (
    "api_key", "access_token", "secret", "password", "passphrase",
    "sessdata", "cloud_key", "decider_key", "cookie", "private_key",
)
SECRET_ALLOW = {"max_tokens", "tokens", "max_input_tokens", "max_output_tokens",
                "token_budget", "token_limit"}


def truth(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def find_profile_config(explicit: str = "") -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()
    roots = []
    if os.environ.get("DSH_HOME"):
        roots.append(Path(os.environ["DSH_HOME"]).expanduser())
    roots.append(Path.home() / ".dsh")
    for root in roots:
        profiles = root / "profiles"
        if not profiles.is_dir():
            continue
        for patch in sorted(profiles.glob("*/cordis.patch.yml")):
            try:
                text = patch.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if re.search(r"^\s*-\s*id:\s*pawbot\s*$", text, re.M):
                return patch.resolve()
    return None


def read_block(lines: list[str]) -> tuple[int, int]:
    """返回 @local/pawbot 配置块 [start, end) 的行号；找不到返回 (-1,-1)。"""
    start = -1
    indent = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)-\s*id:\s*pawbot\s*$", line)
        if m:
            start = i
            indent = len(m.group(1))
            break
    if start < 0:
        return -1, -1
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(rf"^\s{{0,{indent}}}-\s", lines[j]) and not lines[j].startswith(" " * (indent + 1)):
            end = j
            break
    return start, end


def get_data_dir_from_profile(text: str) -> str:
    lines = text.splitlines()
    start, end = read_block(lines)
    if start < 0:
        return ""
    for line in lines[start:end]:
        m = re.match(r"^\s*dataDir\s*:\s*(.*)$", line)
        if m:
            raw = m.group(1).strip()
            if raw.startswith(("'", '"')) and raw.endswith(("'", '"')) and len(raw) >= 2:
                raw = raw[1:-1]
            return raw
    return ""


def set_data_dir_in_text(text: str, data_dir: str) -> tuple[str, str]:
    """在 @local/pawbot 的 config 下写入 dataDir；返回 (新文本, 变更说明)。"""
    lines = text.splitlines()
    start, end = read_block(lines)
    if start < 0:
        raise SystemExit("profile 中找不到 '@local/pawbot' 插件条目，拒绝修改")
    value = f'"{data_dir.replace(chr(92), "/")}"'
    config_idx = -1
    config_indent = 0
    for i in range(start, end):
        m = re.match(r"^(\s*)config\s*:\s*$", lines[i])
        if m:
            config_idx = i
            config_indent = len(m.group(1))
            break
    if config_idx < 0:
        insertion = [f"{' ' * (config_indent)}config:", f"{' ' * (config_indent + 2)}dataDir: {value}"]
        lines[end:end] = insertion
        return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), "inserted config+dataDir"
    # config 块结束：下一个缩进 <= config_indent 的非空行（且不是注释）
    config_end = end
    for j in range(config_idx + 1, end):
        if not lines[j].strip() or lines[j].lstrip().startswith("#"):
            continue
        if len(lines[j]) - len(lines[j].lstrip()) <= config_indent:
            config_end = j
            break
    for j in range(config_idx + 1, config_end):
        m = re.match(r"^(\s*)dataDir\s*:\s*(.*)$", lines[j])
        if m:
            old = m.group(2).strip()
            if old.strip("'\"") == data_dir.replace("\\", "/"):
                return text, "unchanged"
            lines[j] = f"{m.group(1)}dataDir: {value}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), f"updated dataDir: {old} -> {value}"
    child_indent = " " * (config_indent + 2)
    lines.insert(config_end, f"{child_indent}dataDir: {value}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), "inserted dataDir"


def is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        # Windows junction: realpath 与路径本身不同
        return os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path))
    except OSError:
        return False


def secret_count(config_path: Path) -> tuple[int, list[str]]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return -1, []

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, f"{prefix}.{k}" if prefix else k)
        else:
            yield prefix, node

    found = []
    for key, value in walk(data):
        low = key.lower().split(".")[-1]
        if low in SECRET_ALLOW or low.endswith("_tokens"):
            continue
        if any(h in low for h in SECRET_HINTS) and value not in ("", None, 0, False, [], {}):
            found.append(key)
    return len(found), found


def sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def dir_stats(path: Path) -> tuple[int, int]:
    files = total = 0
    if not path.is_dir():
        return 0, 0
    for base, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", "backups", "logs"}]
        for name in names:
            try:
                total += (Path(base) / name).stat().st_size
                files += 1
            except OSError:
                pass
    return files, total


def describe(root: Path, app_dir: Path, profile_config: Path | None,
             env_data_dir: str = "") -> dict:
    app_data = app_dir / "data"
    profile_text = ""
    profile_data_dir = ""
    if profile_config and profile_config.is_file():
        profile_text = profile_config.read_text(encoding="utf-8", errors="ignore")
        profile_data_dir = get_data_dir_from_profile(profile_text)
    env_value = env_data_dir or os.environ.get("PAWBOT_DATA_DIR", "")
    requested = profile_data_dir or env_value
    effective = str(app_data)
    external = False
    if app_data.exists() and is_link(app_data):
        effective = os.path.realpath(app_data)
        external = True
    elif app_data.exists() and requested:
        effective = os.path.realpath(app_data)
    canonical = Path(effective)
    cfg = canonical / "state" / "config.json"
    persona = canonical / "content" / "persona.md"
    count, fields = secret_count(cfg) if cfg.is_file() else (-1, [])
    content_files, content_bytes = dir_stats(canonical / "content")
    state_files, state_bytes = dir_stats(canonical / "state")
    lock = canonical / ".runtime.lock"
    lock_age = None
    if lock.is_file():
        try:
            lock_age = round(time.time() - lock.stat().st_mtime, 1)
        except OSError:
            pass
    return {
        "app_dir": str(app_dir),
        "app_data": str(app_data),
        "app_data_is_link": bool(app_data.exists() and is_link(app_data)),
        "app_data_target": os.path.realpath(app_data) if app_data.exists() else "",
        "profile_config": str(profile_config) if profile_config else "",
        "profile_data_dir": profile_data_dir,
        "env_data_dir": env_value,
        "requested": requested,
        "effective_data_root": effective,
        "external": external,
        "update_safe": bool(external and requested and
                            os.path.normcase(os.path.realpath(app_data)) ==
                            os.path.normcase(os.path.abspath(requested))),
        "config_exists": cfg.is_file(),
        "config_sha256": sha256(cfg),
        "persona_exists": persona.is_file(),
        "persona_sha256": sha256(persona),
        "persona_bytes": persona.stat().st_size if persona.is_file() else 0,
        "secret_fields_nonempty": count,
        "secret_field_names": fields,
        "content_files": content_files,
        "content_bytes": content_bytes,
        "state_files": state_files,
        "state_bytes": state_bytes,
        "runtime_lock_present": lock.is_file(),
        "runtime_lock_age_sec": lock_age,
    }


def print_human(info: dict) -> None:
    print("PawBot 数据分离状态")
    print(f"  程式目录        : {info['app_dir']}")
    print(f"  app/data        : {info['app_data']}")
    print(f"  是否目录联接    : {info['app_data_is_link']}")
    print(f"  联接目标        : {info['app_data_target'] or '(无)'}")
    print(f"  插件 profile    : {info['profile_config'] or '(未找到)'}")
    print(f"  dataDir(插件)   : {info['profile_data_dir'] or '(未设置)'}")
    print(f"  PAWBOT_DATA_DIR : {info['env_data_dir'] or '(未设置)'}")
    print(f"  实际数据根      : {info['effective_data_root']}")
    print(f"  已外置          : {info['external']}")
    print(f"  更新安全        : {info['update_safe']}")
    print(f"  config.json     : {'存在' if info['config_exists'] else '缺失'}  "
          f"sha256={info['config_sha256'][:16] or '-'}")
    print(f"  persona.md      : {'存在' if info['persona_exists'] else '缺失'}  "
          f"{info['persona_bytes']} B  sha256={info['persona_sha256'][:16] or '-'}")
    print(f"  非空密钥字段    : {info['secret_fields_nonempty']} "
          f"{info['secret_field_names'] if info['secret_field_names'] else ''}")
    print(f"  content 文件    : {info['content_files']} 个 / {info['content_bytes']/1024:.1f} KiB")
    print(f"  state 文件      : {info['state_files']} 个 / {info['state_bytes']/1024:.1f} KiB")
    if info["runtime_lock_present"]:
        print(f"  运行锁          : 存在（{info['runtime_lock_age_sec']} 秒前更新）→ 服务可能在跑")
    else:
        print("  运行锁          : 不存在（服务可能已停止）")
    print()
    if info["update_safe"]:
        print("结论：数据已外置，升级只换程式，不会覆盖人设/记忆/设定。")
    else:
        print("结论：数据仍在程式目录内（或联接未就绪）——升级前请按 plan 的步骤外置。")


def mode_plan(info: dict, data_dir: str) -> int:
    print_human(info)
    print("建议方案")
    print(f"  外部数据目录    : {data_dir}")
    print("  一次性操作      : 写入插件 dataDir，然后重启 DSH；插件会自动：")
    print("                    复制 app/data → 外部目录 → 把 app/data 改名留档 → 建立目录联接")
    print("  本次计划        :")
    print(f"    py -3.13 scripts/data_separation.py --mode apply --data-dir {data_dir}")
    print("    （随后重启 DSH；再运行 --mode verify）")
    print()
    print("注意")
    print("  1. 不要手动搬 app/data；迁移由插件在重启时完成。")
    print("  2. 外部目录不要放在程式目录内，也不要让程式目录放在外部目录内。")
    print("  3. 当前密钥字段为 0；模型 key 在 DSH 凭证里，不会进 PawBot 数据。")
    return 0


def mode_apply(info: dict, data_dir: str, profile_config: Path | None) -> int:
    if not data_dir:
        print("ERROR: --mode apply 需要 --data-dir", file=sys.stderr)
        return 2
    target = Path(data_dir).expanduser().resolve()
    app_dir = Path(info["app_dir"]).resolve()
    try:
        common = Path(os.path.commonpath([str(target), str(app_dir)]))
    except ValueError:
        common = None
    if common in (target, app_dir):
        print(f"ERROR: 外部数据目录不能与程式目录重叠：{target} vs {app_dir}", file=sys.stderr)
        return 2
    if not profile_config or not profile_config.is_file():
        print("ERROR: 找不到 DSH profile 配置，请用 --profile-config 指定", file=sys.stderr)
        return 2
    text = profile_config.read_text(encoding="utf-8", errors="ignore")
    new_text, change = set_data_dir_in_text(text, str(target))
    if change == "unchanged":
        print(f"已是最新：dataDir={target}（{profile_config}）")
        return 0
    backup = profile_config.with_name(profile_config.name + ".bak-datasep-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(profile_config, backup)
    tmp = profile_config.with_name(profile_config.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8", newline="\n")
    os.replace(tmp, profile_config)
    target.mkdir(parents=True, exist_ok=True)
    print(f"已写入 {profile_config}：{change}")
    print(f"原配置备份：{backup}")
    print(f"外部目录已准备：{target}")
    print()
    print("下一步（必须）：停止并重新启动 DSH，让插件载入新配置；启动时自动迁移 app/data。")
    print("迁移完成后运行：py -3.13 scripts/data_separation.py --mode verify")
    return 0


def mode_verify(info: dict, json_out: bool) -> int:
    if json_out:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print_human(info)
        print("验收")
        checks = [
            ("dataDir 已设置", bool(info["requested"])),
            ("数据已外置", info["external"]),
            ("app/data 是联接", info["app_data_is_link"]),
            ("config.json 存在", info["config_exists"]),
            ("persona.md 存在", info["persona_exists"]),
            ("非空密钥字段=0", info["secret_fields_nonempty"] == 0),
        ]
        for name, ok in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print()
        print("结论：" + ("更新安全（PASS）" if all(ok for _, ok in checks) else "尚未就绪（FAIL）"))
    return 0 if info["update_safe"] and info["secret_fields_nonempty"] == 0 else 2


def resolve_app_dir(value: str = "") -> Path:
    """--app-dir 可传 app 目录（含 run.py）或部署根（含 app/run.py），自动识别。"""
    if value:
        p = Path(value).expanduser().resolve()
        if (p / "run.py").is_file():
            return p
        if (p / "app").is_dir():
            return (p / "app").resolve()
        return p
    return (Path(__file__).resolve().parent.parent / "app").resolve()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["plan", "apply", "verify"], default="plan")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--app-dir", default="", help="app 目录（含 run.py）或部署根（含 app/run.py）")
    ap.add_argument("--profile-config", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    app_dir = resolve_app_dir(args.app_dir)
    profile = find_profile_config(args.profile_config)
    info = describe(root, app_dir, profile)
    if args.mode == "plan":
        return mode_plan(info, args.data_dir or str(Path("D:/PawBotData")))
    if args.mode == "apply":
        return mode_apply(info, args.data_dir, profile)
    return mode_verify(info, args.json)


if __name__ == "__main__":
    sys.exit(main())
