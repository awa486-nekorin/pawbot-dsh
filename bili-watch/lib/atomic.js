/**
 * 原子寫檔 + JSON 讀寫（fs，零依賴）。
 */
import { writeFileSync, renameSync, readFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

/** 原子寫入：先寫 .tmp 再 rename（避免讀到半截檔案） */
export function atomicWriteFile(filePath, content) {
  mkdirSync(dirname(filePath), { recursive: true })
  const tmp = `${filePath}.tmp`
  writeFileSync(tmp, content, 'utf8')
  renameSync(tmp, filePath)
}

export function readJson(filePath, fallback) {
  try {
    if (!existsSync(filePath)) return fallback
    return JSON.parse(readFileSync(filePath, 'utf8'))
  } catch {
    return fallback
  }
}

export function writeJson(filePath, obj) {
  atomicWriteFile(filePath, JSON.stringify(obj, null, 2))
}
