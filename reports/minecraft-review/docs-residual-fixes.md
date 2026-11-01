# Minecraft 文档残余修复记录（2026-11-02）

> 角色：文档残余修复者；只改两处与源码/日志相反的过期陈述，不重写 verdict、不新增证据。
> 输入：`AGENTS.md`、`reports/minecraft-review/docs-fixes.md`、`reports/minecraft-review/docs-consistency-audit.md`、
> 五份 Minecraft 文档、最终复核报告、`app/pawbot/core.py`、`app/pawbot/minecraft_resident.py` 与
> `reports/minecraft-review/final-verify/11-guard-artifact.log`。
> 写入范围只有本文件、`docs/decisions-minecraft-resident.md`、`reports/minecraft-review/documentation-final.md`。
> **未改**源码/测试/配置/AGENTS/其它 docs/审计报告/历史日志；未启动游戏、浏览器、微软登录或部署，
> 未读取运行态人设、记忆、真实凭证或原存档，未重跑离线探针。

## 0. 快照与变更文件

- HEAD `3dff9ef`；`git status --porcelain` 默认计数仍为 **206** 条——`reports/` 在默认 untracked 模式折叠为
  一行 `?? reports/`，新增本记录不会改变该计数；`--untracked-files=all` 下为 287 条。本次只写入下列 3 个文件。
- 两个被修文档的修改前 mtime：`docs/decisions-minecraft-resident.md` 00:46:59、
  `reports/minecraft-review/documentation-final.md` 00:51:38（均早于本次 01:42 之后的修复）。
- 未改任何源码；`core.py`、`minecraft_resident.py` 仅作只读核对。

| 文件 | 改动 |
| --- | --- |
| `docs/decisions-minecraft-resident.md` | 决策 8 的紧急停止顺序：由「先撤销 → 停/回收 → 最后闩锁」改为「先闩锁并保持到重启 → 撤销（持久化诚实报告）→ 停/回收 → 写入排空」 |
| `reports/minecraft-review/documentation-final.md` | §3 守卫产物行：`7 intermediary / 0 yarn-only` 改为 8 个 intermediary Class 引用（含 `class_3324`）、1 个 official identity-name 引用 `MinecraftServer`、0 个 yarn-exclusive 引用，并给日志行号 |
| `reports/minecraft-review/docs-residual-fixes.md` | 本记录 |

## 1. 修复 1：决策 8 的闩锁顺序（latch-first，保持到重启）

### 旧文（修复前 `docs/decisions-minecraft-resident.md:33`）

> 8. **紧急停止不经模型，且必须诚实报告。** 宿主先撤销守卫授权，再取消并回收居民/侧车，最后闩锁到 PawBot 重启。

这条把闩锁写成最后一步，暗示撤销/停机期间存在宿主仍可启动居民的窗口，且与同文件 `:58`
「`minecraft_emergency_stop` 先关闸、再撤销/停止/回收」自相矛盾。

### 新文（当前 `docs/decisions-minecraft-resident.md:33`）

> 8. **紧急停止不经模型，且必须诚实报告。** 宿主先置本地闩锁（`_minecraft_estop`，先关闸）并保持到 PawBot 重启；再尝试撤销守卫授权（持久化按回执诚实报告：只有签名回执、或本进程此前记录过该守卫身份的签名回执后再收到已认证 `409`，才给出 `revoked`+`revoke_durable`；裸 `409` 只算内存闩锁，报 `already_revoked_unverified`）；随后取消并回收居民/侧车，等侧车进程确认回收、写入排空。

`:34-36` 的诚实 `ok:true` 要求句**逐字未改**（「`ok:true` 只应在每一步都确认时出现；……」）。

### 源码证据（逐行核对当前树）

- `app/pawbot/core.py:600`：`self._minecraft_estop = True`，在任何撤销/停止之前置闩锁；`:601` 才获取
  `_minecraft_lifecycle_lock()`，`:602-603` 摘除居民引用。
- `app/pawbot/core.py:604`：结果结构直接带 `latched: True`；`:608-609` 有居民时才进入
  `_minecraft_stop_session`。
- `app/pawbot/core.py:546-547`：`_minecraft_stop_session` 先 `_minecraft_guard_revoke` 并记录
  `revoked`/`revoke_durable`；`:548-553` 才调用居民 `estop`/`stop`；`:558-567` 居民没收干净时兜底关侧车；
  `:568-573` 首次撤销失败时停机后再确认一次（`already_revoked_unverified` 不重试）；`:574-578` 计算
  `sidecar_reaped` 与 `writes_drained`。
- `app/pawbot/core.py:497-532`：撤销持久化的诚实语义——签名回执才是 `True`；裸已认证 `409` 仅证明内存闩锁，
  除非本进程此前记录过**该守卫身份**的签名回执；否则 `already_revoked_unverified`。
- `app/pawbot/core.py:665-672`：`ok` 要求 `latched` + `revoked` + `revoke_durable` + `resident_stopped`
  + `sidecar_reaped` + `writes_drained` 全真且没有 `error`；`:673-682` 成功/不完整分别记录闩锁到重启。
- `app/pawbot/core.py:265`：`_minecraft_estop = False` 只在 `__init__` 复位；运行期没有其它清零点，
  因此闩锁保持到 PawBot 重启。
- `app/pawbot/minecraft_resident.py:1754-1759`：居民 `estop()` 先置 `self._latched = True` 再 `stop()`，
  与宿主闩锁语义一致（宿主闩锁更早）。
- 交叉核对已在位的文档：`docs/MINECRAFT-RESIDENT.md:434`（「先置本地闩锁……再确认守卫撤销，再取消并回收」）、
  `:457-460`（闩锁/撤销标记不会因重启 PawBot 之外的操作自动清除；`revoke_durable:false` 不当持久撤销）；
  `docs/MINECRAFT-ACCEPTANCE.md:74`（G6：裸 409 只证明内存闩锁）；`docs/MINECRAFT-PROTOCOL.md:320-325`
  （裸 409 = `revoked:false`/`revoke_durable:false`/`ok:false`/`already_revoked_unverified`，durable 只认
  本进程此前对该守卫身份的签名回执）。四处与本修复一致。

## 2. 修复 2：重映射计数 7 → 8（raw scan 为准）

### 旧文（修复前 `reports/minecraft-review/documentation-final.md:103`）

> `class major 61；7 intermediary / 0 yarn-only`

### 新文（当前 `reports/minecraft-review/documentation-final.md:103`）

> `class major 61；**8 个 intermediary Class 引用（含 `class_3324` PlayerManager）/ 0 个 yarn-exclusive 引用**；另 1 个 official identity-name 引用 `MinecraftServer`（raw scan `yarn_like_refs=1`）`
>
> 日志列：`final-verify/11-guard-artifact.log:82-87,96-97`

### 日志证据（`reports/minecraft-review/final-verify/11-guard-artifact.log`）

- `:82`：`intermediary(remapped)_refs=8`。
- `:83`：样本为 `class_1132, class_2561, class_310, class_3222, class_3244, class_3324, class_5218, class_5250`，
  明确包含 `net/minecraft/class_3324`（PlayerManager）。
- `:84-85`：`yarn_like_refs=1`，样本 `net/minecraft/server/MinecraftServer`。
- `:86-87`：`mojmap_like_refs=1`，同一样本。
- `:96`：`mappings.tiny` 的 class 行 official == intermediary == yarn，说明 `MinecraftServer` 在 1.20.1
  是 **official identity mapping**（不是 yarn-exclusive）；`:97` 官方 merged jar 含该类，引用可在运行时解析。
- `:98-99` 的 summary 仍写「7 intermediary Class refs」且样本漏掉 `class_3324`，并列举 dev jar 的 7 个 yarn
  引用；这是历史日志自身的摘要不一致。报告与文档均以 `:82` 的 raw scan 为准（最终报告 `:84` 已注明），
  本修复也让定稿记录与 raw scan 对齐。

### 已与其它文档交叉核对（无需再改）

- `reports/minecraft-review/final-verification-2026-11-02.md:84`（E10）：8 个 intermediary Class 引用、含
  `class_3324`、1 个 `MinecraftServer` official identity mapping、0 个 yarn-exclusive，且注明 summary 与 raw scan
  不一致时以 raw scan 为准；`:304`（§9）：`remap=8 intermediary Class refs / 0 yarn-only refs (...) /
  MinecraftServer = official identity name`。
- `docs/MINECRAFT-ACCEPTANCE.md:45`（E10）：8 个 intermediary（含 `class_3324`）/ 0 yarn-only；另有 1 个
  yarn 名引用 `MinecraftServer`（official identity mapping）。
- `docs/MINECRAFT-RESIDENT.md:83`：8 个 intermediary / 0 yarn-only；另 1 个 yarn 名引用 `MinecraftServer`
  属 official identity mapping。

## 3. 复跑 grep：范围、方法与结果

范围固定为**五份 Minecraft 文档 + 最终复核报告 + 定稿记录**（不含本记录，也不含审计/修复记录与历史日志；
本记录为保留 old→new 对照而刻意引用旧句，故不参与扫描）：

```
docs/MINECRAFT-PROTOCOL.md
docs/MINECRAFT-RESIDENT.md
docs/MINECRAFT-ACCEPTANCE.md
docs/ARCHITECTURE-BASELINE.md
docs/decisions-minecraft-resident.md
reports/minecraft-review/final-verification-2026-11-02.md
reports/minecraft-review/documentation-final.md
```

方法：PowerShell 7，`Get-Content -LiteralPath <file> -Encoding utf8` 逐行 `.Contains(<phrase>)`（字面量、区分大小写）。
每条短语对 7 个文件逐行扫描；结果为**修复前 → 修复后**的命中数。

| 检索词 | 修复前 | 修复后 | 修复前命中位置 |
| --- | --- | --- | --- |
| `不会被撤销打断` | 0 | **0** | — |
| `回退到脚下` | 0 | **0** | — |
| `无观测时回退` | 0 | **0** | — |
| `7 intermediary` | 1 | **0** | `reports/minecraft-review/documentation-final.md:103` |
| `7 个 intermediary` | 0 | **0** | — |
| `33 条` | 0 | **0** | — |
| `55/45` | 0 | **0** | — |
| `先撤销守卫授权` | 1 | **0** | `docs/decisions-minecraft-resident.md:33` |
| `最后闩锁` | 1 | **0** | `docs/decisions-minecraft-resident.md:33` |

修复前总命中 3 条（2 个文件）；修复后 **9 个检索词全部 0 命中**。没有把 `dev jar` 的 7 个 yarn Class 引用
误删：最终报告 `:84` 的「日志 remap proof 段的 dev jar 对照列举了 7 个 yarn Class 引用」与 `:304` 的
`(dev jar: 7 yarn refs)` 是**另一个事实**（dev jar 的 yarn 引用列举），不是「最终 jar 7 个 intermediary」的旧计数。

## 4. 残余（不在本次写入范围，留给 owner）

1. **历史记录仍按原文引用旧句**：`reports/minecraft-review/docs-fixes.md`（如 `:23`、`:45-46`、`:88`、`:91-92`、
   `:120-121`、`:137-141`、`:154`、`:200`、`:213-219`、`:225`）与 `reports/minecraft-review/docs-consistency-audit.md`
   （如 `:30-33`、`:57-62`、`:93-103`、`:118`、`:131-133`、`:143-144`、`:149`、`:237-239`）按角色必须保留旧文引用。
   它们不是当前文档主张；本次未改写。`docs-fixes.md:213-219` 的残余 1/3 现已由本记录关闭。
2. **`11-guard-artifact.log:98-99` 的 summary 仍写 7 且漏 `class_3324`**：历史证据日志不在写入范围；
   定稿记录与最终报告均已注明以 `:82` raw scan 为准，并引用 `:96-97` 的 identity-mapping 证明。
3. **`app/pawbot/core.py:582` 的 docstring 仍写「先撤销守卫授权，再停常驻/侧车，最后闩死不再重启」**：
   源码不在写入范围，且实际代码 `:600` 先闩锁；没有任何文档再把它当作顺序依据。属于源码注释的后续清理项，
   不是文档主张。`core.py:535-539` 的 `_minecraft_stop_session` docstring 只描述**闩锁之后**的会话内顺序
   （revoke → stop → reap），与决策 8 不冲突。
4. **没有重跑探针/构建**：本次是离线文档修复；未新增证据日志，未把 `READY OFFLINE` 升级为实机结论。
5. **未做**：真实游戏/LAN/微软登录/真实存档/恢复演练/凭证/部署/服务重启；未改源码、测试、配置、AGENTS、
   sidecar/guard README 或其它文档。

## 5. 结论

- 两处残余均已按**当前源码**与**被引日志**修正：决策 8 现在是「先闩锁并保持到重启 → 撤销（持久化诚实报告）→
  停/回收 → 写入排空」，且保留了原有的诚实 `ok:true` 要求；定稿记录现在是「8 个 intermediary（含 `class_3324`）
  / 1 个 official identity-name `MinecraftServer` / 0 个 yarn-exclusive」。
- 指定 9 个过期短语在 7 个目标文件中的剩余命中为 **0**；历史审计/修复记录中的引用属于刻意保留，不影响
  当前文档与 `READY OFFLINE` 的范围。
