/**
 * lib/hostdeps.js —— 從宿主那棵樹裡取 `@deepseek-ai/*`（拿不到就退到 bare import / 本地等價實現）
 * ============================================================================
 * 原因（參考 dsh-bili-subagent 的交接筆記）：`@deepseek-ai/dsh-tools` 與
 * `@deepseek-ai/schemastery` 在本插件是 optional peer，桌面版 profile
 * `autoInstallPeers: false` 不會裝 ⇒ bare import 常失敗。宿主把它們裝在自己的
 * node_modules 樹裡，所以按「可能性排序」去幾棵樹找，找到就用跟宿主同一份；
 * schemastery 徹底拿不到 ⇒ 返回 null（插件照常載入，只是沒有 Config、設置頁降級）。
 * ============================================================================
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'

/** 插件自己那棵樹（若有 node_modules） */
const SELF_MODULES = join(dirname(fileURLToPath(import.meta.url)), '..', 'node_modules', '@deepseek-ai')

/** 可能藏著宿主 `@deepseek-ai` 包的地方（不寫死某一台機器） */
function candidateRoots() {
  const out = []
  const push = (p) => {
    if (p && !out.includes(p)) out.push(p)
  }
  push(process.env.DSH_HOST_MODULES)

  const appdata = process.env.APPDATA
  if (appdata) {
    const npm = join(appdata, 'npm', 'node_modules')
    push(join(npm, '@deepseek-ai', 'dsh', 'node_modules', '@deepseek-ai'))
    push(join(npm, '@deepseek-ai'))
    try {
      for (const n of readdirSync(join(npm, '@deepseek-ai'))) {
        push(join(npm, '@deepseek-ai', n, 'node_modules', '@deepseek-ai'))
      }
    } catch {
      /* 沒有就算了 */
    }
  }
  push('/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai')
  push('/usr/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai')
  push('/usr/local/lib/node_modules/@deepseek-ai')
  push('/usr/lib/node_modules/@deepseek-ai')

  try {
    const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh')
    for (const p of readdirSync(join(dshHome, 'profiles'))) {
      push(join(dshHome, 'profiles', p, 'node_modules', '@deepseek-ai'))
    }
  } catch {
    /* 沒有就算了 */
  }

  push(SELF_MODULES)
  return out
}

/** 按包的 exports['.'] 找入口文件，返回 file:// URL */
function entryUrlFor(root, name) {
  try {
    const dir = join(root, name)
    const pkgFile = join(dir, 'package.json')
    if (!existsSync(pkgFile)) return null
    const pkg = JSON.parse(readFileSync(pkgFile, 'utf8'))
    const dot = pkg.exports?.['.']
    const rel = typeof dot === 'string' ? dot : (dot?.import || dot?.default || pkg.module || pkg.main)
    if (!rel) return null
    const file = join(dir, String(rel).replace(/^\.\//, ''))
    return existsSync(file) ? pathToFileURL(file).href : null
  } catch {
    return null
  }
}

async function fromHostTree(name) {
  for (const root of candidateRoots()) {
    if (root === SELF_MODULES) continue // 自己那份交給下面的 bare import 處理
    const url = entryUrlFor(root, name)
    if (!url) continue
    try {
      return await import(url)
    } catch {
      /* 換下一棵樹 */
    }
  }
  return null
}

/** 本地等價實現：只做「把扁平參數表轉成 object schema」這一件事，其餘原樣 */
function localDefineTool(options) {
  const properties = {}
  const required = []
  for (const [k, v] of Object.entries(options.parameters || {})) {
    properties[k] = v
    if (v && v.required === true) {
      required.push(k)
      delete v.required
    }
  }
  return {
    ...options,
    parameters: { type: 'object', properties, ...(required.length ? { required } : {}) },
  }
}

/**
 * 取回宿主半端要用的一點東西。
 * @returns {Promise<{z:any|null, defineTool:Function, notes:string[]}>}
 */
export async function loadHostDeps() {
  const notes = []

  // ① schemastery（Config 要用）
  let z = null
  let zMod = await fromHostTree('schemastery')
  if (!zMod) {
    try {
      zMod = await import('@deepseek-ai/schemastery')
    } catch {
      zMod = null
    }
  }
  z = zMod?.default ?? zMod ?? null
  if (!z) notes.push('schemastery 拿不到 —— 本插件沒有 Config（設置頁降級，本體照常跑）')

  // ② dsh-tools 的 defineTool（拿不到就用本地等價實現）
  let defineTool = null
  let dtMod = await fromHostTree('dsh-tools')
  if (!dtMod) {
    try {
      dtMod = await import('@deepseek-ai/dsh-tools')
    } catch {
      dtMod = null
    }
  }
  defineTool = dtMod?.defineTool || ((options) => localDefineTool(options))
  if (!dtMod?.defineTool) notes.push('dsh-tools 拿不到 —— 工具註冊用本地等價實現頂上')

  return { z, defineTool, notes }
}
