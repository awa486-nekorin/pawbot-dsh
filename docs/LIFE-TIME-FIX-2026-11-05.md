# 生活面板时间显示 1970 修复

日期：2026-11-05。本地和云端都出现「最近经历 / 下次活动」显示 `1970/1/22`。

## 原因

PawBot API（`/api/life` 等）的时间字段是 **epoch 秒**（例如 `1793806907` = 2026-11-04 23:41:47）。`client.js` 的 `fmtTime()` / `fmtRelative()` 直接 `new Date(值)`，把秒当成毫秒，得到 1970-01-21/22。

`fmtRelative()` 同时把 `Date.now()`（毫秒）和秒值相减，导致相对时间也变成巨大数、再回退到 `fmtTime()`，所以一条记录里会出现两个 1970 时间。

## 修改

- `client.js` 新增 `toEpochMs(value)`：`value < 1e12` 视为秒并乘 1000，已经大于等于 1e12 的毫秒值原样使用。
- `fmtTime()`、`fmtRelative()` 统一经过 `toEpochMs()`；`fmtWhen()` 自动跟随修复。
- `lastSuccessAt` 是前端本地 `Date.now()` 毫秒，保持原样，不做二次转换。
- 新增 `test/life-time-format.mjs`：直接读取 `client.js` 源码里的时间函数，断言：
  - `1793806907` 显示 2026，不出现 1970；
  - 秒和毫秒输入显示一致；
  - `1 小时后` 的相对时间以秒为基准。
- 原有 `test/client-meta-check.mjs` 32 项、`test/offline.mjs` 22 项保持通过。

执行结果：

```
life-time-format: ok {
  absolute: '2026/11/4 23:41:47',
  rel: '1 小时后',
  when: '2026/11/4 23:41:47 · 10 小时前'
}
client-meta-check: 32 passed, 0 failed
offline: 22 passed, 0 failed
```

## 部署

- 本地：插件目录是指向源码的符号链接，`client.js` 已更新；DSH 页面刷新后生效。
- 云端：`deploy_core.py --apply` 原子替换 `client.js`，本地/云端哈希一致：
  `C367F509D48489F343F3F3F6473955B8B00B1C2B149232CFB60A971F57DBDE71`
- 这是纯前端展示修复，不需要重启 PawBot/DSH；但浏览器若缓存旧 client 包，需要刷新页面（必要时重启 DSH 客户端）。

## 未验证

- 没有真实浏览器截图能力，未做视觉验收；逻辑已用直接读取 `client.js` 的 Node 回归验证。
- 如果刷新后仍有 1970，说明 DSH 客户端仍在用旧 client bundle，需要重启 DSH 客户端/插件重载，而不是后端时间错。
