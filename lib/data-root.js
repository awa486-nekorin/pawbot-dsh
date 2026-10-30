/**
 * 统一数据根（DataRoot）：Config.dataDir > PAWBOT_DATA_DIR > <appDir>/data。
 *
 * 设计要点：
 *   - 只做「解析 + 可选迁移/快照」，绝不急切创建业务子目录（apply 时不写盘）；
 *   - 迁移/联接逻辑复用 lib/data-guard.js，失败时统一回退到 <appDir>/data，
 *     保证 Host 与 Python 指向同一个根（不双端分裂、不删除原数据）；
 *   - 子路径只有这一份定义，Host 的 config 读取、brain 工作目录、napcat、备份、
 *     以及未来的 authz/sessions/logs 都从这里取。
 *
 * 纯 Node（不依赖 DSH 宿主），可离线测试。
 */
import { mkdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

import * as dataGuard from './data-guard.js'

/** 环境变量名：独立运行 / DSH 托管的统一入口。 */
export const DATA_ROOT_ENV = 'PAWBOT_DATA_DIR'

/** 数据根下统一子路径（相对名）。改这里等于改契约。 */
export const DATA_ROOT_SUBPATHS = Object.freeze({
  config: 'config.json',
  persona: 'persona.md',
  memory: 'memory',
  backups: 'backups',
  brain: 'brain',
  napcat: 'napcat',
  logs: 'logs',
  authz: 'authz',
  sessions: 'sessions',
  tmp: 'tmp',
  runtimeLock: '.runtime.lock',
})

/** 需要按需创建的目录（不含文件）。 */
export const DATA_ROOT_DIRS = Object.freeze([
  'memory', 'backups', 'brain', 'napcat', 'logs', 'authz', 'sessions', 'tmp',
])

/** 把数据根展开成绝对路径表。 */
export function dataPaths(dataRoot) {
  const root = resolve(String(dataRoot || '.'))
  return {
    root,
    config: join(root, DATA_ROOT_SUBPATHS.config),
    persona: join(root, DATA_ROOT_SUBPATHS.persona),
    memory: join(root, DATA_ROOT_SUBPATHS.memory),
    backups: join(root, DATA_ROOT_SUBPATHS.backups),
    brain: join(root, DATA_ROOT_SUBPATHS.brain),
    napcat: join(root, DATA_ROOT_SUBPATHS.napcat),
    logs: join(root, DATA_ROOT_SUBPATHS.logs),
    authz: join(root, DATA_ROOT_SUBPATHS.authz),
    sessions: join(root, DATA_ROOT_SUBPATHS.sessions),
    tmp: join(root, DATA_ROOT_SUBPATHS.tmp),
    runtimeLock: join(root, DATA_ROOT_SUBPATHS.runtimeLock),
  }
}

/**
 * 按需创建统一子目录（幂等）；返回本次真正新建的目录名。
 * 只在确实需要落盘时调用（例如开始写日志/快照前），避免只读解析也写盘。
 */
export function ensureDataLayout(dataRoot, { names = DATA_ROOT_DIRS } = {}) {
  const root = resolve(String(dataRoot || '.'))
  mkdirSync(root, { recursive: true })
  const created = []
  for (const rel of names) {
    const dir = join(root, rel)
    const existed = existsDir(dir)
    mkdirSync(dir, { recursive: true })
    if (!existed) created.push(rel)
  }
  return { root, paths: dataPaths(root), created }
}

function existsDir(path) {
  try {
    return statSync(path).isDirectory()
  } catch {
    return false
  }
}

/**
 * 解析运行时数据根。
 *
 * @param {object} options
 * @param {string} options.appDir 运行时基目录（Python 的 BASE_DIR / django 的 run.py 所在目录）
 * @param {string} [options.configDataDir] 显式 Config.dataDir（最高优先级）
 * @param {object} [options.env] 环境变量表（默认 process.env）
 * @param {string} [options.version] 插件版本；非空时按旧语义做一次版本快照
 * @param {number} [options.keep] 保留几份快照
 * @param {boolean} [options.enabled] 数据守卫开关；关闭时忽略外部目录（旧行为）
 * @returns {{
 *   appDir:string, appData:string, dataRoot:string, requested:string,
 *   source:'config'|'env'|'default', enabled:boolean, external:boolean,
 *   migrated:boolean, warnings:string[], version:string, snapshot:string|null
 * }}
 */
export function resolveDataRoot({
  appDir,
  configDataDir = '',
  env = process.env,
  version = '',
  keep = 5,
  enabled = true,
  log,
} = {}) {
  const app = resolve(String(appDir || ''))
  const appData = resolve(join(app, 'data'))
  const fromConfig = String(configDataDir || '').trim()
  const fromEnv = String(env?.[DATA_ROOT_ENV] || '').trim()
  const requestedRaw = fromConfig || fromEnv
  const source = enabled
    ? (fromConfig ? 'config' : fromEnv ? 'env' : 'default')
    : 'default'
  const requested = requestedRaw ? resolve(requestedRaw) : ''

  let resolved = {
    appData,
    requested,
    effective: appData,
    external: false,
    migrated: false,
    warnings: [],
  }
  if (enabled && requested) {
    resolved = dataGuard.resolveDataDir({ appDir: app, dataDir: requested, log })
  }

  const dataRoot = resolve(resolved.effective || appData)
  const info = {
    appDir: app,
    appData,
    dataRoot,
    requested,
    source,
    enabled: Boolean(enabled),
    external: Boolean(resolved.external),
    migrated: Boolean(resolved.migrated),
    warnings: [...(resolved.warnings || [])],
    version: String(version || ''),
    snapshot: null,
  }

  if (enabled && info.version) {
    const snap = dataGuard.snapshotData({ dataDir: dataRoot, version: info.version, keep, log })
    info.snapshot = snap.snapshot
    info.warnings.push(...(snap.warnings || []))
  }
  return info
}
