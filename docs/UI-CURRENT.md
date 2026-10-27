# PawBot 设置面板 UI 现状盘点（UI-CURRENT）

> **用途**：给后续「UI 重规划」提供事实底稿。本文只描述**当前代码事实**，不给最终方案。
> **产出方式**：逐行只读核对代码，未修改任何源文件（唯一新增文件就是本 md）。
> **行号约定**：除特别注明文件名外，`行号` 一律指 `client.js`（共 3421 行）。

**事实来源**

| 文件 | 行数 / 大小 | 在本文中的角色 |
| --- | --- | --- |
| `client.js` | 3421 行 / 188 KB | 面板本体：字段元数据 + CSS + React 组件 + section 注册 |
| `index.js` | 1555 行 / 60 KB | Host 插件：生命周期、`/pawbot-ui/*` 路由挂载、setup/napcat/boot payload、Host 侧 Config（profile） |
| `lib/webui-proxy.js` | 595 行 | `/pawbot-ui/*` 真正的路由与动作实现（同源代理 + CSRF 守卫 + 遮罩） |
| `lib/config-utils.js` | 149 行 | `MASK` / `maskSecrets` / `pickUpdatable` / `deepMerge`（遮罩与合并语义的唯一出处） |
| `lib/setup.js` / `lib/napcat.js` | — | `setup/status`、`napcat/status`、`boot/status` 的真实返回形状 |
| `app/pawbot/webui.py` | 906 行 | PawBot 自己的 6185 网页后端（面板背后的真实数据源） |
| `app/web/index.html` | — | PawBot 自己的 6185 网页前端（**9 个 tab**，与面板不是同一套 UI） |
| `test/client-meta-check.mjs` | 450 行 | 字段计数与控件/ARIA 连线的离线校验（308 这个数字的出处） |

---

## 0. 一页速览（写方案前先记住这几个数字）

| 事实 | 数值 | 出处 |
| --- | --- | --- |
| 在 DSH 设置页注册的 section | 1 个：`id: 'pawbot'`, `order: 40`, `label: 'PawBot'` | `client.js:3411-3414` |
| tab 数量 / 顺序 | 4 个：启动 → 状态 → 设置 → 人设；**默认打开「启动」** | `client.js:3305-3314` |
| 字段元数据总数 | **308**（常用 73 + 高级 235） | `client.js:48/137`，`test/client-meta-check.mjs:247-249` |
| 8 个分组导航 | model / chat / proactive / memory / media / bili / qq / advanced | `client.js:422-431` |
| 高级字段的原始区段数 | 实际用 36 个（`SECTION_LABELS` 有 37 个键，`account` 没被高级字段用到）；最大 `tools` 50 项 | `client.js:411-420` |
| config 实际规模 | `config.py DEFAULT` 有 **37 个区段 / 309 个叶子**，元数据只覆盖 308 → **缺 1 个 `reply.tone_trim`**（离线校验因它失败） | `test/client-meta-check.mjs` 运行输出 |
| secret 控件数 | **14**（常用 1 + 高级 13） | `client.js` 元数据 `control: 'secret'` |
| 面板会打的 `/pawbot-ui/*` 路由 | 16 条路径 / 18 个处理分支（GET 8 + POST 10，其中 1 个别名） | `index.js:1457-1465`、`lib/webui-proxy.js:527-586` |
| CSS 钩子 / 主题 token | 101 个 `data-pb-*`（CSS 用到 99 个、JS 用到 100 个、交集 98）、31 个 `--dsw-*` | `client.js:669-939` |
| 响应式断点 | **1 个容器断点 760px**（容器查询，不是媒体查询） | `client.js:930-938` |
| 二次确认（ConfirmDialog） | 8 处引用 | 状态 3 / 设置 2 / secret 清除 1 / 人设 2 |
| 持久化前端状态 | **没有**：全文 0 处 `localStorage` / `sessionStorage` / `beforeunload` | 全文件检索 |

---

## 1. 整体框架

### 1.1 插件在 DSH 设置页里注册成什么

`client.js` 是「浏览器半端」，有两种加载路径（`client.js:944-948`）：

- **Node 分支**（`typeof window === 'undefined'`）：只把元数据挂到 `globalThis.__PAWBOT_PANEL_META__`，不碰 DOM/React —— 这是 `test/client-meta-check.mjs` 的入口；
- **浏览器分支**：`window.__ModuleLoader__.load({ id: '@local/pawbot', factory })`，在 factory 里 `require('react')` 并定义全部组件。

注册方式（`client.js:3397-3416`）：

```
return {
  inject: ['slots'],
  apply(ctx) {
    1) 注入样式：<style data-plugin-css="@local/pawbot">，textContent = CSS
       —— 不用 CSS Module / 不用 shadow DOM，靠 [data-pb-*] 属性选择器做作用域
    2) const slots = ctx.get('slots') || ctx.slots
    3) slots.inject('settings.section', () => slots.register(
         { name: 'settings.section', id: 'pawbot', order: 40, label: 'PawBot' },
         () => h(Panel),
       ))
  },
}
```

结论：**面板是 DSH 设置页里的一个 section 卡片/块，标题「PawBot」，排序 40**；它只往 `settings.section` 这一个 slot 里注册，不注册独立页面、不注册顶栏入口。

> 注意：Host 插件自身的 `Config`（`index.js:63-122`，26 个键：`appDir`、`autostart`、`onebotToken`、`brain*`、`webuiToken`、`napcatImage` …）**不在这 308 个字段里**，只能通过 DSH profile 的插件配置表单/配置文件改。面板改的是 PawBot 自己的 `data/config.json`（经 `/pawbot-ui/config`），两者是两套设置。

### 1.2 四个 tab：顺序、默认、挂载策略

```js
const TABS = [
  ['setup',   '启动'],
  ['status',  '状态'],
  ['config',  '设置'],   // 注意：id 是 config，文案是「设置」
  ['persona', '人设'],
]                                   // client.js:3305-3310
```

- **默认 tab = `setup`（启动）**：`useState('setup')`，注释写明「新用户第一眼就看到一键引导」（`client.js:3313-3314`）。
- **懒挂载 + 永不卸载**（`client.js:3316`）：`mounted = { setup:true, status:false, config:false, persona:false }`，首次切到某 tab 才 `setMounted(true)`；切走后不是卸载而是 **CSS 隐藏**（`hidden` + `data-pb-hidden { display:none }`，`client.js:685`）。这样切走再回来草稿还在（设置页未保存改动不会因为切换 tab 消失）。
- 页面顶部固定区（`client.js:3350-3355`）：`data-pb-eyebrow` = 「DSH 受管服务」、`data-pb-title` = 「PawBot 控制面板」、`data-pb-sub` = 一句话说明「不用再开 PawBot 自己的 6185 网页」。

### 1.3 tab 栏 DOM / ARIA

| 角色 | DOM | 属性 |
| --- | --- | --- |
| tab 容器 | `div[data-pb-tabs]` | `role="tablist"`、`aria-label="PawBot 控制面板标签页"`（`client.js:3356`） |
| tab 按钮 ×4 | `button[data-pb-tab]` | `id="pb-tab-<id>"`、`role="tab"`、`aria-selected`、`aria-controls="pb-panel-<id>"`、`tabIndex` 0/-1（roving）、`data-active`（当前项）、`onClick`、`onKeyDown`（`client.js:3357-3370`） |
| 面板 ×4 | `div[data-pb-tabpanel]` | `id="pb-panel-<id>"`、`role="tabpanel"`、`aria-labelledby="pb-tab-<id>"`、`tabIndex` 0/-1、`hidden`（非当前）+ `data-pb-hidden`（`client.js:3340-3348`） |

- tab 文案在设置页有未保存改动时会追加计数：`设置（3）`（`client.js:3370`）。
- 键盘：`ArrowRight/Down` 下一个、`ArrowLeft/Up` 上一个（循环）、`Home` 第一个、`End` 最后一个；均 `preventDefault()` 并移动焦点（`requestAnimationFrame(() => tabRefs.current[id]?.focus())`，`client.js:3320-3339`）。
- 面板与 tab 的对应关系在 `Panel` 里硬编码 4 次 `panelProps('setup'|'status'|'config'|'persona')`（`client.js:3381-3394`）；`test/client-meta-check.mjs:204-217` 会校验这些 ARIA 关系与方向键支持。

### 1.4 切换逻辑与「未保存提醒」

唯一逻辑在 `selectTab(id, focus = true)`（`client.js:3320-3329`）：

1. 如果**正从 config 切走且 `configDirty > 0`** → 设置 `tabNotice = '设置页还有 N 项未保存的改动；已切换标签页，改动还保留着，回到「设置」就能继续。'`；
2. 如果切到 config → 清空 `tabNotice`；
3. `setTab` + 懒挂载 + 聚焦新 tab。

`tabNotice` 渲染成一条 `warn` 级 `Notice`，右侧带操作按钮「回到设置」（`client.js:3371-3380`）。

草稿计数由 ConfigTab 通过 `onDirtyChange(dirtyCount)` 上报（`client.js:2759-2761`），`Panel` 用 `configDirty` 保存。

**关键事实**：整个面板**没有路由守卫、没有 `beforeunload`、没有「离开前确认」对话框**。关闭/刷新浏览器或 DSH 页面，设置页未保存改动会直接丢失；只有「切 tab」有提示。

### 1.5 页面容器与布局 / 网格 / 断点

| 容器 | 规则 | 出处 |
| --- | --- | --- |
| 根 | `[data-pb-root]`：`display:flex; flex-direction:column; gap:16px; max-width:1180px; font-size:14px; line-height:1.65`，并声明 `container-type: inline-size` | `client.js:670-674` |
| 设置页主体 | `[data-pb-config]`：`display:grid; grid-template-columns: 208px minmax(0,1fr); gap:16px; align-items:start` | `client.js:719` |
| 左导航 | `[data-pb-nav]`：`position:sticky; top:0; max-height:calc(100vh - 140px); overflow:auto; padding:10px; border:1px` | `client.js:721-723` |
| 右主区 | sticky 顶栏 `[data-pb-cfg-top]`（`top:0; z-index:6`）——搜索/开关/保存按钮吸顶；下方 `[data-pb-stack]` 卡片流 | `client.js:736-737`、`3118` |
| 字段网格 | `[data-pb-fields]` 默认 1 列；容器 ≥760px 变 `repeat(2, minmax(0,1fr))`（`column-gap:28px`） | `client.js:746`、`930-932` |
| 卡片流 | `[data-pb-stack]`：`flex column; gap:12px`；卡片 `[data-pb-card]`：`padding:14px 16px; border:1px; radius:lg` | `client.js:691-693` |

**断点只有两个容器查询（`@container`，依据根容器的内联宽度）**，放在 CSS 末尾（`client.js:930-938`）：

```css
@container (min-width:760px) { [data-pb-fields] { grid-template-columns: repeat(2, minmax(0,1fr)); } }
@container (max-width:759px) {
  [data-pb-config]   { grid-template-columns: minmax(0,1fr); }   /* 单列 */
  [data-pb-nav]      { position: static; max-height: none; }      /* 不再吸顶 */
  [data-pb-nav-list] { flex-direction: row; flex-wrap: wrap; }    /* 导航变横向芯片 */
  [data-pb-nav-btn]  { width: auto; }
}
```

唯一的媒体查询是 `@media (prefers-reduced-motion: reduce)`，只关掉步骤点的旋转动画（`client.js:888-890`）。**没有基于 `@media` 的移动端断点。**

**「nav 折叠」不是断点行为，而是一个手动状态**：`navCollapsed`（`client.js:2665`）为真时 `[data-pb-config][data-nav-collapsed]` 变单列（`720`）、`<nav>` 整块不渲染（`3040`），并在顶栏出现「显示分组导航」按钮（`3082-3086`）。

---

## 2. 元素清单（卡片 → 行 → 控件）

### 2.1 通用组件（全站复用）

| 组件 | 定义 | DOM / 属性 | 说明 |
| --- | --- | --- | --- |
| `Badge` | 957-959 | `span[data-pb-badge][data-tone]` | tone: good / warn / err / info / muted，圆角胶囊 |
| `Impacts` | 961-968 | `div[data-pb-impacts]` 内 N 个 `span[data-pb-badge]` | 输入 `item.impact` 数组，缺省回退 `['instant']` |
| `Switch` | 970-985 | `button[data-pb-sw][data-on][role=switch][aria-checked][aria-labelledby]`，内含 `<i>` 滑块 | 42×24px 自绘开关，`disabled` 时 `opacity:.45` |
| `Notice` | 987-993 | `div[data-pb-notice][data-tone]`，`role="alert"`（err）/ `role="status"`（其他） | 内联提示，右侧可选 `action` 按钮 |
| `InfoRow` | 995-1000 | `div[data-pb-info-row]` > `span[data-pb-info-label]` + `span[data-pb-info-value][data-tone]` | label 固定 150px，value 自适应；空值显示「—」 |
| `Kpi` | 1002-1007 | `div[data-pb-kpi][data-tone]` > label / num（28px）/ sub | 数字卡，tone 决定数字颜色 |
| `ConfirmDialog` | 1010-1098 | `div[data-pb-modal role=presentation]` > `div[data-pb-modal-card role=dialog aria-modal aria-labelledby tabindex=-1]` | 见下 |
| `NoticeBlock` | 1100-1116 | 组合 `Notice` + 可选「去状态页重启」按钮 | 只在 `notice.restart` 非空时出现按钮 |

**ConfirmDialog 的完整交互**（`client.js:1010-1098`）：

- 打开时记录 `document.activeElement`，把焦点放到「取消」按钮（禁用则放到卡片）；
- 焦点陷阱：`keydown` 捕获阶段监听 `Tab`/`Shift+Tab`，用 `querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])')` 过滤可见元素，首尾循环；
- `Escape` 关闭（`busy` 时不关），并 `stopPropagation`；
- `document focusin` 监听：焦点跑到卡片外就拉回；
- 关闭后把焦点还给之前的元素（`previous.isConnected` 判断）；
- 底部两个按钮：取消（默认文案「取消」）+ 确认（`confirmLabel`，`danger` 时红色描边、否则主色实心），`busy` 时确认按钮文案变「处理中…」且两者都禁用。
- **事实**：没有用 React Portal，节点就渲染在组件树当前位置；没有给 `body` 加滚动锁（CSS 只有 `position:fixed; inset:0`）。

**空态 / loading / 错误提示 / toast 现状**：

| 形态 | 实现 | 位置示例 |
| --- | --- | --- |
| 空态 | `div[data-pb-empty]` + `data-pb-help` 文案 | 分组无匹配：`'这个分组没有符合条件的设置。'`（2905）；搜索无结果卡（2984-2990） |
| loading | 纯文字 `data-pb-help`：「正在读取 PawBot 状态…」/「正在读取 PawBot 设置…」/「正在读取安装状态…」 | 2208、3019、1303、1411、1807 |
| 按钮 loading | `disabled` + 文案替换（「保存中…」「重启中…」「执行中…」「发送中…」「更新中…」…） | 多处 |
| 错误 | `Notice tone="err"` + 可选「再试一次 / 再读一次」按钮 | 状态 2195、设置 3021、环境 1298、NapCat 1405、人设 3216、启动进度 1798 |
| 成功/信息 | `Notice tone="good|info|warn"`（保存成功、已复制、看片结果…） | 1789、3117、1410、1746 |
| **toast** | **没有**。所有反馈都是内联 `Notice`，不会自动消失，也没有全局提示队列 | — |

### 2.2 SetupTab（启动页）—— 一键引导

组件 `SetupTab({ active, onGoStatus, onGoConfig })`，`client.js:1533-1862`。整体是「hero + 一条四步进度 + 三张操作卡 + 就绪卡」。

**数据与轮询**（三个独立轮询，都只在 `active` 为真时跑）：

| 数据 | 端点 | 周期 | 停止条件 |
| --- | --- | --- | --- |
| 环境状态 `setupSt` | `GET /setup/status` | 3000ms（1577） | 切走 tab 即清 interval + abort |
| NapCat 状态 `napcat` | `GET /napcat/status` | 3000ms（1601） | 同上 |
| 四步进度 `bootSt` | `GET /boot/status` | 1500ms（1641） | 同上；且 `data.done === true` 时 `stop()` 停表 |

细节：`mergeNapcat` 会在新响应没带 `qr` 且未登录时保留上一张二维码（`1593-1599` 调用，`1193-1199` 实现）；安装日志窗在 `logLength / setupPhase` 变化时自动滚到底（`1654-1657`）。

**① Hero 区**（不是卡片，`[data-pb-hero]`，`1768-1786`）

- `h1[data-pb-title]`「一键把 PawBot 跑起来」
- `p[data-pb-sub]`「装环境 → 连 NapCat → 启动 PawBot → 检查就绪，四步自动完成」
- 一行随状态变化的 `data-pb-help`（跑动中 / 有步骤失败 / 默认三段文案）
- 主按钮 `button[data-pb-btn][data-primary][data-hero]`「一键启动」，`aria-busy`，`bootBusy`（请求中 ∨ 有步骤 running ∨ `bootArmed`）时禁用、文案「执行中…」→ `POST /boot`（1663-1681，**无二次确认**）

**② 四步进度卡**（`section[data-pb-card]`，`1791-1820`）

- 头：`h2` 「四步进度」+ sub「来自 /pawbot-ui/boot/status，每 1.5 秒自动更新；done 为 true 就停」（完成后改文案「四步都跑完了，已经停止轮询」）
- 错误：`Notice err` + 「再读一次」（`refreshBoot`）
- 主体 `div[data-pb-stepper]`，固定 4 行（顺序写死 `BOOT_STEP_IDS = ['env','napcat','pawbot','ready']`，`1122-1128`），每行 `SetupStepRow`（`1250-1275`）：
  - 状态点 `span[data-pb-step-dot][data-state][role=img][aria-label]`：ok 显示 `✓`、fail `✕`、skip `⊘`、pending `·`、running 显示一个旋转小圈（`prefers-reduced-motion` 时静止）
  - 行体：`①…④`（`aria-hidden`）+ 名称 + 状态徽章（待办/进行中/完成/失败/跳过，tone muted/info/good/err/muted）
  - `detail` 一行说明（pending 时回退成该步 hint）
  - **失败行额外出现**：一段说明 + 按钮「重新执行启动流程」/「重新执行中…」→ 再跑 `POST /boot`
- 底部：`bootSt && !bootDone && !bootBusy` 时一行 help

**③ 环境卡 `SetupEnvCard`**（`1277-1364`，标题「环境」）

| 元素 | 内容 / 数据来源 |
| --- | --- |
| 卡头 sub | 「Python、venv、依赖与设定；每 3 秒自动更新」 |
| 错误 Notice | 「读不到安装状态：…」+「再读一次」 |
| loading | 「正在读取安装状态…」 |
| `InfoRow` Python | `st.python.version` → 「Python 3.x」或「未找到 Python」；tone good/err |
| `InfoRow` venv | `st.venv.path` + `· 已建立 / 还没建立`；tone good/warn |
| `InfoRow` 设定档 | `st.config.exists/valid` → 「还没有配置文件 / 配置文件已就绪 / 配置文件存在，但内容不合法」；tone good/err |
| `InfoRow` 应用目录 | `st.appDir`（有才显示） |
| 说明 | 没 Python 时给安装指引；`st.appPresent === false` 时给「程序还没放好」提示 |
| 状态旗标 `[data-pb-flags]` | 3 个 `span[data-pb-flag][data-tone]`：`pillow` / `websockets`（`st.deps.*`，✓/✕）+ `PawBot 程序`（`st.appPresent`） |
| 按钮 | 「只重跑环境安装」/「送出中…」→ `POST /setup/run {force:false}`（**force 在 UI 上永远 false**）+ 说明「只重装 venv 与依赖，不会动设定，也不会重启 PawBot」 |
| 安装日志窗（条件：`phase === 'installing'`） | `div[data-pb-log][role=log][aria-live=polite]`，最多最后 10 行 `span[data-pb-log-line][data-stream=stdout|stderr]`（stderr 红字），空时「（还没有日志）」；上方一行「安装进度：<message>（下面是日志最后 10 行，会自动滚动）」 |

**④ NapCat 卡 `SetupNapCatCard`**（`1366-1531`，标题「NapCat」）

| 元素 | 内容 / 数据来源 |
| --- | --- |
| 卡头 sub | 「侦测与连线；NapCat 登入后 PawBot 才收得到 QQ 消息」 |
| 错误 Notice | 「读不到 NapCat 状态：…」+「再读一次」 |
| 操作结果 Notice | 只在模式没变时显示上一次「重新侦测」的结果（`notice.mode === mode` 的防矛盾逻辑，1374-1377） |
| `InfoRow` 运行方式 | `st.mode` → docker「已用 Docker 启动」/ binary「使用本机已安装的 NapCat」/ none「这台机器没有 Docker，也没装 NapCat」 |
| `InfoRow` 进程状态 | `st.running` → 正在运行 / 没有在运行（mode≠none 才显示） |
| `InfoRow` 登入状态 | `st.loggedIn` → 「已登录，NapCat 已连上 PawBot」/「还没登录（用手机 QQ 扫码）」 |
| 状态旗标 | 「✓ 已登录…」/「· 还没登录」+ Docker 模式 / 本机 NapCat / ✕ 没有 NapCat |
| 没有 NapCat 时 | `Notice err` 一段后果说明 + 手动步骤 `ol[data-pb-manual]`（来自 `POST /napcat/setup` 的 `manual.steps`）+ 按钮「复制这些步骤」（`navigator.clipboard`，成功/失败都给一行 hint） |
| NapCat 登录页块（`st.webui.url` 存在时） | `div[data-pb-action]`：标题「NapCat 登录页」+ 说明「token 已自动带在网址里，链接本身就是凭证」+ 主按钮链接「打开 NapCat 登录页」（`a[data-pb-btn][data-primary][data-link][target=_blank][rel=noopener noreferrer]`）+ 按钮「查看 token / 收起 token」（`aria-expanded`）+ 展开后 `code[data-pb-token]` 显示明文 token |
| 二维码块 · URL 形式 | `div[data-pb-action]`：标题「登录二维码」+ 说明「二维码是抓取当次有效的」+ 链接「打开二维码」 |
| 二维码块 · 文件形式 | 同上标题，但不渲染图片：只给文件名/目录（`appDir` 之下）或完整路径，说明「面板不读取图片文件」 |
| 后端提示 | `st.hint` 一行 help |
| 按钮行 | 「重新侦测 / 重连 NapCat」（`busy==='setup'` 时「侦测中…」）→ `POST /napcat/setup`；「重新抓二维码」（`busy==='qr'` 时「抓取中…」）→ `POST /napcat/qr`；+ 说明「侦测只读本机状态；重连会请 PawBot 再连一次 NapCat」 |

**⑤ 就绪卡**（条件：`ready` 步 ok ∧ `napcat.loggedIn === true`，`1844-1861`）

- `div[data-pb-ready][role=status]`，成功色描边 + 14% 成功色混色底
- 文案：「✓ 都好了 — 去群里说句话试试」+ 副文案
- 两个按钮：「去状态页」（→ `selectTab('status')`）、「去设定页」（→ `selectTab('config')`）

**SetupTab 的二次确认**：**0 处**。所有动作（一键启动、重跑环境安装、重连 NapCat、抓二维码）都是「按了就跑」，靠按钮旁的后果说明。
**SetupTab 的全局提示**：`actErr` / `actInfo` 两条 `Notice`（1788-1789），任一动作失败/成功都会先清空再写。

### 2.3 StatusTab（状态页）

组件 `StatusTab({ onGoConfig, active })`，`client.js:1942-2360`。

- 轮询：`GET /status`，**5000ms**（1977），只在 `active` 时跑；另点「立即刷新」手动再抓一次（1985-1999）。
- `onGoConfig` 这个 prop **被传进来了但函数体里没用**（唯一使用点只有 3390 的传参）。

**① 顶部区**（`div[data-pb-top]`，2182-2193）
- `h1` 「PawBot 状态」+ sub「每 5 秒自动更新；离开这个标签页就停止，不会在后台一直请求主机」
- 按钮「立即刷新 / 更新中…」+ help「只重新抓一次状态，不会改任何设置」

**② 错误与时间戳**
- `Notice err`「读不到状态：…」+「再试一次」（2195-2199）
- 一行 help：`上次成功读取：<time datetime=ISO>本地时间</time>（浏览器收到响应的时间，不代表服务器采样时间）`；若当前处于错误态，改成「下方为上次成功读取的状态，可能已过时。」（2201-2204）

**③ 未读到状态时**：一张 `data-pb-card`，文案区分 loading /「尚未成功读取状态，无法判断 PawBot 是否运行。」（2206-2208）

**④ KPI 网格**（`div[data-pb-kpis]`，按 `auto-fit, minmax(160px,1fr)`，2068-2093、2210）

| KPI | value | sub | tone |
| --- | --- | --- | --- |
| 运行状态 | 「运行中 / 已停止」 | `PID <pid>` 或 `st.reason` /「PawBot 进程没在跑」 | good / err |
| 已运行 | `fmtDuration(uptime)`（`X 天 Y 时` / `X 时 Y 分` / `X 分 Y 秒`） | 从这次启动算起 | — |
| 连接数 | `pawbot.connections` 计数 | 「NapCat / WebSocket 连接」 | 0 连接时 warn |
| 今日看片（条件） | `todayWatchCount(pawbot)` 有值才出现 | 「B 站外接插件回报」 | — |

**⑤ 未运行时**：`Notice warn`「PawBot 目前没有在跑。可以到下面的「快捷操作」按重启…」（2212-2214）

**⑥ 卡「连接与会话」**（2217-2221，sub「把那包 JSON 翻成人话」）—— 一行一个 `InfoRow`（2095-2177）：

1. `PawBot 进程`：正在运行 / 没有在跑（good/err）
2. `WebUI 通道`：可连接 / 连不上（good/warn）
3. **连接明细**：`pawbot.connections` 前 6 条 → `连接 1..6`，值由 `describeConnection()` 拼（`QQ <self_id>・<nickname>・在线/离线・连上于 <时间>`）；超过 6 条加一行「其他连接：还有 N 条」；0 条时给一行 warn「目前没有连接（NapCat 可能没开或正在重连）」
4. `会话数`（`pawbot.sessions`）、`已加载插件`（`pawbot.plugins`）、`指令处理器`（`pawbot.handlers`）
5. `目前心情`（`humanMood`：label + 「N 分」）
6. `情绪主轴`（`humanEmotion`：优先 `dominant`，否则取 `values` 最大项 → `维度（分数）`）
7. `亲密度`（`humanLibido`：label + 「数值 N」）
8. `回复模型`（`pawbot.llm_model`）
9. `消息统计`：「收 N／送 N／错误 N」（`pawbot.stats`）
10. `主动观察中的群`：`pawbot.proactive.length + ' 群'`（仅当它是数组）
11. `最后错误`（`st.last_error`，err 色）、`上次退出`（`st.last_exit` → 「代码 x・信号 y・时间」）、`项目目录`（`process.project_dir`）
12. `pawbot === null`（WebUI 不通）时改成一行 warn：「PawBot WebUI 没有响应，所以拿不到连接、会话与心情数据。」

**⑦ 卡「快捷操作」**（2223-2303，sub「每颗按钮都写了后果；破坏性操作会再问一次」）

- 顶部 `NoticeBlock`（操作结果；此处 `onGoStatus: null`，所以永远不出「去状态页重启」按钮）
- 三个 `div[data-pb-action]` 区块（标题 + 说明 + 按钮行）：
  1. **重启 PawBot**：danger 按钮 → 打开 `ConfirmDialog`（标题「确定要重启 PawBot？」，danger，确认文案「确定重启」，正文列出 3 条后果：连接断开、正在生成/回复的消息丢失、短暂离线）
  2. **重载 PawBot 插件**：普通按钮 → `ConfirmDialog`（「确定要重载 PawBot 插件？」，正文 2 条：外挂内存状态重置 / 连接与配置不受影响）→ `POST /plugins/reload`
  3. **发测试消息**：折叠按钮「发测试消息 / 收起测试消息表单」→ 展开 `div[data-pb-form]`：
     - `label[for=pb-send-target]`「目标（群号或 QQ 号）」+ `input[data-pb-input][type=text]`（placeholder `123456789`）+ help
     - `label[for=pb-send-message]`「消息内容」+ `textarea[data-pb-textarea][rows=4]`
     - 行内：`Switch`「发到群」+ 一行「目前：发到群 / 发私聊」+ 主按钮「发送测试消息 / 发送中…」
     - 校验：目标或内容为空 → `Notice err` 就地报错，不弹确认（2032-2042）
     - 通过 → `ConfirmDialog`（「确定要真的发送这条消息？」，正文列出「目标：…（群聊/私聊）」「内容：…（>80 字截断）」）→ `POST /send {target,message,is_group}`；成功后清空消息正文、保留目标

**⑧ 卡「原始 JSON（/pawbot-ui/status）」**（2305-2316）
- `button[data-pb-fold][aria-expanded]` 折叠头（`▸` 图标，`data-open` 时旋转 90°）+ 说明「展开只会显示内容，不会改任何东西」
- 展开后 `pre[data-pb-pre]` 打印 `JSON.stringify(st, null, 2)`（`max-height:360px` 可滚）

**⑨ ConfirmDialog ×3**（restart / reload / send）：都渲染在 `StatusTab` 返回值尾部（2318-2359），不在任何卡片里。

### 2.4 ConfigTab（设置页）—— 本文档最核心的一页

组件 `ConfigTab({ onGoStatus, onDirtyChange })`，`client.js:2655-3150`。

**状态**（2656-2674，共 18 个 `useState`）：`cfg`、`maskedKeys`、`err`、`loading`、`dirty`(map key→值)、`searchDraft`、`search`、`showAdvanced`、`activeGroup`(默认 `'model'`)、`navCollapsed`、`rev`（重挂字段用的自增版本）、`rawOpen`、`rawText`、`rawErr`、`confirm`、`discardConfirm`、`saving`、`notice`。

**数据加载**：`GET /config` 只在挂载时一次（2699-2704），**没有轮询**；`applyServerConfig` 同时重置 `dirty`、`rawText`、`rev`（2676-2684）。

#### 2.4.1 顶部标题区（`data-pb-top`，3010-3014）

- `h1`「PawBot 设置」
- sub：「默认只显示常用设置；其余 **235** 个原始键收在「高级」。改完按保存，会先列出中文标签让你确认。」（数字来自 `ADVANCED_META.length`）

#### 2.4.2 读取失败态（`!cfg`，3016-3031）

- loading：一张卡 +「正在读取 PawBot 设置…」
- 失败：`Notice err`「读不到设置：…。可以到状态页确认 PawBot 有没有在跑。」+「再试一次」；下方一张卡说明「读取成功后才会出现设置表单；现在按保存不会送出任何东西。」

#### 2.4.3 主体两栏（`div[data-pb-config][data-nav-collapsed?]`，3036-3118）

**左：分组导航 `<nav data-pb-nav>`**（3040-3060）

- 头 `div[data-pb-nav-head]`：`span[data-pb-nav-title]`「分组导航」+ 按钮「收起」
- help 一行：「点一组只看那一组；不会改任何设置。」
- `div[data-pb-nav-list]`：8 组（7 常用组；`showAdvanced` 为真时追加第 8 组「高级（原始 JSON）」）→ `button[data-pb-nav-btn][data-active]`，结构 = `span` 组名 + `span[data-pb-nav-count]` 计数
- 点击行为：`setActiveGroup(id)` + **清空搜索**（`setSearchDraft('')`、`setSearch('')`，3055-3059）
- 计数含义：搜索时是「该组命中的字段数」，否则是「该组字段总数」

**右：主区 `div[data-pb-cfg-main]`**

**(a) 吸顶工具条 `div[data-pb-cfg-top]` → `div[data-pb-cfg-tools]`**（3062-3110）

| 顺序 | 元素 | 说明 |
| --- | --- | --- |
| 1 | `input[data-pb-input][data-pb-search][type=search][aria-label=搜索设置]` | placeholder「搜索中文标签、原始 key 或说明…」；输入立即更新 `searchDraft`，**200ms 防抖**后写 `search`（2710-2713） |
| 2 | 按钮「清除」（条件：有搜索词） | 同时清 `searchDraft` 与 `search` |
| 3 | `div[data-pb-grow]` 撑开 | — |
| 4 | 按钮「显示分组导航」（条件：导航已收起） | `setNavCollapsed(false)` |
| 5 | `div[data-pb-toggle]`：`Switch` + 文案「显示高级项目」 | 打开时同时 `setActiveGroup('advanced')`（3091-3094） |
| 6 | 主按钮「保存改动」/「保存改动（N）」/「保存中…」 | `disabled: !dirtyCount \|\| saving`；点击 → `requestSave()` 弹确认 |
| 7 | danger 按钮「放弃全部改动」（条件：有脏字段） | 弹 `ConfirmDialog` 二次确认 |

**(b) 提示行 `div[data-pb-cfg-hint]`**（3111-3116）：三段文字
1. 「常用 73 项；高级 235 项只在开启后出现。」
2. 有脏字段：「N 项未保存」（`span[data-pb-dirty]`，warn 色）；否则「只会送出改动过的键。」
3. 「保存前会列出中文标签让你确认。」

**(c) 结果提示 `NoticeBlock`**（3117）：保存成功时文本「已保存 N 项改动。」，`notice.restart` 非空时补一行「其中需要重启 PawBot 才生效：<中文标签…>。」并出现「去状态页重启」按钮。

**(d) 内容区 `div[data-pb-stack]`（`content` 数组，2967-3008）** —— 三种互斥分支：

| 分支 | 条件 | 渲染 |
| --- | --- | --- |
| 搜索模式 | `q`（trim 后的搜索词）非空 | ①摘要行 `div[data-pb-search-summary]`「搜索「X」：N 个常用（、M 个高级）结果」；②每个有命中的**常用组**一张卡；③若 `showAdvanced` 再追加**每个有命中的原始区段**卡；④若没开高级且有高级命中 → `Notice info`「还有 M 个符合的高级设置…」+ 按钮「显示 M 个高级结果」；⑤常用 0 命中且（未开高级或高级也 0）→ 「找不到符合的设置」空态卡 |
| 高级模式 | `activeGroup === 'advanced'`（且无搜索） | ①**原始 JSON 卡**（整份 JSON 编辑器）；②36 个原始区段卡（只渲染有字段的区段） |
| 普通组模式 | 其他 | 只渲染当前 `activeGroup` 对应的那**一张**组卡（`COMMON_META.filter(group === activeGroup)`） |

**组卡 `renderGroupCard`**（2898-2906）：`section[data-pb-card]`，头 = `h2[data-pb-card-title]`（组名）+ `span[data-pb-card-sub]`「N 项（符合）· <组的 hint>」；体 = 字段网格；无字段时 `div[data-pb-empty]`「这个分组没有符合条件的设置。」

**原始区段卡 `renderSectionCard`**（2907-2911）：头 = `h2`「`SECTION_LABELS[section]`」+ sub「N 项 · 原始区段 `<section>`」；体 = 字段网格。

**原始 JSON 卡 `rawCard`**（2913-2949）：

- 头：`h2`「原始 JSON（整份设置）」+ sub「N 个区段」（`Object.keys(cfg).length`）
- 说明：保存只送有改动的键；遮罩值不会被写回；PawBot 只做深合并，删键不会移除
- 按钮「展开原始 JSON / 收起原始 JSON」+ 一行 help
- 展开后：`textarea[data-pb-code][rows=18][aria-label=原始 JSON（整份设置）][aria-invalid]`（等宽 13px）+ JSON 错误 `Notice err` + 主按钮「保存原始 JSON / 处理中…」+ help「会先算差异，再列出这次要改的中文标签」

**字段网格 `renderFields`**（2884-2896）：`div[data-pb-fields-wrap]` > `div[data-pb-fields]` > 逐个 `Field`（key 带 `rev` 前缀，用 `rev` 变化强制重挂以清掉控件内部 state）。

**单条字段行 `Field`**（2589-2651）：

```
div[data-pb-field][data-pb-field-key=<key>][data-dirty?]
  div[data-pb-field-head]
    label#pb-field-label-<enc key>[data-pb-field-label][title=原始key]   ← 中文标签
    div[data-pb-impacts]   ← 影响徽章（1~3 个）
  div[data-pb-control]     ← 控件（见 2.4.4）
  div[data-pb-help]        ← 说明 + （高级字段才追加）code[data-pb-keychip] 原始 key
```

- 有未保存改动时：`data-dirty` 让整行左侧出现 3px warn 色竖条（`client.js:749`）。

#### 2.4.4 控件渲染器（8 种 + 回退规则）

`Field` 里的分派是「**元数据 control 提示 + 当前值类型兜底**」的链式 if（2595-2641），顺序即优先级：

| # | 条件 | 组件 | 实际 DOM |
| --- | --- | --- | --- |
| 1 | `masked`（在 `masked_keys` 里）或 `item.control === 'secret'` | `SecretControl`（2537-2587） | 见下 |
| 2 | `control === 'switch'` 或当前值是 boolean | `Switch` | `button[role=switch][aria-checked]` |
| 3 | `control === 'ratio'`，或（非 number 控件且当前值是非整数 0~1） | `RatioControl`（2435-2450） | `input[type=range][min=0][max=1][step=0.01]` + `span[data-pb-ratio-val]`（百分数，1 位小数） |
| 4 | `control === 'number'` 或当前值是 number | `NumberControl`（2382-2433） | `input[type=number][data-pb-input][data-pb-number]`，max-width 220px |
| 5 | `control === 'select'` | `SelectControl`（2459-2471） | `select[data-pb-select]`；选项值不在列表里时**临时插入一项**「目前：<值>」或「（空值）」 |
| 6 | `control === 'json'` 或当前值是数组/对象 | `JsonControl`（2473-2535） | 折叠：摘要（`数组 · N 项` / `对象 · N 个键` / `目前值：X`）+ 按钮「展开编辑 JSON」（`aria-expanded`/`aria-controls`）→ `textarea[data-pb-code][aria-invalid][aria-describedby]` + 解析错误 Notice + 状态说明 |
| 7 | `control === 'multiline'` 或（字符串 & 长度>80 或含换行） | 原生 `textarea[data-pb-textarea][rows=4]` | |
| 8 | 兜底 | 原生 `input[type=text][data-pb-input]` | |

**NumberControl 的取值行为**（值得重规划时注意）：

- 边界来自 `boundsFor(item, current)`（2364-2380）：优先用元数据 `min`/`max`；`max` 未给时按 key 后缀猜：`*port`→65535、`*chars|*tokens`→1000000、`timeout|_sec|interval|cooldown|_gap|delay`→86400、`_hours`→8760、`_days`→3650、其他→100000；`step` 未给时按「当前值是否整数」选 1 或 0.01。
- 显式 `max: null` 表示「去掉 HTML 上限」（`qzone.daily_limit` 就是这种）；`min` 缺省为 0。
- 输入框有本地 `text` state + `focused` 标记：聚焦时不跟随外部值，失焦时 `commit()`；空串或非数字 → `onClear()`（回到服务器值），等于当前值也 `onClear()`。

**SecretControl 的完整语义**（2537-2587）—— 安全要求最集中的地方：

| 状态 | 徽章 | 输入框 | 其他 |
| --- | --- | --- | --- |
| 服务器有值（被遮罩） | `Badge warn`「已设置（不会显示）」 | `input[type=password][autocomplete=new-password][placeholder=输入新值才会覆盖…]` | 按钮「清除（设为空）」可用 |
| 服务器空值 | `Badge muted`「未设置」 | 同上 | 「清除」按钮 disabled |
| 用户点了清除 | `Badge err`「将清除（送空字符串）」 | 清空 | 文案「保存后会把这个值清空。」 |

- 点「清除（设为空）」→ **二次确认** `ConfirmDialog`（标题「确定清除「<中文标签>」？」，danger，确认文案「标记为清除」，正文两段：保存后送空字符串、原值无法恢复 / 不想改就按取消）。
- 「留空＝不改」是硬规则：`setField` 里 secret 字段遇到 `''` 或遮罩串直接 `clearField`（2733-2736）。

#### 2.4.5 保存链路（表单 / 原始 JSON 两条）

**表单保存 `requestSave`**（2775-2793）：

1. `dirtyCount === 0` → `Notice info`「没有改动，不需要保存。」并 return；
2. `patch = patchFromDirty()`：对每个 dirty key `setPath({}, key, value)` 组装嵌套对象，再整体过 `stripMask`（2763-2768）；结果为 falsy → `Notice warn`「这次的改动只有遮罩值，没有可送出的内容…」；
3. 打开 `ConfirmDialog`：标题「这次会改动 N 项设置」，正文 `<ul[data-pb-changelist]>` 逐项列「**中文标签** `原始key`」+ 一句「只会把下面这些键送回 PawBot；没动到的设置不会被覆盖。」+ 结尾「其中 M 项需要重启 PawBot 才生效：<标签…>」或「这些项目都会立即生效。」（3120-3137）
4. 确认 → `performSave()`：`POST /config {patch}` → 用返回的 `config` + `masked_keys` 覆盖本地、`rawText` 同步、清空 `dirty`、`rev+1`、关闭对话框、`Notice good`「已保存 N 项改动。」（带 restart 列表）；失败 → `Notice err`「保存失败：…；改动还保留着，可以再试一次。」（2825-2854）

**原始 JSON 保存 `requestRawSave`**（2795-2823）：

1. `JSON.parse(rawText)` 失败 → 就地 `rawErr`「原始 JSON 格式错误：…」；
2. 顶层不是对象 → 「最外层必须是对象（不能是数组或单一值）。」；
3. `diff = deepDiff(cfg, parsed)` → `stripMask(diff)`；空 → `Notice info`「原始 JSON 没有可送出的改动（遮罩值不会被写回）。」；
4. 打开同一个 `ConfirmDialog`（`kind:'raw'`），`keys` 由 `flattenLeaves` 展开、`count` 由 `countLeaves` 统计；
5. 确认后走同一个 `performSave()`（同一个端点）。

**放弃改动 `discardAll`**（2856-2868）：清 `dirty`、`rawText` 复位成 `cfg`、`rev+1`（强制重挂，清掉 SecretControl 本地输入）、`Notice info`「已放弃 N 项未保存的表单改动。/ 已还原原始 JSON 编辑框。」

**脏值判定 `setField`**（2731-2743）：新值与原值 `deepEqual` → 删除 dirty（不记录）；否则写入 `dirty[key]`。

**其余 2 个 ConfirmDialog**：保存确认（`confirm`）与放弃确认（`discardConfirm`「确定放弃全部改动？」，正文「表单里 N 项未保存的改动会消失，无法恢复。」+「原始 JSON 编辑框也会回到上次读到的设置；服务器上的 config 不会被改动。」）。

### 2.5 PersonaTab（人设页）

组件 `PersonaTab()`，`client.js:3154-3301`。没有 `active` prop，**挂载即 `GET /persona` 读一次，无轮询**（3180-3185）。

**① 顶部**：`h1`「PawBot 人设」+ sub「人设会直接影响她说话的方式，保存后下一轮就生效。」

**② 读取失败**：`Notice err`「读不到人设：…」+「再试一次」

**③ 卡「人设内容」**（`section[data-pb-card]`，3225-3267）

| 元素 | 说明 |
| --- | --- |
| 卡头 | `h2`「人设内容」+ sub `文件：<path>` 或「全宽编辑器」 |
| 说明 | 「这是她回话时的世界观与说话规则…保存是整份覆盖，不是合并。」 |
| 工具行 `div[data-pb-persona-tools]` | ①主按钮「保存人设 / 保存中…」`disabled: !loaded \|\| busy \|\| !changed` → 弹确认 → `POST /persona {content}`；②按钮「还原成上次读到的版本」`disabled` 同上 → 弹确认后仅把编辑框回到 `original`；③`span[data-pb-count]`「共 N 字（不含空白 M 字；Unicode 字符，不是字节）」；④`Badge warn`「尚未保存」/ `Badge good`「与读到的版本相同」 |
| 两行 help | 保存 = 覆盖人设文件、下一轮生效；还原 = 只回退编辑框，不动服务器文件，也不是恢复出厂 |
| `NoticeBlock`（notice） | 保存成功「已保存 N 字；她下一轮回话就会用新的说法。」/ 失败「保存失败：…；编辑框内容还在，可以再试一次。」 |
| 编辑器 | `textarea[data-pb-code][data-pb-persona-editor][rows=22][aria-label=人设内容][disabled=!loaded\|\|busy]`，`min-height:420px`，等宽 13px，`line-height:1.7`；未加载时 placeholder「读取人设中…」 |

**④ ConfirmDialog ×2**

1. 保存：标题「确定要保存人设？」，danger，「确定保存」；正文「会直接覆盖人设文件，不是合并，也不会自动备份旧版本。」+ 列表（写入 N 字 / 下一轮生效 / 后悔只能手动改回）。
2. 还原：标题「确定要丢弃这次修改？」+ 列表（未保存修改消失不可恢复 / 服务器文件不会被改动、也不会变出厂值）。

---

## 3. 字段元数据模型

全部写在 `client.js` 顶部（第 38-478 行），没有任何外部 JSON/接口；`test/client-meta-check.mjs` 把它与 `app/pawbot/config.py` 的 `DEFAULT` 做离线比对。

### 3.1 行格式

```js
// 每一行：[key, 中文标签, 说明, 影响徽章[], 控件型别?, 额外设定?]
['chat.reply_mode', '回话时机', '什么情况她才回：…',
 ['speech', 'instant', 'napcat'],
 'select', { options: [['at_or_wake', '被 @ 或被叫才回'], …] }]
```

| 位置 | 名称 | 必需 | 说明 |
| --- | --- | --- | --- |
| 0 | `key` | ✅ | 点分路径，如 `llm.api_key`；必须是 config 里真实存在的叶子 |
| 1 | `label` | ✅ | 中文标签（测试要求含 CJK、全局唯一——当前 308 个标签全唯一） |
| 2 | `help` | ✅ | 人话说明（测试要求去空白后 ≥8 字；compat 字段要求 help 里出现「旧版/旧字段/兼容」） |
| 3 | `impact` | ✅ | 影响码数组；空数组会被 `buildMeta` 兜成 `['instant']` |
| 4 | `control` | 否 | `text`/`secret`/`number`/`switch`/`select`/`ratio`/`json`/`multiline`；省略时靠值类型推断（见 2.4.5） |
| 5 | `extra` | 否 | 展开进 meta 对象：`options`（select 用，`[[value,label],…]`）、`min`、`max`、`step`（number 用）。`max: null` = 不设 HTML 上限 |

### 3.2 IMPACTS（7 种徽章）

| code | 文案 | tone | 出现次数（按字段计） |
| --- | --- | --- | --- |
| `instant` | 立即生效 | good | 283 |
| `restart` | 需重启 PawBot | warn | 19 |
| `napcat` | 需连上 NapCat | warn | 27 |
| `speech` | 会影响她说话 | info | 132 |
| `media` | 会影响发图／语音 | info | 37 |
| `memory` | 会影响记忆 | info | 32 |
| `compat` | 旧版兼容（现在无作用） | muted | 6 |

（`IMPACTS` 定义 `client.js:38-46`；一个字段可带多个码，徽章按数组顺序渲染。）

### 3.3 GROUPS（8 个分组导航）与 groupForCommonKey

| id | 文案 | hint | 常用字段数 |
| --- | --- | --- | --- |
| `model` | 模型与密钥 | 主模型、评审模型与密钥 | 9 |
| `chat` | 聊天与回复 | 怎么回、回多长、延迟多久 | 19 |
| `proactive` | 主动与情绪 | 冷场、插话、作息与情绪 | 9 |
| `memory` | 记忆与人物 | 记忆、日记、人物与关系 | 11 |
| `media` | 图片与媒体 | 表情包、图片、视频、语音 | 10 |
| `bili` | B 站与搜索 | 工具、搜索、B 站与 X | 7 |
| `qq` | QQ 与服务 | QQ 账号、空间、白名单与服务端口 | 8 |
| `advanced` | 高级（原始 JSON） | 其余原始键与整份 JSON | 0（挂 235 个高级字段） |

分派靠 `groupForCommonKey(key)` 的**前缀 if 链**（`client.js:433-444`），顺序即优先级：

```
llm.* | judge.* | emotion.*                      → model
chat.* | reply.* | safety.*                      → chat
proactive.* | random_interject.* | life.enabled  → proactive
memory.* | memes.* | diary.* | longmem.* | people.* | relations.* → memory
media.* | persona.* | video.* | speech.* | tts.* | tools.image_judge | tools.nsfw_censor | tools.block_minor → media
tools.*（其余）                                  → bili
其余一切（默认）                                 → qq
```

由此产生的落点值得注意：`selfedit.enabled` 落在 `qq`（因为默认分支），`tools.bili_*`/`tools.search_*`/`tools.pixiv_*` 全落到 `bili`，而 `qzone.*`/`whitelist.*`/`server.port`/`webui.port`/`account.owner_ids` 在 `qq`。

### 3.4 高级字段的组织方式：SECTION_LABELS（37 个键 / 36 个在用区段）

高级字段**不参与 8 分组**，`buildMeta` 直接把 `group` 设为 `'advanced'`（`client.js:459`）；在 UI 上按 `key.split('.')[0]` 的原始区段**分组卡片呈现**，区段中文名来自 `SECTION_LABELS`（`client.js:411-420`，**37 个键**）：

> 其中 `account`（账号）这个标签**没有任何高级字段**（该区段只有常用的 `account.owner_ids`），所以高级页实际只渲染 **36 张区段卡**；反过来，没有任何高级字段的区段漏标签（测试会校验）。

```
保存 1 / 长期记忆 7 / 记忆 6 / 日记 2 / 兴趣 3 / 提醒 3 / 梗 1 / 连接服务器 3 / 网页界面 2 /
机器人身份 4 / 聊天 15 / 尺度 4 / 人物侧写 5 / 关系网 2 / 回复 8 / 网络用语研究 6 / 人设 5 /
媒体 2 / 工具 50 / 语音转写 6 / 视频 2 / 语音合成 14 / QQ 空间 5 / 自我编辑 3 / 图库 1 /
主模型 16 / 评审模型 5 / 欲望 2 / 情绪 7 / 白名单 1 / 随机插话 2 / 读空气 6 / 生活 7 /
主动 8 / 群事件 8 / 安全延迟 13            ← 合计 235
```

### 3.5 buildMeta 生成的字段对象

```js
{
  key, section,          // section = key.split('.')[0]
  leaf,                  // 点后面的部分（备用，当前 UI 未直接使用）
  label, help,
  impact,                // 兜底 ['instant']
  control,               // 或 null
  advanced,              // 布尔
  group,                 // advanced ? 'advanced' : groupForCommonKey(key)
  ...extra               // options / min / max / step
}
```

汇总导出 `PANEL_META = { common, advanced, metadata(byKey), groups, impacts, sectionLabels }`（`client.js:471-478`），Node 分支挂到 `globalThis.__PAWBOT_PANEL_META__`。`META_BY_KEY` 是普通对象（`Object.create(null)`），被 `Field`、保存确认、restart 提醒、`NoticeBlock` 反查中文标签反复使用。

### 3.6 字段统计（308 的构成）与元数据「漂移」

**总量**：常用 **73** + 高级 **235** = **308**（与 `test/client-meta-check.mjs:247-249` 的硬断言一致）。

**但有 1 个键漏了元数据**：`config.py` 的 `DEFAULT` 目前是 **37 个区段 / 309 个叶子**，元数据只有 308 个 → **`reply.tone_trim`（句末语气词削减比例 0~1，`app/pawbot/config.py:147`）没有任何 label/help，面板里看不见也改不了**。`test/client-meta-check.mjs` 当前因此是**红的**（`30 passed, 1 failed`，失败项「config 每个 key 都有 metadata（没有漏）」）。重规划前这是一个必须先决定的现状：要么补元数据，要么把该键标记为「不暴露」。

**按控件类型**：

| control | 常用 | 高级 | 合计 |
| --- | --- | --- | --- |
| number | 24 | 102 | 126 |
| switch | 27 | 32 | 59 |
| text | 5 | 38 | 43 |
| json | 3 | 24 | 27 |
| select | 8 | 12 | 20 |
| secret | 1 | 13 | **14** |
| ratio | 5 | 13 | 18 |
| multiline | 0 | 1 | 1 |
| 合计 | 73 | 235 | 308 |

**测试对元数据的硬约束**（`test/client-meta-check.mjs`）：key 不重复、key 都真实存在于 `config.py DEFAULT`、常用/高级都有中文 label 与 ≥8 字 help、高级都有 `SECTION_LABELS`、`masked_keys` ↔ `secret` 双向、config 每个叶子都有 meta（无遗漏）、影响码合法、分组合法、select 必须有 options、compat 徽章与 help 文案一致、restart 徽章的 help 不许写「立即生效」。另有 11 项静态「接线检查」（label↔control id、JSON 折叠的 aria、对话框焦点管理、tab ARIA、搜索防抖…）。

---

## 4. 数据与接口

### 4.1 三层结构

```
浏览器（DSH Web GUI，同源）
   ↓  fetch('/pawbot-ui/*', credentials: 'same-origin')
DSH Host 进程（index.js 注册路由 → lib/webui-proxy.js 的 dispatch）
   ↓  pawbotFetch() 打 http://<webuiHost>:<webuiPort>/api/*
PawBot 子进程（app/pawbot/webui.py，默认 127.0.0.1:6185）
   ↓
data/config.json、data/persona.md、data/*、NapCat 反向 WS
```

- 面板**只认 `/pawbot-ui/*` 这一个前缀**（`const API = '/pawbot-ui'`，`client.js:485`）。
- `index.js` 同时注册 `prefix /pawbot-ui`、`prefix /pawbot-ui/` 和 16 条 `exact`（`index.js:1455-1465`），并说明「本版 webServer 的 prefix 匹配与预期不同，两套都挂」（`index.js:1443-1445`）。

### 4.2 路由总表（全部同源、全部回 JSON）

| 方法 + 路径 | 请求体 | 成功返回（形状） | 面板用在哪 |
| --- | --- | --- | --- |
| `GET /pawbot-ui/status` | — | `{ok:true, running, pid, uptime_sec, spawn_count, last_exit, last_spawn_error, last_error, reason, webui_reachable, http_ok, process:{running,pid,uptime_sec,spawn_count,last_exit,last_spawn_error,last_error,project_dir,python_exe}, pawbot:<bot.status()>\|null}`（`webui-proxy.js:273-308`） | StatusTab 轮询 |
| `GET /pawbot-ui/config` | — | `{ok:true, config:<已遮罩>, masked_keys:[dotted path]}`；PawBot 异常 → 502 `{ok:false,error}`（`webui-proxy.js:311-330`） | ConfigTab 加载 |
| `POST /pawbot-ui/config` | `{patch:{…}}`（也接受裸 partial config） | `{ok:true, config, masked_keys, dropped_keys}`（`webui-proxy.js:333-348`） | ConfigTab 保存（表单 / 原始 JSON 共用） |
| `GET /pawbot-ui/persona` | — | `{ok:true, text, content, path}`（`text` 与 `content` 同时给） | PersonaTab 加载 |
| `POST /pawbot-ui/persona` | `{text}` / `{content}` / 纯字符串 | `{ok:true, path:'persona.md', bytes}`；>200KB → 413（`webui-proxy.js:373-382`） | PersonaTab 保存 |
| `POST /pawbot-ui/restart` | `{}` | `{ok:true, restarted_from_running, pid, spawn_count}`；失败 500 | StatusTab「重启 PawBot」 |
| `POST /pawbot-ui/plugins/reload`（别名 `reload-plugins`） | `{}` | `{ok:true, result}` | StatusTab「重载插件」 |
| `POST /pawbot-ui/send` | `{target, message, is_group}` | `{ok:true, sent:true, note:'已真的以机器人账号送出此消息（不是模拟）', target, is_group, result}`；PawBot 回 ok:false 也转成 502（`webui-proxy.js:392-417`） | StatusTab「发测试消息」 |
| `GET /pawbot-ui/setup/status` | — | `{ok:true, appDir, appPresent, python:{ok,version,exe,error?}, venv:{exists,path}, deps:{pillow,websockets,ok,checked}, config:{exists,valid}, phase, progress, message, log:[{stream,line}], install:<job>, boot:<b>}`（`index.js:986-1027`、`lib/setup.js`） | SetupTab 环境卡 |
| `POST /pawbot-ui/setup/run` | `{force?:bool}` | `{ok:true, started, already_running}`（`index.js:1141-1154`） | 环境卡「只重跑环境安装」 |
| `GET /pawbot-ui/napcat/status` | — | `{ok:true, mode:'docker'\|'binary'\|'none', running, webui:{url,token,source}\|null, qr:{kind:'url'\|'file', value}\|null, loggedIn, logged_in_by, onebot, hint, manual?, checked, detail, last_error}`（`index.js:1080-1094`，**这是唯一回真 token 的路由**，`revealToken:true`） | SetupTab NapCat 卡 |
| `POST /pawbot-ui/napcat/setup` | `{}` | `{ok:true, mode, started, manual:{steps:[…]}, hint, error?, onebot}`（`index.js:1100-1112`；路由版强制 `ok:true`，让面板能读到 `manual.steps`） | 「重新侦测 / 重连 NapCat」 |
| `POST /pawbot-ui/napcat/qr` | `{}` | `{ok:true, mode, qr, webui, hint}`（`index.js:1114-1139`） | 「重新抓二维码」 |
| `GET /pawbot-ui/boot/status` | — | `{ok:true, running, done, phase, progress, message, steps:[{id,state,detail}]}`（`index.js:1182`） | SetupTab 四步进度 |
| `POST /pawbot-ui/boot` | `{force?:bool}` | `{ok:true, started, already_running, phase, progress, steps}`（`index.js:1156-1180`） | 「一键启动」 |
| `GET /pawbot-ui/context`、`GET /pawbot-ui/memory` | — | `{ok:true, data:{…}}` | **面板没有用**（`index.js:1459` 注册了，`webui-proxy.js:554-555` 有实现，UI 无入口） |

**统一错误形态**：HTTP 非 2xx 或 `ok:false` 都返回 `{ok:false, error:'人话说明'}`，另有 `dropped_keys`（config）等附加字段。未知子路径 → 404 `{ok:false, error:'未知的 /pawbot-ui 子路径'}`；未注入实现的能力 → 501 `{ok:false,error:'<feature> 未启用（Host 没有注入对应实现）'}`。

**请求体大小与超时**（`webui-proxy.js:74-166`、`504-586`）：

| 路由 | 上限 |
| --- | --- |
| `POST /config` | 1 MB |
| `POST /persona` | 200 KB + 64 KB |
| `POST /send` | 256 KB |
| `POST /setup/run`、`/napcat/*`、`/boot` | 64 KB |
| 读 body 整体超时 | 15 s（408） |

**同源 / CSRF 守卫**（`webui-proxy.js:232-269`，先于所有 actions）：

1. `Sec-Fetch-Site` 存在且不是 `same-origin`/`none` → 403；
2. `Origin` 与 DSH `webServer.getOrigin()` 不一致 → 403；GET 还额外比对 `Referer` 的 origin；
3. 非 GET 必须 `content-type: application/json` 开头 → 否则 415；
4. **拿不到 Host origin 时 fail-closed**：拒绝所有非 GET，并只记一次日志。

### 4.3 config 读取 / 保存 diff 规则

**读取**：`GET /api/config` → `maskSecrets()` → `{config, masked_keys}`。遮罩判定在 Host 侧、按 **key 名正则**（`lib/config-utils.js:22-31`）：

```
api_key|access_token|refresh_token|id_token|cloud_key|secret|password|passwd|
authorization|bearer|sessid|sessdata|cookie|private_key|sign_key   （大小写不敏感）
或者 key 恰好等于 token（刻意不误伤 max_tokens）
```

- 遮罩值 `MASK = '***已设定***'`（`lib/config-utils.js:11`，与 `client.js:484` 同一字面量）。
- **空值（`''`/null/undefined）不遮、也不进 `masked_keys`** —— 面板才能诚实显示「未设置」。

**保存**：面板只送「改动过的键」组成的**嵌套 partial**：

1. `dirty` 是扁平的 `key → value` map；
2. `patchFromDirty()` 用 `setPath` 组装嵌套对象 → `stripMask`（把任何等于 `MASK` 的值整枝丢掉，`client.js:561-578`）；
3. `POST /config` → Host 侧 `pickUpdatable()` 再丢一次 `MASK` 值并回报 `dropped_keys`（`lib/config-utils.js:79-98`）；
4. 转发给 PawBot `POST /api/config`，PawBot 做**深合并**（`deepMerge` 语义镜像，`lib/config-utils.js:105-116`）：对象递归合并、**数组整包替换**、**删键不会生效**；
5. 返回合并后的**整份（已遮罩）config**，面板用它重置 `cfg` / `rawText` / `dirty`。

由此产生的三条既有行为（重规划要保留或明确改变）：

- 「只送脏键」是保守的：两个浏览器窗同时改不同键不会互相覆盖，但同一键会后来者覆盖；
- 原始 JSON 保存也走 `deepDiff` → 同样只送差异，所以**在 JSON 里删掉一个键不会真的删掉**（UI 里有文字说明）；
- 保存成功后 `dirty` 清零，`rev+1` 强制重挂所有字段控件。

### 4.4 前端错误处理

```js
class ApiError extends Error { constructor(message, status) { …; this.status = status } }   // 628-634
async function api(method, path, body, signal) {                                           // 636-663
  - fetch 抛错（非 AbortError）→ ApiError('连不上 DSH Host 的 /pawbot-ui 通道：' + msg, status: 0)
  - status 非 2xx → ApiError(body.error || body.message || 'HTTP n', status)
  - 200 但 body.ok === false → ApiError(body.error || '主机回报失败', status)
  - 返回整个 JSON body
}
function friendlyError(err) {                                                              // 622-626
  status === 0 时补一句「（请确认 DSH Host 还活着）」
}
function isAbortError(err) { name === 'AbortError' || code === 'ABORT_ERR' }              // 618-620
```

各 tab 的落地方式：`isAbortError` 的取消不写错误态；其余一律 `friendlyError` → 内联 `Notice err`；写操作失败时**不清空用户输入**（设置页保留 dirty、人设页保留编辑框、发送表单保留内容），并在文案里明确「可以再试一次」。

### 4.5 轮询 / 刷新时机一览

| 页面 | 数据 | 触发 | 频率 | 是否后台跑 |
| --- | --- | --- | --- | --- |
| 启动 | `/setup/status` | 挂载+可见 | 3 s | 否（`active` 才跑） |
| 启动 | `/napcat/status` | 挂载+可见 | 3 s | 否 |
| 启动 | `/boot/status` | 挂载+可见 | 1.5 s，`done:true` 后停 | 否 |
| 设置 | `/config` | 挂载一次 | 无轮询 | — |
| 人设 | `/persona` | 挂载一次 | 无轮询 | — |
| 状态 | `/status` | 挂载+可见 | 5 s + 手动「立即刷新」 | 否 |

所有轮询都用 `AbortController`，切 tab / 卸载时 `clearInterval` + `abort()`；POST 用另一个共享 controller（SetupTab 是 `actCtrl`，离开即批量取消）。

### 4.6 持久化现状

| 数据 | 存哪儿 | 谁写 | 说明 |
| --- | --- | --- | --- |
| PawBot 全部业务设置（308 个字段） | `app/data/config.json` | `POST /pawbot-ui/config` | 深合并写回；Host 侧只做遮罩/去遮罩，不落盘 |
| 人设正文 | `app/data/persona.md`（`lib/webui-proxy.js:50` 的 `persona.md` 是相对 `data/` 的 API 路径） | `POST /pawbot-ui/persona` | 整份覆盖，≤200KB |
| Host 插件自身配置（26 键：`appDir`/`pythonExe`/`autostart`/`restartOnCrash`/`onebotToken`/`webuiToken`/`napcatImage`/`brain*` …） | DSH profile 的插件配置 | **不在面板里** | 其中 `onebotToken`、`webuiToken` 在 schema 里是 `.role('secret')`（`index.js:105/117`） |
| 面板 UI 状态（当前 tab / 搜索词 / 分组 / 折叠 / 草稿） | **内存** | — | 无 `localStorage`/`sessionStorage`；刷新页面即丢；config 页挂载后不卸载，所以「切 tab 不丢草稿」 |

---

## 5. 样式体系

### 5.1 Token：全站 31 个 `--dsw-*` 变量（0 处硬编码色值）

| 类别 | token |
| --- | --- |
| 文字 | `--dsw-alias-label-primary` / `-secondary` / `-tertiary` / `--dsw-alias-label-primary-inverted` |
| 背景 | `--dsw-alias-bg-base` / `bg-layer-1` / `bg-layer-2` / `bg-overlay` / `bg-mask-1` / `--dsw-alias-settings-card-fill` |
| 边框 | `--dsw-alias-border-l1` / `-l2` |
| 品牌与交互 | `--dsw-alias-brand-primary`、`--dsw-alias-button-primary-fill`、`-hover`、`--dsw-alias-interactive-bg-hover`、`-active`、`-hover-danger`、`--dsw-alias-switch-thumb`、`--dsw-alias-state-idle-primary` |
| 状态 | `--dsw-alias-state-success-primary` / `-warn-primary` / `-error-primary` |
| 圆角 | `--dsw-radius-xs` / `-sm` / `-md` / `-lg` |
| 阴影/焦点 | `--dsw-elevation-soft` / `-prominent`、`--dsw-focus-ring-width` / `-color` |

唯一的「计算色」是就绪卡的 `color-mix(in srgb, var(--success) 14%, var(--bg-layer-1))`（`client.js:924`）。

### 5.2 排版与尺寸（验收规格写在文件头注释里，`client.js:8-9`）

| 用途 | 值 |
| --- | --- |
| 正文 | 14px / line-height 1.65 |
| 说明文字 `data-pb-help` | 12.5px / 1.6，secondary 色 |
| 卡片标题 / 页面标题 | 16px / 600 / 1.4 |
| KPI 数字 | 28px / 600 / 1.2 |
| 等宽（日志/JSON/token/keychip） | `ui-monospace, Consolas, monospace`，12–13px |
| 卡片 | `padding:14px 16px`、`radius:lg`、`border:1px`、间距 12px（`--dsw-alias-settings-card-fill` 填充） |
| 字段行 | `padding:10px 0` + 底部 1px 分隔线；脏行左侧 3px warn 竖条 + `padding-left:8px` |
| 按钮 | `padding:7px 12px`、`radius:sm`；hero 主按钮 `9px 18px`/600 |
| 输入框 | `padding:7px 10px`、`radius:sm`；number 最宽 220px；textarea 最小高 76px；JSON 编辑区 `rows:18`/13px；人设编辑器最小高 420px |
| 徽章 | `12.5px`、`padding:2px 8px`、`radius:999px` |
| 开关 | 42×24px，滑块 16px，`transition:left .15s` |
| 模态 | `width:min(600px,100%)`、`max-height:85vh`、`padding:18px 20px`、遮罩 `bg-mask-1` |

### 5.3 视觉层级（从外到内）

1. 页面标题（16px 600）→ 页面副标题（12.5px secondary）
2. tab 栏：`bg-layer-2` 槽 + 活动项 `bg-layer-1` + `elevation-soft` + 600 字重
3. 卡片（`settings-card-fill` + `border-l1` + `radius-lg`）→ 卡头（16px 标题 + 12.5px 副标）
4. 字段行（label 14px 600 + 影响徽章 + 控件 + help 12.5px）
5. 状态点 / 旗标 / badge 用 success/warn/error 三色区分，tone 同时驱动文字色与边框色

### 5.4 选择器体系

所有样式都挂在 `[data-pb-*]` 属性上（全站共 **101** 个不同钩子：CSS 里出现 99 个、JS 里引用 100 个、交集 98 个），共 3 类：

- **样式 + 语义钩子**（CSS 与 JS 都用）：98 个，如 `data-pb-card`、`data-pb-field`、`data-pb-btn`、`data-pb-badge`、`data-pb-modal`…
- **纯 JS 钩子（无 CSS）**：`data-pb-field-key`（定位单字段）、`data-pb-fields-wrap`（字段网格外壳）
- **纯 CSS 钩子（无 JS）**：`data-pb-form-label`（目前无人使用，属遗留）

状态用布尔属性表达：`data-active`、`data-open`、`data-on`、`data-dirty`、`data-tone`、`data-state`、`data-primary`、`data-danger`、`data-hero`、`data-link`、`data-stream`、`data-nav-collapsed`、`data-pb-hidden`。

### 5.5 可访问性（当前已做到的）

| 项 | 实现 |
| --- | --- |
| 焦点可见 | `[data-pb-root] :is(button,input,select,textarea,[tabindex]):focus-visible { box-shadow: 0 0 0 var(--focus-ring-width) var(--focus-ring-color) }`（687-689） |
| 键盘 tab | roving tabIndex + 方向键/Home/End + 焦点跟随（3320-3339） |
| tab ↔ panel | `role=tablist/tab/tabpanel`、`aria-selected`、`aria-controls`、`aria-labelledby`、`hidden`（3340-3370） |
| 表单标签 | 每条字段 `label[for]` + 控件 `id` + `aria-labelledby`（Number/Select/Ratio/Secret/Json 都接线，测试会查） |
| 开关 | `role=switch` + `aria-checked` + `aria-labelledby` |
| JSON 折叠 | `aria-expanded` + `aria-controls` + `aria-invalid` + `aria-describedby`（错误提示 `role` 由 Notice 给 alert/status） |
| 秘密字段 | 遮罩徽章 + `input[type=password]` + `autocomplete=new-password` + `aria-label="<label> 新值"` |
| 对话框 | `role=dialog` + `aria-modal` + `aria-labelledby` + 焦点陷阱 + Escape + 焦点归还（1019-1068） |
| 日志区 | `role=log` + `aria-live=polite`（1352-1353） |
| 步骤点 | `role=img` + `aria-label="进行中/完成/失败…"`（1231-1248） |
| 动态提示 | `Notice` 的 `role="alert"`（err）/`"status"`（其它） |
| 减少动效 | `@media (prefers-reduced-motion: reduce)` 关掉旋转圈（888-890） |
| 外链 | `rel="noopener noreferrer"` + `target="_blank"`（1472-1473、1497-1498） |

**尚未做到的**（事实，不是评价）：对话框未使用 portal、未锁 body 滚动；`data-pb-kpi-num` 等纯视觉数字无 `aria-live`；无 skip-link；无「表单错误汇总」；未提供 `aria-describedby` 把 help 文案挂到每个控件（只有 JSON 编辑器挂了错误文本）。

---

## 6. 「API / 密钥」类字段现状（后续「API 统一页」的事实基础）

### 6.1 元数据里 `control: 'secret'` 的 14 个字段（全部走 SecretControl）

| # | key | 中文标签 | 所在 tab / 分组 | 控件 | 影响徽章 | 有值才遮罩 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `llm.api_key` | 主模型 API 密钥 | **设置 · 常用** / 分组 `model`（模型与密钥） | secret | 会影响她说话、立即生效 | 是 |
| 2 | `llm.vision_api_key` | 主模型：看图 API 密钥 | 高级 / 区段 `llm`（主模型） | secret | 会影响发图／语音、立即生效 | 是 |
| 3 | `judge.api_key` | 评审模型：API 密钥 | 高级 / `judge`（评审模型） | secret | 立即生效 | 是 |
| 4 | `emotion.judge_api_key` | 情绪：判官 API 密钥 | 高级 / `emotion`（情绪） | secret | 立即生效 | 是 |
| 5 | `speech.api_key` | 语音转写：API 密钥 | 高级 / `speech`（语音转写） | secret | 立即生效 | 是 |
| 6 | `tts.cloud_key` | 语音合成：云端密钥 | 高级 / `tts`（语音合成） | secret | 立即生效 | 是 |
| 7 | `slang.api_key` | 网络用语：API 密钥 | 高级 / `slang`（网络用语研究） | secret | 立即生效 | 是 |
| 8 | `tools.search_api_key` | 搜索：API 密钥 | 高级 / `tools`（工具） | secret | 立即生效 | 是 |
| 9 | `tools.image_api_key` | 图片：图库 API 密钥 | 高级 / `tools`（工具） | secret | 立即生效 | 是 |
| 10 | `tools.bili_jct` | B 站：CSRF 令牌 | 高级 / `tools`（工具） | secret | 立即生效 | 是 |
| 11 | `tools.pixiv_refresh_token` | Pixiv：Refresh 令牌 | 高级 / `tools`（工具） | secret | 立即生效 | 是 |
| 12 | `tools.pixiv_sessid` | Pixiv：SESSID 登录码 | 高级 / `tools`（工具） | secret | 立即生效 | 是 |
| 13 | `server.access_token` | 连接服务器：连接令牌 | 高级 / `server`（连接服务器） | secret | 需重启 PawBot、需连上 NapCat | 是 |
| 14 | `webui.token` | 网页界面：登录令牌 | 高级 / `webui`（网页界面） | secret | 需重启 PawBot | 是 |

**分布结论（事实）**：14 个里有 **1 个在常用**（`llm.api_key`，靠前缀规则被分到 `model` 组），**13 个在「高级（原始 JSON）」**，横跨 **9 个原始区段**（`llm` 1、`judge` 1、`emotion` 1、`speech` 1、`tts` 1、`slang` 1、`tools` 5、`server` 1、`webui` 1 — 合计 13；加上常用的 `llm.api_key` 后共 14）。在导航的 8 个分组里，只有 `model` 组能看到密钥（且只有 1 个），其余 13 个要点开「显示高级项目」→ 高级分组 → 再滚到对应区段卡。

### 6.2 凭证相关但**不是** secret 控件的字段（同样是「API 统一页」要收的）

| key | 中文标签 | 位置 | 控件 | 说明 |
| --- | --- | --- | --- | --- |
| `tools.bili_watch_credential_file` | B 站：凭证文件路径 | 高级 / `tools` | text | 指向主人提供的 B 站凭证**文件**；路径明文显示 |
| `account.owner_ids` | 主人 QQ 名单 | **常用** / 分组 `qq` | json | 授权名单（不是密钥，但属权限凭证） |
| `qzone.owner_ids` | QQ 空间：授权主人 | 高级 / `qzone` | json | 同上 |
| `whitelist.user_ids` / `whitelist.group_ids` / `whitelist.blacklist_group_ids` | 白名单/黑名单 | 高级 `whitelist` / 常用 `qq` | json | 访问控制，不是密钥 |

### 6.3 与密钥成对出现、但散落在 8 个区段的「端点 / 模型」字段

**端点类（11 个，全部在高级）**

`slang.base_url`、`tools.search_base_url`、`tools.image_custom_endpoint`、`speech.base_url`、`tts.url`、`tts.health_url`、`tts.cloud_url`、`llm.base_url`、`llm.vision_base_url`、`judge.base_url`、`emotion.judge_base_url`

**模型类（11 个）**

常用：`llm.model`、`judge.model`、`emotion.judge_model`；高级：`llm.vision_model`、`llm.fallback_model`、`llm.vision_fallback_model`、`tools.search_model`、`slang.model`、`slang.fallback_model`、`persona.anime_check_model`、`speech.model`

**按「服务」聚一下现状**（同一件事的字段散在哪个区段）：

| 服务 | 密钥字段 | 端点字段 | 模型字段 | 分处区段数 |
| --- | --- | --- | --- | --- |
| 主模型（聊天/视觉） | `llm.api_key`(常用)、`llm.vision_api_key` | `llm.base_url`、`llm.vision_base_url` | `llm.model`(常用)、`llm.vision_model`(常用)、`llm.fallback_model`、`llm.vision_fallback_model`、`llm.provider`、`llm.*timeout/breaker/temperature` | `llm` 一个区段内 16 项（但常用页只露 5 项） |
| 评审/情绪/画质 | `judge.api_key`、`emotion.judge_api_key` | `judge.base_url`、`emotion.judge_base_url` | `judge.model`(常用)、`emotion.judge_model`(常用) | `judge` + `emotion` 两个区段 |
| 搜索 | `tools.search_api_key` | `tools.search_base_url` | `tools.search_model`(常用) | `tools` |
| 图片/图库 | `tools.image_api_key` | `tools.image_custom_endpoint` | — | `tools` |
| B 站 | `tools.bili_jct`、`tools.pixiv_refresh_token`、`tools.pixiv_sessid`、`tools.bili_watch_credential_file` | — | — | `tools`（50 项的大区段里） |
| 语音转写 | `speech.api_key` | `speech.base_url` | `speech.model` | `speech` |
| 语音合成 | `tts.cloud_key` | `tts.url`、`tts.cloud_url`、`tts.health_url` | — | `tts`（14 项） |
| 网络用语研究 | `slang.api_key` | `slang.base_url` | `slang.model`、`slang.fallback_model` | `slang` |
| 连接/网页 | `server.access_token`、`webui.token` | — | — | `server` + `webui`（**两项都标「需重启」**） |
| DSH Host 侧（不在面板） | `onebotToken`、`webuiToken` | — | — | `index.js` 的插件 Config |

### 6.4 密钥的读写链路要点（做统一页必须保留的语义）

1. **读取**：Host 按 key 名正则遮罩 → 面板拿 `masked_keys` 判断「有没有值」；空值不遮 → 显示「未设置」。出厂默认 14 个 secret **全是空值**，所以首次打开时 `masked_keys` 为 0，14 个字段全部显示「未设置」（离线校验的实际输出可证）。
2. **写值**：只有用户真的输入新值才进 `dirty`；`SecretControl` 输入框不预填任何东西。
3. **不改**：留空 = 不改（`setField` 对 secret 遇 `''`/MASK 直接删 dirty）。
4. **清空**：必须显式点「清除（设为空）」→ 二次确认 → dirty 记为 `''` → 保存时送空字符串。
5. **绝不回写遮罩**：客户端 `stripMask` + Host `pickUpdatable` 双重丢弃（`dropped_keys` 会回报）。
6. **真值只在两处出现**：`GET /pawbot-ui/napcat/status` 的 `webui.token`（面板上「查看 token」按钮才展开，且提示「链接本身就是凭证」）；其余任何地方（工具返回值等）都遮成 `***`（`index.js:1029-1043`）。
7. **重启语义**：`server.access_token` / `webui.token` 的徽章是「需重启 PawBot」，保存确认框会单独列出这些键，保存成功后的 Notice 还会出现「去状态页重启」按钮。

---

## 7. 已知痛点 / 可改进点（只描述现状，不给最终方案）

### 7.1 信息规模与层级

1. **308 个设置项**里只有 73 个「常用」，其余 **235 个（76%）**挂在「显示高级项目」开关后面；这 235 项又按 **36 个原始区段**平铺成卡，单是 `tools` 一个区段就有 50 行。
2. **两套分类互不对齐**：常用字段走 7 个 `GROUPS`（销售视角的分组），高级字段走 36 个 `SECTION_LABELS`（config 文件视角的区段），`advanced` 组只有 1 个；同一件事（例如「主模型」）在常用页是「模型与密钥」组里 5 行，在高级页是 `llm` 区段 16 行，**用户要在两种心智模型间切换**。
3. **分组靠 key 前缀 if 链**（`groupForCommonKey`）：`selfedit.enabled` 掉进 `qq` 组；`tools.image_judge`/`tools.nsfw_censor`/`tools.block_minor` 被硬编码进 `media`；`tools.*` 其余全进 `bili`（包括 Pixiv、图库、语音合成之外的杂项）。新增字段时容易落错组，且没有测试保护分组语义（测试只校验「分组 id 合法」）。

### 7.2 密钥与 API 配置

4. **密钥分散**：14 个 secret 里 13 个在高级区，横跨 9 个区段；用户想「把 API 都配一遍」要开高级、点 9 个区段卡、逐个滚。常用页只露 1 个 `llm.api_key`。
5. **同一服务的三件套（key / base_url / model）不在同一屏**：例如评审模型，`judge.model` 在常用，`judge.api_key`/`judge.base_url` 在高级 `judge` 区段，`emotion.judge_*` 又在另一个区段。
6. **另有 2 个 secret 完全不在面板里**（`onebotToken`、`webuiToken`，在 DSH profile 的 Host Config，`index.js:105/117`），用户会以为「面板已经涵盖所有密钥」。
7. **路径型凭证**（`tools.bili_watch_credential_file`）是明文 text 输入，没有「选择文件」控件，也没有「是否已存在」的校验提示。
8. **`llm.provider = 'dsh'` 时不需要 api_key**（`index.js`/`core.py:530-533` 明确注释），但面板没有任何「当前用哪种鉴权方式 / 这个密钥是否还有效」的反馈；`llm.api_key` 的 help 只说「填错她会完全回不了话」。
9. **没有任何「测试连接」入口**：面板里没有 `POST /pawbot-ui/llm/test` 这类动作（PawBot 的 `webui.py` 有 `/api/llm/test`，但 `/pawbot-ui` 没代理、`app/web/index.html` 的「收发测试」页有），所以填完密钥只能去群聊里看效果。

### 7.3 搜索、导航与移动端

10. **搜索与分组是两套视图**：搜索时左导航的高亮被禁用（`data-active` 条件要求 `!q`），结果按「组卡 + 原始区段卡」重排；点导航会**清空搜索词**；高级结果默认被折叠，需要点 Notice 上的「显示 N 个高级结果」。搜索匹配范围是 label + key + help + select 选项文案（不含高级区段的其它同义说法）。
11. **移动端只有「容器 760px」一条规则**：窄屏时导航变横向 wrap 芯片（8 个），字段变单列；没有针对触摸的更大点击区、没有吸底保存条，顶部工具条在窄屏会换行成 3–4 行（搜索 / 清除 / 开关 / 保存 / 放弃全挤在 `cfg-top`）。
12. **导航折叠是手动开关**（`navCollapsed`），刷新页面即恢复展开；折叠后「显示分组导航」按钮出现在吸顶工具条里，位置会随搜索词出现/消失而跳动。

### 7.4 保存与确认流程

13. **保存是全局的**：`dirty` 跨所有分组累积，「保存改动（N）」不区分「哪些组改了」；放弃也只能全放（没有单字段/单组撤销）。左导航不显示「哪一组有未保存改动」。
14. **两条保存路径**（表单 / 原始 JSON）写同一个端点，但一个是 diff-from-dirty、一个是 diff-from-JSON，两套 dirty 状态独立（改完 JSON 不保存再点表单保存，两者互不感知；`discardAll` 会同时复位两者）。
15. **二次确认分布不均**：状态页 3 个破坏性动作都有确认；设置页「保存」「放弃」有确认；**启动页所有动作 0 确认**（包括「一键启动」会装依赖、起进程）；「只重跑环境安装」也没有确认。
16. **没有离开守卫**：只有切 tab 会提示未保存；刷新/关页/切到别的 DSH section 都会静默丢失（无 `beforeunload`、无 `localStorage` 草稿）。
17. **确认框本身**：`ConfirmDialog` 不用 portal、不锁 body 滚动；标题是纯文本，正文里混排 `p`/`ul`，长键名列表（308 项全改时最多 235 行 `<li>`）会把卡片撑到 `max-height:85vh` 滚动。

### 7.5 状态页与数据契约

18. **连接信息显示不全**：`describeConnection()` 期待 `online`/`connected_at`，而 `bot.status()` 实际回 `role`/`ready`/`online_sec`/`last_alive`（`app/pawbot/core.py:513-540`），所以「在线/离线」「连上于 …」两段在当前后端下**永远不会出现**，只剩「QQ <id>・<nickname>」。
19. **「今日看片」KPI 当前不会出现**：`todayWatchCount()` 依次找 `pawbot.bili_today / today_watch / watch_today / stats.bili_today / stats.today_watch`，而 `bot.status()` 里没有这些键，`app/` 全仓也搜不到这几个键名（只有前端在读）。
20. **状态页拿不到的数据只能靠「原始 JSON」卡**：整包 `st` 折叠在页面最底部，排查时要手动展开读 JSON。
21. **重复请求**：状态页每 5s 一次 `/status`，后端每次都要打 `GET /api/status`；`/napcat/status` 每次都要跑一次 NapCat 探测 + 收集日志（3s 一次），属于「轮询里的重活」。

### 7.6 启动页

22. **日志窗只在一瞬间可见**：`installing`（`phase==='installing'`）才渲染日志卡，装完就消失；且只显示最后 10 行（后端 `setup/status` 的 `log` 只回最后 60 行）。用户想回看安装过程没有入口。
23. **进度信息双源**：`setup/status` 同时携带 `install` 与 `boot` 两个 job，`phase/progress/message` 会被 boot 覆盖（`index.js:1014-1025`），面板只用 `phase==='installing'` 一个判断，两者并发时语义会打架（后端已有「boot 在跑时不让 setup 单跑」的保护，`index.js:1144-1151`）。
24. **二维码不渲染成图**：只给「打开链接」或「文件路径」，明确写了「面板不读取图片文件」；手机扫码体验依赖用户自己找文件。
25. **`POST /setup/run` 的 `force` 在 UI 上永远是 `false`**（`client.js:1688`），后端支持强制重装但面板没有入口。
26. **`manual` 步骤只在「重新侦测 / 重连」之后才有**（`client.js:1707-1710`），首屏如果 `mode==='none'` 且用户没点过按钮，只会看到一句「主机就会把这台机器要手动做的步骤列在这里」。

### 7.7 人设页

27. **两个大文本编辑器形态不同**：人设是独立 tab 的 420px 编辑器；`llm.system_prompt`（系统提示词，`control:'multiline'`）藏在高级 `llm` 区段里，只是一个 4 行小 textarea —— 两者都「改世界观」，但被放在两套完全不同的界面里，且互相没有提示。
28. 人设**整份覆盖、无 diff、无备份、无版本**，字符数只统计 Unicode 字符（`charCount`），不显示字节数（真实上限是 200KB）。

### 7.8 其它工程性现状

29. **两套设置 UI 并存**：DSH 面板（4 tab）与 PawBot 自己的 6185 网页（`app/web/index.html`，**9 个 tab**：概览 / 配置 / 人设·记忆 / 群友 / 记忆 / 上下文 / 插件 / 日志 / 收发测试）。面板只覆盖了其中一部分能力；`/pawbot-ui/context`、`/pawbot-ui/memory` 已注册但面板**没有任何入口**。
30. **`StatusTab` 的 `onGoConfig` 是死 prop**（传了没用）；`data-pb-form-label` 是死 CSS 钩子；`reload-plugins` 与 `plugins/reload` 两个别名并存（面板只用后者）。
31. **字段控件的「推断规则」隐式**：`Field` 会按当前值类型覆盖元数据 control（boolean→switch、0~1 非整数→ratio、超长字符串→textarea），所以「同一个字段在值变化后控件类型可能变」（例如某字段原本是 null 显示 text，赋值为数字后变 number）。
32. **数字边界靠 key 后缀猜**（`boundsFor` 的正则），新增字段忘了写 `min/max` 时上限会落到 100000 之类的默认值。
33. **测试保护的是「元数据与接线」**，不保护视觉/文案一致性，也没有浏览器/读屏测试（`test/client-meta-check.mjs:13-14` 自己写明「不替代真实浏览器与读屏测试」）。
34. **元数据漂移已经在发生**：`config.py DEFAULT` 有 309 个叶子，元数据只有 308 个（缺 `reply.tone_trim`），离线校验当前是红的（30 passed / 1 failed）；同时 `SECTION_LABELS` 有 1 个标签（`account`）没有任何高级字段。也就是说「308」不是配置的真实规模，而是元数据的规模。

---

## 附录 A：关键行号索引（client.js）

| 主题 | 行号 |
| --- | --- |
| 文件头设计目标 / 路由契约 | 1-31 |
| `IMPACTS` | 38-46 |
| `COMMON`（73 行） | 48-135 |
| `ADVANCED`（235 行） | 137-409 |
| `SECTION_LABELS`（37 个键，36 个在用） | 411-420 |
| `GROUPS`（8 组） | 422-431 |
| `groupForCommonKey` | 433-444 |
| `buildMeta` / `COMMON_META` / `ADVANCED_META` / `META_BY_KEY` / `PANEL_META` | 446-478 |
| `MASK` / `API` | 484-485 |
| `getPath` / `setPath` / `deepEqual` / `deepDiff` / `stripMask` | 493-578 |
| `fmtDuration` / `fmtTime` / `fmtCount` / `charCount` | 580-616 |
| `friendlyError` / `ApiError` / `api()` | 622-663 |
| `CSS`（全部样式） | 669-939 |
| Node 分支暴露 metadata | 944-946 |
| `window.__ModuleLoader__.load` 开始 | 948 |
| `Badge` / `Impacts` / `Switch` / `Notice` / `InfoRow` / `Kpi` | 957-1007 |
| `ConfirmDialog` | 1010-1098 |
| `NoticeBlock` | 1100-1116 |
| 启动页常量（`BOOT_STEP_*`、`NAPCAT_MODE_TEXT`） | 1122-1152 |
| `normalizeBootSteps` / `mergeNapcat` / 路径工具 | 1155-1229 |
| `SetupStepDot` / `SetupStepRow` | 1231-1275 |
| `SetupEnvCard` | 1277-1364 |
| `SetupNapCatCard` | 1366-1531 |
| `SetupTab` | 1533-1862 |
| 状态页格式化函数（`describeConnection` 等） | 1866-1940 |
| `StatusTab` | 1942-2360 |
| `boundsFor` / `NumberControl` / `RatioControl` / `SelectControl` | 2364-2471 |
| `JsonControl` | 2473-2535 |
| `SecretControl` | 2537-2587 |
| `Field` | 2589-2651 |
| `ConfigTab` | 2655-3150 |
| `PersonaTab` | 3154-3301 |
| `TABS` / `Panel` / section 注册 | 3305-3419 |

## 附录 B：字段统计（可复算）

```
常用 73 = model 9 + chat 19 + proactive 9 + memory 11 + media 10 + bili 7 + qq 8
高级 235 = 36 个原始区段（tools 50 / llm 16 / chat 15 / tts 14 / safety 13 / reply 8 /
           events 8 / proactive 8 / longmem 7 / emotion 7 / life 7 / memory 6 / slang 6 /
           speech 6 / topic 6 / people 5 / persona 5 / qzone 5 / judge 5 / bot 4 / spice 4 /
           hobbies 3 / reminders 3 / selfedit 3 / server 3 / diary 2 / media 2 / relations 2 /
           video 2 / webui 2 / libido 2 / random_interject 2 / store 1 / memes 1 / album 1 /
           whitelist 1）
合计 308；控件分布：number 126 / switch 59 / text 43 / json 27 / select 20 / ratio 18 /
secret 14 / multiline 1

config.py DEFAULT 实际规模：37 个区段 / 309 个叶子 → 元数据少 1 个（reply.tone_trim）
```

## 附录 C：本文结论的核对方法（只读，不写任何文件）

```powershell
# 字段计数 + secret 清单 + 分组/区段分布（client.js 的 Node 分支只暴露 metadata）
node -e "import('file:///D:/WorkBuddy/pawbot-dsh/client.js').then(()=>{const m=globalThis.__PAWBOT_PANEL_META__;const all=m.common.concat(m.advanced);console.log('COMMON',m.common.length,'ADV',m.advanced.length,'TOTAL',all.length);console.log('SECRET',all.filter(i=>i.control==='secret').length)})"

# 官方离线校验（会顺带核对 config.py 的 DEFAULT、masked_keys 与 308 的断言）
node test/client-meta-check.mjs
```

**当前实际运行结果**（2026-10-27 只读执行，供对照）：

```
config 區段：37，葉節點：309
metadata：常用 73 + 進階 235 = 308
masked_keys：0 個            ← DEFAULT 里 14 个 secret 全是空值，所以一个都不遮
== 結果 == 30 passed, 1 failed
失敗：config 每個 key 都有 metadata（沒有漏）→ reply.tone_trim
```

> 注：`test/client-meta-check.mjs` 需要本机有 `py`（Windows）或 `python3`；它只读 `app/pawbot/config.py` 的 `DEFAULT`，不读写运行时配置。
