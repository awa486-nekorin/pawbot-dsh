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
import { dirname, join, resolve } from 'node:path'

const isWin = process.platform === 'win32'

/** 快照/迁移时不要往备份里塞的目录（大、可重建、或本身是备份） */
const SKIP_DIRS = new Set([
  'backups', 'brain', 'browser', 'album', 'image_cache', 'napcat',
  'removed-nonanime', 'bili_notes', 'logs',
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

function copyTree(src, dst, { overwrite = true, skipNames = new Set() } = {}) {
  if (!existsSync(src)) return 0
  mkdirSync(dst, { recursive: true })
  let count = 0
  for (const name of readdirSync(src)) {
    if (skipNames.has(name)) continue
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
      if (SKIP_DIRS.has(name)) continue
      count += copyTree(s, d, { overwrite, skipNames })
    } else if (st.isFile()) {
      if (!overwrite && existsSync(d)) continue
      mkdirSync(dirname(d), { recursive: true })
      copyFileSync(s, d)
      count += 1
    }
  }
  return count
}

/** 迁移：把 appDir/data 复制到外部目录（外部已有文件不覆盖），再把 appDir/data 换成联接。 */
function migrateToExternal(appData, external, log) {
  const result = { migrated: false, copied: 0, warnings: [] }
  const targetReal = linkTarget(external) || resolve(external)
  mkdirSync(external, { recursive: true })

  if (existsSync(appData)) {
    if (isLink(appData)) {
      const current = linkTarget(appData)
      if (current && resolve(current) !== targetReal) {
        result.warnings.push(`appDir/data 已是指向 ${current} 的联接，与 dataDir=${external} 不一致；保持原状`)
      }
      return result
    }
    // 真实目录：先不覆盖地复制，再改名保留现场，最后建联接
    result.copied = copyTree(appData, external, { overwrite: false })
    const old = `${appData}.migrated-${stamp()}`
    const renamed = retrySync(() => renameSync(appData, old))
    if (renamed) {
      if (createLink(appData, external, log)) {
        result.migrated = true
        log?.(`数据目录已迁到外部：${external}（旧目录留档 ${old}）`)
      } else {
        try {
          renameSync(old, appData)
        } catch {}
        result.warnings.push(`建立目录联接失败，继续用 ${appData}`)
      }
      return result
    }
    // Windows 上偶尔 EPERM（杀软/句柄没放）：复制已经完成，退一步「清空原目录再建联接」，
    // 数据在外部那份里；清空失败就保持原状（数据仍在，只是没迁成）。
    const cleaned = retrySync(() => rmSync(appData, { recursive: true, force: true }))
    if (cleaned && createLink(appData, external, log)) {
      result.migrated = true
      log?.(`数据目录已迁到外部：${external}（原目录已清空后重建联接）`)
      return result
    }
    result.warnings.push(`旧 data 目录改名失败（被占用），继续用原目录；外部副本已保留`)
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
  const appData = join(appDir, 'data')
  const wanted = String(dataDir || '').trim()
  let effective = appData

  if (wanted) {
    try {
      const result = migrateToExternal(appData, resolve(wanted), log)
      info.warnings.push(...(result.warnings || []))
      info.migrated = Boolean(result.migrated)
      if (!result.warnings.some((w) => w.includes('继续用'))) effective = resolve(wanted)
      info.external = effective !== appData
    } catch (error) {
      info.warnings.push(`迁移到外部数据目录失败：${error?.message || error}`)
    }
  }
  info.dataDir = effective

  if (info.version) {
    const marker = readMarker(effective)
    if (String(marker.version || '') !== info.version) {
      try {
        mkdirSync(join(effective, 'backups'), { recursive: true })
        const dir = join(effective, 'backups', `plugin-update-${info.version.replace(/[^\w.-]/g, '_')}-${stamp()}`)
        mkdirSync(dir, { recursive: true })
        let files = 0
        for (const name of SNAPSHOT_FILES) {
          const src = join(effective, name)
          if (!existsSync(src)) continue
          try {
            copyFileSync(src, join(dir, name))
            files += 1
          } catch {}
        }
        for (const name of SNAPSHOT_DIRS) {
          const src = join(effective, name)
          if (!existsSync(src)) continue
          files += copyTree(src, join(dir, name), { overwrite: true })
        }
        info.snapshot = dir
        writeMarker(effective, {
          version: info.version,
          last_snapshot: dir,
          updated_at: new Date().toISOString(),
        })
        const removed = pruneBackups(effective, keep)
        log?.(`数据守卫：版本 ${info.version} 变化，已快照 ${files} 个文件到 ${dir}` +
          (removed.length ? `（清理旧快照 ${removed.length} 份）` : ''))
      } catch (error) {
        info.warnings.push(`版本快照失败：${error?.message || error}`)
      }
    }
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
