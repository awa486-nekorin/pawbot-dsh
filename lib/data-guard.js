/**
 * 数据守卫：让 PawBot 的「人设 / 记忆 / API key」在 DSH 更新、插件重装、版本升级后不被冲掉。
 *
 * 两条保护，互相独立：
 *   1. 外部数据目录（可选）：把 `<appDir>/data` 变成指向外部目录的目录联接（Windows junction）
 *      或符号链接。插件升级/覆盖安装只会动 app/，不会碰外部目录；联接丢了下次启动会自动重建。
 *      迁移是「不覆盖」的：外部目录已有同名文件就保留外部那份（不会用插件模板盖掉你的 key）。
 *   2. 版本快照（默认开启）：每次插件版本变化时，在数据目录的 backups/ 下拷一份
 *      config.json + persona.md + 记忆文件；保留最近 N 份，随时可以手工捞回。
 *
 * 纯 Node（不依赖 DSH 宿主），方便离线测试与单独调用。
 */
import {
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { spawnSync } from 'node:child_process'
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  acquireRuntimeLock,
  inspectRuntimeLock,
  releaseRuntimeLock,
} from './instance-lock.js'

const isWin = process.platform === 'win32'

/** 快照/迁移时不要往备份里塞的目录（大、可重建、或本身是备份/运行锁元数据） */
const SKIP_DIRS = new Set([
  'backups', 'brain', 'browser', 'album', 'image_cache', 'napcat',
  'removed-nonanime', 'bili_notes', 'logs', '.locks',
])

/** 记忆/凭证/人设相关：版本升级时优先快照这些 */
const SNAPSHOT_FILES = [
  'config.json', 'persona.md',
  'sessions.json', 'relations.json', 'media_ledger.json', 'memes.json',
  'mood.json', 'emotion.json', 'libido.json', 'proactive_state.json',
  'bili_post.json', 'bili_scroll_seen.json', 'hobbies.json', 'album.json',
]
const SNAPSHOT_DIRS = ['memory', 'people', 'longmem', 'diary', 'history']

const MARKER = '.data-guard.json'

export function readPluginVersion(pluginDir) {
  try {
    const pkg = JSON.parse(readFileSync(join(pluginDir, 'package.json'), 'utf8'))
    return String(pkg.version || '')
  } catch {
    return ''
  }
}

export function isLink(path) {
  try {
    return lstatSync(path).isSymbolicLink()
  } catch {
    return false
  }
}

export function linkTarget(path) {
  try {
    return realpathSync(path)
  } catch {
    return null
  }
}

function stamp() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}

/** 同步重试（Windows 上 rename/rm 目录偶尔被占用，等几十毫秒通常就好了）。 */
function retrySync(fn, attempts = 5, delayMs = 40) {
  let last
  for (let i = 0; i < attempts; i += 1) {
    try {
      fn()
      return true
    } catch (error) {
      last = error
      if (i < attempts - 1) {
        try {
          Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, delayMs)
        } catch {}
      }
    }
  }
  return false
}

function copyTree(src, dst, { overwrite = true, skipNames = new Set(), skipDirs = SKIP_DIRS } = {}) {
  if (!existsSync(src)) return 0
  mkdirSync(dst, { recursive: true })
  let count = 0
  for (const name of readdirSync(src)) {
    if (skipNames.has(name)) continue
    // 运行锁/临时锁文件属于「根的身份」，不是业务数据：迁移复制与快照都不能把
    // 源根正在使用的锁（或崩溃残留的 .runtime.lock.tmp-*）带到目标目录。
    if (name === '.runtime.lock' || name.startsWith('.runtime.lock.')) continue
    const s = join(src, name)
    const d = join(dst, name)
    let st
    try {
      st = lstatSync(s)
    } catch {
      continue
    }
    if (st.isSymbolicLink()) continue
    if (st.isDirectory()) {
      if (skipDirs.has(name)) continue
      count += copyTree(s, d, { overwrite, skipNames, skipDirs })
    } else if (st.isFile()) {
      if (!overwrite && existsSync(d)) continue
      mkdirSync(dirname(d), { recursive: true })
      copyFileSync(s, d)
      count += 1
    }
  }
  return count
}

/**
 * 规范化路径：尽量做 realpath；路径还不存在时，解析最近的已存在祖先再拼回剩余段。
 * 这样 junction / symlink 造成的「词法不同但实际相同/包含」能被发现。
 */
function canonicalPath(input) {
  const abs = resolve(String(input || '.'))
  let current = abs
  const tail = []
  for (let i = 0; i < 64; i += 1) {
    try {
      const real = realpathSync(current)
      return resolve(join(real, ...tail))
    } catch (error) {
      if (error?.code !== 'ENOENT' && error?.code !== 'ENOTDIR') return abs
      const parent = dirname(current)
      if (parent === current) return abs
      tail.unshift(basename(current))
      current = parent
    }
  }
  return abs
}

function comparable(input) {
  const text = canonicalPath(input)
  return isWin ? text.toLowerCase() : text
}

/** parent 与 child 相同，或 parent 是 child 的祖先。 */
function covers(parent, child) {
  const rel = relative(comparable(parent), comparable(child))
  return rel === '' || (!isAbsolute(rel) && rel !== '..' && !rel.startsWith(`..${sep}`))
}

/** 迁移前确认没有仍然活着的运行锁：不搬走正在运行的根、也不搬走它的锁。 */
function liveLockOn(...roots) {
  for (const root of roots) {
    if (!root) continue
    try {
      const state = inspectRuntimeLock(root)
      if (state.present && state.alive !== false) return { root, state }
    } catch {
      // 读不到就按未知处理更安全：只影响是否迁移，不删除任何数据。
      return { root, state: { present: true, alive: null, error: '运行锁状态读取失败' } }
    }
  }
  return null
}

/**
 * 锁 helper 用的 Python：生产环境可能只有 `app/.venv` 而没有系统 Python。
 * 显式 env 优先交给 instance-lock 自己处理（它是协议唯一实现）。
 */
function lockPython() {
  if (String(process.env.PAWBOT_PYTHON_EXE || '').trim()) return undefined
  const candidates = isWin
    ? [join(dirname(fileURLToPath(import.meta.url)), '..', 'app', '.venv', 'Scripts', 'python.exe')]
    : [
        join(dirname(fileURLToPath(import.meta.url)), '..', 'app', '.venv', 'bin', 'python3'),
        join(dirname(fileURLToPath(import.meta.url)), '..', 'app', '.venv', 'bin', 'python'),
      ]
  for (const candidate of candidates) {
    try {
      if (statSync(candidate).isFile()) return candidate
    } catch {}
  }
  return undefined
}

/**
 * 在复制/改名/快照窗口内预约某个数据根的运行锁（写 `<root>/.runtime.lock`，
 * 内容 owner = 当前 Node 进程）。这是与 Python run.py 共用的同一套单实例协议，
 * 因此窗口内合作实例会在 acquire 阶段得到 LOCKED，而不是「检查之后才开始跑」。
 *
 * helper 只是短命进程：acquire 返回后没有任何长期打开的 guard 句柄，
 * 所以 Windows 上不会因为「Node 持 open guard」而 rename 失败。
 *
 * helper 不可用、超时或失败时必须拒绝迁移/快照；Host 无法发现解释器
 * 并不代表不存在另一个使用独立 Python/venv 的实例。
 */
function reserveRuntimeRoot(root, { purpose = 'guard', log } = {}) {
  const target = resolve(String(root || '.'))
  const pythonExe = lockPython()
  let acquired
  try {
    acquired = acquireRuntimeLock({
      dataRoot: target,
      version: `data-guard:${purpose}`,
      pythonExe,
      log,
    })
  } catch (error) {
    return { ok: false, code: 'RESERVE_FAILED', message: `预约运行锁异常：${error?.message || error}` }
  }
  if (acquired?.ok === true && acquired.lock?.nonce) {
    return {
      ok: true,
      held: true,
      root: target,
      nonce: String(acquired.lock.nonce),
      pythonExe,
    }
  }
  const code = String(acquired?.code || 'RESERVE_FAILED')
  const message = String(acquired?.message || (acquired?.ok ? '运行锁缺少 nonce' : '预约运行锁失败'))
  return { ok: false, code, message }
}

/** 释放预约锁；`reservation.root` 由调用方在改名/回滚后更新为新路径（锁随目录走）。 */
function releaseReservation(reservation, { warnings, log } = {}) {
  if (!reservation || !reservation.held || reservation.lost) return false
  let released
  try {
    released = releaseRuntimeLock({
      dataRoot: reservation.root,
      nonce: reservation.nonce,
      pythonExe: reservation.pythonExe,
    })
  } catch (error) {
    released = { ok: false, code: 'RELEASE_FAILED', message: String(error?.message || error) }
  }
  if (released.ok === true) return true
  const message = `释放运行锁失败（${released.code || 'RELEASE_FAILED'}）：${released.message || '未知原因'}；` +
    `锁仍留在 ${reservation.root}，确认没有 PawBot 在跑后可手工删除`
  warnings?.push(message)
  log?.(message)
  return false
}

function inspectRootLock(root) {
  try {
    return inspectRuntimeLock(root)
  } catch (error) {
    return { present: true, lock: null, alive: null, stale: false, error: String(error?.message || error) }
  }
}

/**
 * 复检预约是否仍然成立。held=false（helper 降级）时退化为只读检查：
 * 复制期间出现活锁/未知锁都当作冲突，绝不带着别人的活锁继续 rename。
 */
function verifyReservation(reservation, root) {
  const state = inspectRootLock(root)
  if (reservation?.held) {
    if (!state.present) return { ok: false, reason: '运行锁在中途消失', state }
    if (!state.lock) return { ok: false, reason: `运行锁无法解析（${state.error || '未知'}）`, state }
    if (state.lock.pid !== process.pid || state.lock.nonce !== reservation.nonce) {
      return { ok: false, reason: `运行锁已被 pid=${state.lock.pid} 的另一实例替换`, foreign: true, state }
    }
    if (state.alive === false) {
      return { ok: false, reason: '运行锁 owner 被判为不存活（状态异常）', state }
    }
    return { ok: true, state }
  }
  if (!state.present) return { ok: true, state }
  if (!state.lock) return { ok: false, reason: `运行锁无法解析（${state.error || '未知'}）`, state }
  if (state.alive === false) return { ok: true, stale: true, state }
  return { ok: false, reason: `出现未预约的运行锁（pid=${state.lock.pid}）`, foreign: true, state }
}

/**
 * 迁移：把 appDir/data 复制到外部目录（外部已有文件不覆盖），再把 appDir/data 换成联接。
 *
 * 失败策略（批次 2 明确承诺）：**绝不 rm 原目录兜底**。改名失败就保留原目录、
 * 回退为「没迁移」，外部已经复制好的副本留着（下次可继续）；数据不会被删除。
 */
function migrateToExternal(appData, external, log) {
  const result = { migrated: false, copied: 0, archived: null, warnings: [] }
  if (comparable(external) === comparable(appData)) {
    result.warnings.push('外部数据目录与 appDir/data 相同，跳过迁移')
    return result
  }
  // 祖先/后代重叠（含 junction/symlink 解析后）会自我复制甚至形成循环联接。
  if (covers(external, appData) || covers(appData, external)) {
    result.warnings.push(
      `外部数据目录 ${external} 与 ${appData} 存在包含关系（解析联接后仍重叠）；拒绝迁移以避免自我复制/迁移`)
    return result
  }
  if (existsSync(appData)) {
    if (isLink(appData)) {
      const current = linkTarget(appData)
      if (current && comparable(current) !== comparable(external)) {
        result.warnings.push(`appDir/data 已是指向 ${current} 的联接，与 dataDir=${external} 不一致；保持原状`)
      }
      return result
    }
    const live = liveLockOn(appData, external)
    if (live) {
      result.warnings.push(
        `运行锁仍被使用（pid=${live.state.lock?.pid ?? '?'}），活根不迁移、快照也跳过；` +
        '请先停止 PawBot 再改 dataDir')
      return result
    }
    // 预约源/目标运行锁：把「检查 → copy → rename」整段窗口放进与 Python run.py
    // 共用的单实例协议里。只读预检无法阻止 copy 期间新实例启动；预约锁让合作实例
    // 在 acquire 阶段就得到 LOCKED。绝不长期持有打开的 guard 句柄（helper 立即退出），
    // 因此 Windows 上不会因 guard 被打开而挡住 rename。
    const migration = reserveRuntimeRoot(
      join(dirname(appData), `.${basename(appData)}.pawbot-migration`),
      { purpose: 'migration-path', log },
    )
    if (!migration.ok) {
      result.warnings.push(`无法预约稳定迁移路径：${migration.message}；本次不迁移`)
      return result
    }
    const source = reserveRuntimeRoot(appData, { purpose: 'migrate-source', log })
    if (!source.ok) {
      releaseReservation(migration, { warnings: result.warnings, log })
      result.warnings.push(
        `无法预约源数据根运行锁（${source.code}）：${source.message}；` +
        '活根不迁移、快照也跳过，请先停止 PawBot 再改 dataDir')
      return result
    }
    let target = null
    try {
      try {
        // 目标目录可能还不存在（首次迁移）：先建出来，再复制/建立联接。
        mkdirSync(external, { recursive: true })
      } catch (error) {
        result.warnings.push(`外部数据目录不可用：${error?.message || error}`)
        return result
      }
      target = reserveRuntimeRoot(external, { purpose: 'migrate-target', log })
      if (!target.ok) {
        result.warnings.push(
          `无法预约外部数据根运行锁（${target.code}）：${target.message}；` +
          '外部目录可能已有 PawBot 在跑，本次不迁移')
        return result
      }
      // 真实目录：先不覆盖地复制，再改名保留现场，最后建联接
      result.copied = copyTree(appData, external, { overwrite: false })
      // 复制后、改名前复检：copy 是最长窗口，活锁可能在这期间发布。
      const preSource = verifyReservation(source, appData)
      if (!preSource.ok) {
        // 锁已被别人替换/变得不可判定：不碰这把锁，也不 rename。
        source.lost = true
        result.warnings.push(
          `复制完成、改名前复检失败：${preSource.reason}；已回退（未改名），原目录与外部副本均保留`)
        return result
      }
      // 同一秒内重复迁移 / 上次留档还在时，不能撞名（撞名会让 rename 失败而误判"被占用"）。
      const base = `${appData}.migrated-${stamp()}`
      let old = base
      for (let i = 1; existsSync(old) && i < 100; i += 1) old = `${base}-${i}`
      if (!retrySync(() => renameSync(appData, old))) {
        result.warnings.push(
          `旧 data 目录改名失败（被占用），已保留原目录并回退；外部副本已保留，不会删除任何原数据`)
        return result
      }
      // 锁文件随目录改名到了 old：后续释放必须用原 nonce + 新路径，不能再看 appData。
      source.root = old
      // 改名后复检：覆盖「检查与 rename 之间」以及 review 注入的活锁发布。
      const postSource = verifyReservation(source, old)
      if (!postSource.ok) {
        source.lost = true
        const restored = retrySync(() => renameSync(old, appData))
        if (restored) source.root = appData
        result.warnings.push(restored
          ? `迁移过程中数据根出现运行锁（${postSource.reason}）；已回滚改名，原数据与外部副本均保留`
          : `迁移过程中数据根出现运行锁（${postSource.reason}）；回滚改名失败，原数据保留在 ${old}，外部副本保留`)
        return result
      }
      // 建联前再确认目标根没有被第二个实例占用（复制期间目标锁同样可能被外部干扰）。
      const preLinkTarget = verifyReservation(target, external)
      if (!preLinkTarget.ok) {
        target.lost = true
        const restored = retrySync(() => renameSync(old, appData))
        if (restored) source.root = appData
        result.warnings.push(restored
          ? `外部数据根在迁移过程中被其它实例占用（${preLinkTarget.reason}）；已回滚，原目录与外部副本均保留`
          : `外部数据根在迁移过程中被其它实例占用（${preLinkTarget.reason}）；回滚改名失败，原数据保留在 ${old}，外部副本保留`)
        return result
      }
      // 源目录改名后到建立联接之间，app/data 词法上短暂不存在；若有人在这个间隙
      // 新建了目录/实例，不抢建联接、不删除对方目录，保留旧目录与外部副本（诚实边界）。
      if (existsSync(appData)) {
        result.warnings.push(
          `源目录改名后、建立联接前出现了新的 ${appData}（可能有实例在切换间隙启动）；` +
          `已保留旧目录 ${old} 与外部副本，不建立联接、不删除新目录`)
        return result
      }
      if (createLink(appData, external, log)) {
        result.migrated = true
        result.archived = old
        log?.(`数据目录已迁到外部：${external}（旧目录留档 ${old}）`)
      } else {
        const restored = retrySync(() => renameSync(old, appData))
        if (restored) source.root = appData
        result.warnings.push(restored
          ? `建立目录联接失败，已恢复原目录 ${appData}`
          : `建立目录联接失败；原目录保留在 ${old}（未删除），请手动恢复`)
      }
      return result
    } finally {
      // 释放「新路径」上的预约锁：目标锁在 external；源锁随目录搬到了 old（或回滚后的
      // appData），必须用原 nonce 释放，且丢失归属时绝不删除别人的锁。
      if (target && !target.lost) releaseReservation(target, { warnings: result.warnings, log })
      if (!source.lost) releaseReservation(source, { warnings: result.warnings, log })
      releaseReservation(migration, { warnings: result.warnings, log })
    }
  }

  try {
    // 干净安装时 app/data 不存在：目标目录必须先存在才能建立联接。
    mkdirSync(external, { recursive: true })
  } catch (error) {
    result.warnings.push(`外部数据目录不可用：${error?.message || error}`)
    return result
  }
  if (createLink(appData, external, log)) {
    result.migrated = true
    log?.(`数据目录已联接到外部：${external}`)
  } else {
    result.warnings.push('建立目录联接失败（不建议在无权限环境使用 dataDir）')
  }
  return result
}

function createLink(linkPath, targetPath, log) {
  mkdirSync(dirname(linkPath), { recursive: true })
  if (existsSync(linkPath)) {
    if (isLink(linkPath)) return true
    return false
  }
  if (isWin) {
    const r = spawnSync('cmd.exe', ['/c', 'mklink', '/J', linkPath, targetPath], {
      windowsHide: true,
      encoding: 'utf8',
    })
    const ok = existsSync(linkPath)
    if (!ok) log?.(`mklink /J 失败：${(r.stderr || r.stdout || r.status || '').toString().trim()}`)
    return ok
  }
  try {
    symlinkSync(targetPath, linkPath, 'dir')
    return existsSync(linkPath)
  } catch {
    return false
  }
}

/**
 * 解析「运行时真正生效的数据目录」。迁移逻辑从 prepareData 抽出来复用：
 *
 *   - dataDir 为空 → `<appDir>/data`；
 *   - dataDir === appDir/data → 不迁移（避免把自己搬走）；
 *   - 否则尝试迁移 + 联接，最终以**文件系统实际状态**决定 effective：
 *       · app/data 是联接 → effective = 联接目标（Python 看到的就是它）；
 *       · app/data 是真目录 → effective = app/data（迁移失败/未迁移）；
 *       · app/data 不存在（联接没建成）→ effective = app/data（两边都落到这里）。
 *
 * 这样 Host 与 Python 永远指向同一个根，不会因为迁移失败而双端分裂；失败时
 * 原目录原样保留，不删除任何数据。
 *
 * @returns {{appData:string,requested:string,effective:string,external:boolean,migrated:boolean,warnings:string[]}}
 */
export function resolveDataDir({ appDir, dataDir = '', log } = {}) {
  const appData = resolve(join(String(appDir || ''), 'data'))
  const wantedRaw = String(dataDir || '').trim()
  const wanted = wantedRaw ? resolve(wantedRaw) : ''
  const result = {
    appData,
    requested: wanted,
    effective: appData,
    external: false,
    migrated: false,
    warnings: [],
  }
  if (!wanted) return result
  if (comparable(wanted) === comparable(appData) && !isLink(appData)) {
    // 用户把 dataDir 指到 app/data 本身：保持真目录，不做自迁移。
    // 但如果 app/data 已经是指向该目标的联接，则继续往下跟随联接（external=true）。
    return result
  }

  let migration = { migrated: false, copied: 0, archived: null, warnings: [] }
  try {
    migration = migrateToExternal(appData, wanted, log)
  } catch (error) {
    migration = {
      migrated: false,
      copied: 0,
      archived: null,
      warnings: [`迁移到外部数据目录失败：${error?.message || error}`],
    }
  }
  result.migrated = Boolean(migration.migrated)
  result.warnings.push(...(migration.warnings || []))

  // 用实际文件系统状态收口：Python 只会认 <appDir>/data。
  if (isLink(appData)) {
    const target = linkTarget(appData)
    if (target) {
      // 用「app/data 的词法位置」与联接目标比较：appData 现在是联接，直接
      // realpath(appData) 会等于 target，不能用来判断循环。
      const appLocation = resolve(join(canonicalPath(dirname(appData)), basename(appData)))
      const inside = (parent, child) => {
        const rel = relative(parent, child)
        return rel === '' || (!isAbsolute(rel) && rel !== '..' && !rel.startsWith(`..${sep}`))
      }
      const appCmp = isWin ? appLocation.toLowerCase() : appLocation
      const targetCmp = comparable(target)
      if (inside(appCmp, targetCmp) || inside(targetCmp, appCmp)) {
        result.warnings.push(
          `appDir/data 的联接目标 ${target} 与自身存在包含关系（疑似循环联接）；按原目录处理`)
        result.effective = appData
        result.external = false
        return result
      }
      result.effective = resolve(target)
      // 注意：此时 appData 已经是联接，realpath(appData) === target，不能拿来比较；
      // 用 app/data 的词法位置判断它是否真的被换成了外部目标。
      result.external = targetCmp !== appCmp
      if (result.external && comparable(target) !== comparable(wanted)) {
        result.warnings.push(
          `appDir/data 已是指向 ${target} 的联接，与 dataDir=${wanted} 不一致；跟随实际联接目标（避免双端分裂）`,
        )
      }
      return result
    }
    result.warnings.push('appDir/data 是联接但无法解析目标；继续用原目录')
  }
  if (existsSync(appData)) {
    result.effective = resolve(appData)
    result.external = false
    return result
  }
  // app/data 不存在：联接没建成，统一退回 app/data，确保 Host 与 Python 看到同一个根。
  result.effective = appData
  result.external = false
  if (existsSync(wanted)) {
    result.warnings.push(`appDir/data 不存在且建立联接失败；Host 与 Python 统一使用 ${appData}`)
  }
  return result
}

/**
 * 版本快照（从 prepareData 抽出，供 data-root 复用）。写失败只回 warning，不抛。
 * @returns {{snapshot:string|null, warnings:string[]}}
 */
export function snapshotData({ dataDir, version = '', keep = 5, log } = {}) {
  const result = { snapshot: null, warnings: [] }
  if (!dataDir || !version) return result
  const marker = readMarker(dataDir)
  if (String(marker.version || '') === String(version)) return result
  // 快照是写入型操作：只读 preflight 与真正写入之间同样存在 TOCTOU。预约运行锁，
  // 让窗口内启动的合作实例在 acquire 阶段被拒绝；快照结束（或失败）必须释放。
  const reservation = reserveRuntimeRoot(dataDir, { purpose: `snapshot:${version}`, log })
  if (!reservation.ok) {
    result.warnings.push(`运行锁被占用或无法预约，本次跳过版本快照：${reservation.message}`)
    return result
  }
  try {
    // 拿到预约后重读 marker：并发快照的第二个进程此时应看到标记并跳过。
    const current = readMarker(dataDir)
    if (String(current.version || '') === String(version)) return result
    mkdirSync(join(dataDir, 'backups'), { recursive: true })
    const dir = join(dataDir, 'backups',
      `plugin-update-${String(version).replace(/[^\w.-]/g, '_')}-${stamp()}`)
    mkdirSync(dir, { recursive: true })
    let files = 0
    for (const name of SNAPSHOT_FILES) {
      const src = join(dataDir, name)
      if (!existsSync(src)) continue
      try {
        copyFileSync(src, join(dir, name))
        files += 1
      } catch {}
    }
    for (const name of SNAPSHOT_DIRS) {
      const src = join(dataDir, name)
      if (!existsSync(src)) continue
      files += copyTree(src, join(dir, name), { overwrite: true })
    }
    result.snapshot = dir
    writeMarker(dataDir, {
      version: String(version),
      last_snapshot: dir,
      updated_at: new Date().toISOString(),
    })
    const removed = pruneBackups(dataDir, keep)
    log?.(`数据守卫：版本 ${version} 变化，已快照 ${files} 个文件到 ${dir}` +
      (removed.length ? `（清理旧快照 ${removed.length} 份）` : ''))
  } catch (error) {
    result.warnings.push(`版本快照失败：${error?.message || error}`)
  } finally {
    releaseReservation(reservation, { warnings: result.warnings, log })
  }
  return result
}

function readMarker(dataDir) {
  try {
    return JSON.parse(readFileSync(join(dataDir, MARKER), 'utf8')) || {}
  } catch {
    return {}
  }
}

function writeMarker(dataDir, marker) {
  try {
    writeFileSync(join(dataDir, MARKER), JSON.stringify(marker, null, 2), 'utf8')
  } catch {}
}

export function listBackups(dataDir) {
  const root = join(dataDir, 'backups')
  try {
    return readdirSync(root)
      .filter((name) => {
        try {
          return statSync(join(root, name)).isDirectory()
        } catch {
          return false
        }
      })
      .sort()
      .reverse()
      .map((name) => {
        const p = join(root, name)
        let bytes = 0
        try {
          const stack = [p]
          while (stack.length) {
            const cur = stack.pop()
            for (const n of readdirSync(cur)) {
              const f = join(cur, n)
              const st = lstatSync(f)
              if (st.isDirectory()) stack.push(f)
              else if (st.isFile()) bytes += st.size
            }
          }
        } catch {}
        let mtime = 0
        try {
          mtime = statSync(p).mtimeMs
        } catch {}
        return { name, path: p, bytes, mtime }
      })
  } catch {
    return []
  }
}

export function createBackup(dataDir, label = 'manual') {
  const safe = String(label || 'manual').replace(/[^\w.-]+/g, '-').slice(0, 40)
  const dir = join(dataDir, 'backups', `${safe}-${stamp()}`)
  mkdirSync(dir, { recursive: true })
  let count = 0
  for (const name of SNAPSHOT_FILES) {
    const src = join(dataDir, name)
    if (!existsSync(src)) continue
    try {
      copyFileSync(src, join(dir, name))
      count += 1
    } catch {}
  }
  for (const name of SNAPSHOT_DIRS) {
    const src = join(dataDir, name)
    if (!existsSync(src)) continue
    count += copyTree(src, join(dir, name), { overwrite: true })
  }
  let manifest = { created_at: new Date().toISOString(), label: safe, files: count, source: dataDir }
  try {
    writeFileSync(join(dir, 'manifest.json'), JSON.stringify(manifest, null, 2), 'utf8')
  } catch {}
  return { ok: true, dir, files: count, manifest }
}

function pruneBackups(dataDir, keep) {
  const limit = Math.max(1, Number(keep) || 5)
  const backups = listBackups(dataDir)
  const removed = []
  for (const b of backups.slice(limit)) {
    try {
      rmSync(b.path, { recursive: true, force: true })
      removed.push(b.name)
    } catch {}
  }
  return removed
}

/**
 * 插件加载时调用：确保数据目录、按需迁移、版本变化时快照。
 * 签名与回传字段保持批次 1 兼容；迁移/联接逻辑已抽到 resolveDataDir 复用。
 * @returns {{enabled:boolean, dataDir:string, external:boolean, migrated:boolean, snapshot:string|null, warnings:string[], version:string}}
 */
export function prepareData({ appDir, dataDir = '', version = '', keep = 5, enabled = true, log } = {}) {
  const info = {
    enabled: Boolean(enabled),
    dataDir: '',
    external: false,
    migrated: false,
    snapshot: null,
    warnings: [],
    version: String(version || ''),
  }
  if (!info.enabled) return info
  if (!appDir) {
    info.warnings.push('appDir 为空，跳过数据守卫')
    return info
  }

  let resolved
  try {
    resolved = resolveDataDir({ appDir, dataDir, log })
  } catch (error) {
    // resolveDataDir 自己已经兜住迁移异常；这里再兜一层，保证 prepareData 永不抛。
    resolved = {
      appData: resolve(join(String(appDir), 'data')),
      requested: '',
      effective: resolve(join(String(appDir), 'data')),
      external: false,
      migrated: false,
      warnings: [`迁移到外部数据目录失败：${error?.message || error}`],
    }
  }
  info.dataDir = resolved.effective
  info.external = Boolean(resolved.external)
  info.migrated = Boolean(resolved.migrated)
  info.warnings.push(...(resolved.warnings || []))

  if (info.version) {
    const snap = snapshotData({ dataDir: info.dataDir, version: info.version, keep, log })
    info.snapshot = snap.snapshot
    info.warnings.push(...snap.warnings)
  }
  return info
}

/** 数据目录现状（给工具/面板看，不读 secret 值）。 */
export function describe(dataDir) {
  const backups = listBackups(dataDir)
  let marker = {}
  try {
    marker = readMarker(dataDir)
  } catch {}
  const counts = {}
  for (const name of SNAPSHOT_DIRS) {
    const p = join(dataDir, name)
    try {
      counts[name] = existsSync(p) ? readdirSync(p).length : 0
    } catch {
      counts[name] = -1
    }
  }
  return {
    data_dir: dataDir,
    exists: existsSync(dataDir),
    marker,
    backups: backups.slice(0, 10),
    backup_count: backups.length,
    dirs: counts,
    has_config: existsSync(join(dataDir, 'config.json')),
    has_persona: existsSync(join(dataDir, 'persona.md')),
  }
}
