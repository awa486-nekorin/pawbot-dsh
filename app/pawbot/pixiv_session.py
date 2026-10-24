"""
用 PawBot 專用的 Chrome 會話去搜 Pixiv。

为什么不用 refresh_token：
  · `www.pixiv.net/refresh_token` 这个入口 Pixiv 已经下掉了（实测 404）
  · `__refresh_token` cookie 在你这台机器上从没出现过
  · 登录态是浏览器 cookie，浏览器关闭时 CDP 连接也会断开

默认使用项目 data/browser/chrome-pixiv 下的独立 Chrome 配置，
不读取、复制或关闭日常 Edge / Chrome 的配置和进程。

Chrome 带 `--remote-debugging-port=9222` 起来之后，CDP 能直接
  · 打开任意 Pixiv 接口（带上登录 cookie）
  · 读页面内容

于是：搜图 → 让浏览器去请求 ajax 搜索接口 → 从页面里读 JSON → 拿图片直链。

R-18 也一样：接口在登录态下会返回 x_restrict=1 的作品。

前提：PawBot 专用 Chrome 要带着调试端口运行，且在里面登录过 Pixiv。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import subprocess
import time
import urllib.parse
import urllib.request

from .log import log
from .imagesearch import pixiv_search_url


def _popular_tag() -> str:
    """要掛的高人気標籤門檻（tools.pixiv_popular，預設 1000）。

    設成 off / 0 / 空就完全不掛，走普通搜。
    """
    try:
        from . import imagesearch
        return str(imagesearch._RUNTIME_CFG.get("pixiv_popular", ",".join(DEFAULT_TIERS)) or "")
    except Exception:
        return ",".join(DEFAULT_TIERS)


# 非会员账号的 order=popular_d 会被 Pixiv 忽略（实测与 date_d 结果相同），
# 所以「热门」只能靠 `<N>users入り` 收藏标签分层：先找万藏，不够再降到五千、一千。
DEFAULT_TIERS = ("10000", "5000", "1000")


def _tiers(popular) -> list[str]:
    """把 tools.pixiv_popular 解析成从高到低的收藏层；最后总留一层不加门槛的兜底。"""
    pv = str(popular if popular is not None else "").strip()
    if pv.lower() in ("", "0", "off", "none", "false"):
        return [""]
    tiers = {str(int(p)) for p in re.split(r"[,，\s]+", pv) if p.isdigit() and int(p) > 0}
    if not tiers:
        tiers = set(DEFAULT_TIERS)
    return sorted(tiers, key=int, reverse=True) + [""]


def _quality(item: dict) -> float:
    """收藏数取对数，再按收藏率（收藏/浏览）加分：同样火，被认真收藏的更可能是好图。"""
    bookmarks = max(0, int(item.get("bookmark_count") or 0))
    views = max(0, int(item.get("view_count") or 0))
    ratio = bookmarks / views if views else 0.0
    return math.log10(bookmarks + 1) + 3 * min(ratio, 0.25)


CHROME_EXE_CANDIDATES = tuple(
    os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")
    for root in (
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get(
            "LOCALAPPDATA",
            os.path.join(os.path.expanduser("~"), "AppData", "Local")),
    )
)
CHROME_EXE = next((p for p in CHROME_EXE_CANDIDATES if os.path.isfile(p)), "")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
CHROME_PROFILE = os.path.join(PROJECT_ROOT, "data", "browser", "chrome-pixiv")
CDP_PORT = 9222
PIXIV_REFERER = "https://www.pixiv.net/"


def _under_project(path: str) -> bool:
    """专用 Chrome 的 profile 必须落在项目目录里，别去碰日常浏览器的 profile。"""
    target = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(PROJECT_ROOT))
    return target == root or target.startswith(root + os.sep)

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _spawn_detached(args: list[str]):
    """把浏览器从当前进程组里甩出去，父进程退出后它还能活着。"""
    if os.name == "nt":
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, close_fds=True, creationflags=flags)
    else:
        subprocess.Popen(args, close_fds=True, start_new_session=True)


def _local_get(url: str, timeout: float = 5.0):
    """本机 CDP HTTP 请求。127.0.0.1 必须绕开系统代理，否则被接管回 502。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


class _CDP:
    """极简 CDP 客户端：wsproto 是裸 WebSocket，没有 OneBot 那套 call()。"""

    def __init__(self, conn):
        self.conn = conn
        self._id = 0

    async def call(self, method, params=None, timeout=25.0):
        self._id += 1
        mid = self._id
        await self.conn.send_text(json.dumps(
            {"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            raw = await asyncio.wait_for(
                self.conn.recv(), timeout=max(0.5, end - time.time()))
            if raw is None:
                break
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == mid:
                if msg.get("error"):
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
        raise RuntimeError(f"{method} 没回包")


class PixivSession:
    """连专用 Chrome 的 CDP，用它的登录态搜 Pixiv。"""

    port_wait = 15.0          # 等调试端口起来的上限（秒）
    kill_poll = 0.4           # 轮询间隔

    def __init__(self, host: str = "127.0.0.1", port: int = CDP_PORT):
        self.host = host
        self.port = port

    # ------------------------------------------------------------ 环境
    def cdp_browser(self) -> str:
        """Return the CDP browser product, or an empty string if unavailable."""
        try:
            ver = _local_get(f"http://{self.host}:{self.port}/json/version",
                             timeout=3)
            return str(ver.get("Browser") or "")
        except Exception:
            return ""

    def cdp_alive(self) -> bool:
        return bool(self.cdp_browser())

    def chrome_profile_running(self) -> bool:
        """有没有 Chrome 正带着**我们这个专用 profile** 在跑。

        用 PowerShell 查进程命令行，按 OrdinalIgnoreCase 比路径和端口，
        避免把用户日常 Chrome / Edge 认成自己的。
        """
        profile = os.path.normcase(os.path.normpath(CHROME_PROFILE))
        port_flag = f"--remote-debugging-port={self.port}"
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$hits=Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" |"
            " Where-Object { $_.CommandLine -and"
            f" ($_.CommandLine.IndexOf('{profile}',"
            "[StringComparison]::OrdinalIgnoreCase) -ge 0) -and"
            f" ($_.CommandLine.IndexOf('{port_flag}',"
            "[StringComparison]::OrdinalIgnoreCase) -ge 0) };"
            "if ($hits) { 'yes' } else { 'no' }"
        )
        try:
            p = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=20)
        except Exception as e:
            log.warn(f"查询 Chrome 专用 profile 失败：{type(e).__name__} {e}")
            return False
        return "yes" in str(getattr(p, "stdout", "") or "").lower()

    def chrome_cdp_ready(self) -> bool:
        """调试端口上是 Chrome，而且确实是我们的专用 profile。"""
        browser = self.cdp_browser()
        return browser.startswith("Chrome/") and self.chrome_profile_running()

    def launch_chrome(self, open_url: str = "https://www.pixiv.net/") -> bool:
        """Start an isolated Chrome profile without touching everyday browsers."""
        if not CHROME_EXE or not os.path.isfile(CHROME_EXE):
            log.warn("找不到 Google Chrome，无法启动 Pixiv 专用浏览器")
            return False
        if not _under_project(CHROME_PROFILE):
            log.warn(f"Chrome 专用配置必须放在项目的 data/ 目录下：{CHROME_PROFILE}")
            return False

        browser = self.cdp_browser()
        if browser:
            if browser.startswith("Chrome/") and self.chrome_profile_running():
                return True
            log.warn(f"调试端口 {self.port} 已被其他浏览器占用：{browser}")
            return False

        try:
            os.makedirs(CHROME_PROFILE, exist_ok=True)
        except OSError as e:
            log.warn(f"创建 Chrome 专用配置目录失败：{e}")
            return False

        args = [CHROME_EXE, f"--user-data-dir={CHROME_PROFILE}",
                f"--remote-debugging-port={self.port}",
                "--remote-debugging-address=127.0.0.1",
                "--no-first-run", "--no-default-browser-check"]
        if open_url:
            args.append(open_url)
        try:
            _spawn_detached(args)
        except Exception as e:
            log.warn(f"启动 Pixiv 专用 Chrome 失败：{type(e).__name__} {e}")
            return False

        end = time.time() + self.port_wait
        while time.time() < end:
            browser = self.cdp_browser()
            if browser:
                if browser.startswith("Chrome/") and self.chrome_profile_running():
                    log.info("Pixiv 专用 Chrome 已启动并通过 CDP 验证")
                    return True
                log.warn(f"调试端口 {self.port} 已被其他浏览器会话占用：{browser}")
                return False
            time.sleep(self.kill_poll)
        log.warn("Chrome 已启动但无法确认专用 CDP 会话，请检查 9222 端口和窗口")
        return False

    def ensure_chrome(self) -> bool:
        """搜图前确保专用 Chrome 开着；没开就自动启动（登录态还在专用 profile 里）。"""
        if self.chrome_cdp_ready():
            return True
        log.info("专用 Chrome 没在运行，自动启动")
        return self.launch_chrome()

    # ------------------------------------------------------------ 解析
    def _parse(self, text: str, n: int) -> list[dict]:
        """把搜索结果页里的 JSON 解析成图片条目。

        `/ajax/search/top/<kw>` 返回的是
        `{"body": {"illust": {"data": [...]}, "manga": {...}}}`，页面上就是这段 JSON。
        """
        text = (text or "").strip()
        if not text:
            return []
        # 页面可能是 JSON，也可能被包了一层
        data = None
        for cand in (text,
                     text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
            if not cand:
                continue
            try:
                data = json.loads(cand)
                break
            except Exception:
                continue
        if data is None:
            return []

        body = data.get("body") if isinstance(data, dict) else None
        items: list = []
        if isinstance(body, dict):
            for key in ("illust", "manga", "illustManga"):
                blk = body.get(key) or {}
                if isinstance(blk, dict):
                    items += (blk.get("data") or [])
        if not items and isinstance(data, list):
            items = data

        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            xr = int(it.get("xRestrict") or it.get("x_restrict") or 0)
            urls = it.get("image_urls") or {}
            u = it.get("url") or ""
            if isinstance(urls, dict) and urls:
                u = (urls.get("original") or urls.get("large")
                     or urls.get("medium") or u)
            if not u:
                continue
            # 250px 缩略图 → 540px 大图
            u = re.sub(r"/c/\d+x\d+_[a-z0-9_]+/", "/c/540x540_70/", u)
            user = it.get("user") if isinstance(it.get("user"), dict) else {}
            tags = it.get("tags")
            tag_list = []
            if isinstance(tags, list):
                for t in tags:
                    if isinstance(t, dict):
                        tag_list.append(str(t.get("name") or ""))
                    else:
                        tag_list.append(str(t))
            out.append({
                "url": u,
                "title": str(it.get("title") or "")[:100],
                "source": "pixiv",
                "page": f"https://www.pixiv.net/artworks/{it.get('id')}",
                "author": user.get("name") or it.get("userName") or "",
                "tags": ", ".join([t for t in tag_list if t][:6]),
                "rating": "r18" if xr else "sfw",
                "need_referer": True,
                "referer": PIXIV_REFERER,
            })
            if len(out) >= n:
                break
        return out

    # ------------------------------------------------------------ 会话操作
    async def _page(self):
        """Select only an existing HTTPS www.pixiv.net page, never another tab."""
        try:
            targets = _local_get(f"http://{self.host}:{self.port}/json/list")
        except Exception:
            return None, None
        found = []
        for page in targets if isinstance(targets, list) else []:
            if not isinstance(page, dict):
                continue
            try:
                url = urllib.parse.urlsplit(page.get("url", ""))
                ws = urllib.parse.urlsplit(page.get("webSocketDebuggerUrl", ""))
                if (page.get("type") == "page" and url.scheme == "https"
                        and url.hostname == "www.pixiv.net"
                        and url.port in (None, 443) and not url.username
                        and ws.scheme == "ws" and ws.hostname == self.host
                        and ws.port == self.port and ws.path.startswith("/devtools/page/")):
                    found.append((url.path.startswith("/artworks/"), page, ws.path))
            except (TypeError, ValueError):
                continue
        # 留浏览记录开的作品页可能还在加载，搜索优先用其他 Pixiv 页
        found.sort(key=lambda x: x[0])
        return (found[0][1], found[0][2]) if found else (None, None)

    async def _evaluate(self, expression):
        page, path = await self._page()
        if not path:
            raise RuntimeError("no_pixiv_page")
        conn = await _connect(self.host, self.port, path)
        try:
            result = await _CDP(conn).call("Runtime.evaluate", {
                "expression": "(() => { if (location.origin !== 'https://www.pixiv.net') "
                              "throw new Error('wrong_origin'); return (" + expression + "); })()",
                "awaitPromise": True, "returnByValue": True}, timeout=40)
            if result.get("exceptionDetails"):
                raise RuntimeError(str(result["exceptionDetails"]))
            return (result.get("result") or {}).get("value")
        finally:
            await conn.close()

    async def status(self) -> dict:
        """Read login metadata from the current DOM without requests or cookies."""
        state = {"ready": False, "page_on_pixiv": False,
                 "logged_in": False, "error": "", "history_mode": "background_fetch"}
        try:
            if not self.chrome_cdp_ready():
                raise RuntimeError("chrome_cdp_not_ready")
            state["ready"] = True
            page, path = await self._page()
            if not path:
                raise RuntimeError("no_pixiv_page")
            state["page_on_pixiv"] = True
            # 旧版页面用 meta#meta-global-data；2026 起首页改成 Next.js，
            # 登录信息在 script#__NEXT_DATA__ 的 pageProps 里。两种都认。
            value = await self._evaluate("""(() => {
                const okId = u => !!(u && /^[1-9][0-9]*$/.test(String(u.id || u.userId || '')));
                const node = document.querySelector('meta#meta-global-data');
                if (node) {
                    const data = JSON.parse(node.getAttribute('content') || '{}');
                    return {logged_in: data.isLoggedIn !== false && okId(data.userData), error:''};
                }
                const nd = document.querySelector('script#__NEXT_DATA__');
                if (!nd) return {logged_in:false, error:'login_metadata_missing'};
                const props = ((JSON.parse(nd.textContent || '{}').props) || {}).pageProps || {};
                let self = null;
                try {
                    const st = JSON.parse(props.serverSerializedPreloadedState || '{}');
                    self = st.userData && st.userData.self;
                } catch (e) {}
                return {logged_in: props.isLoggedIn === true && okId(self), error:''};
            })()""")
            if not isinstance(value, dict):
                raise RuntimeError("invalid_login_metadata")
            state["logged_in"] = value.get("logged_in") is True
            state["error"] = str(value.get("error") or "")
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
        return state

    async def _fetch_json(self, url):
        try:
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname != "www.pixiv.net"
                    or parsed.port not in (None, 443) or parsed.username
                    or parsed.password or parsed.fragment):
                raise ValueError("pixiv_origin_rejected")
            value = await self._evaluate(
                "(async () => { const r = await fetch(" + json.dumps(url) +
                ", {credentials:'include', headers:{Accept:'application/json'}});"
                "if (!r.ok) throw new Error('http_' + r.status); return await r.json(); })()")
        except Exception as exc:
            text = str(exc)
            match = re.search(r"http_(\d{3})", text)
            if match:
                raise RuntimeError("http_" + match.group(1)) from None
            raise RuntimeError("pixiv_request_failed: " + type(exc).__name__) from None
        if not isinstance(value, dict):
            raise RuntimeError("invalid_json_response")
        if value.get("error"):
            raise RuntimeError("pixiv_api_error")
        if not isinstance(value.get("body"), dict):
            raise RuntimeError("invalid_response_body")
        return value["body"]

    async def _search_once(self, query: str, allow_r18: bool, popular: str,
                           page: int = 1) -> dict:
        """Fetch one full-query artwork-search page from the Pixiv origin."""
        # 收藏层用完整的 `<N>users入り` 标签；只写数字会部分匹配到无关标签。
        # Pixiv 按路径里的关键词搜索，门槛必须同时出现在路径和 word 里。
        tier = str(popular or "").strip()
        word = f"{query} {tier}users入り" if tier.isdigit() and int(tier) > 0 else str(query)
        params = urllib.parse.urlencode({
            "word": word,
            "order": "popular_d", "mode": "all" if allow_r18 else "safe",
            "p": max(1, min(5, int(page))), "s_mode": "s_tag", "type": "all"})
        return await self._fetch_json("https://www.pixiv.net/ajax/search/artworks/"
                                      + urllib.parse.quote(word) + "?" + params)

    async def _detail_item(self, iid: str, candidate: dict, terms: set) -> dict:
        """读一张作品的详情，整理成统一的结果项（`_metadata` 供过滤用，调用方取走）。"""
        detail = await self._fetch_json(f"https://www.pixiv.net/ajax/illust/{iid}")
        tags = detail.get("tags") or {}
        tags = tags.get("tags", []) if isinstance(tags, dict) else tags
        names, translations = [], []
        for tag in tags:
            if isinstance(tag, dict):
                names.append(str(tag.get("tag") or tag.get("name") or ""))
                translations.extend(str(v) for v in (tag.get("translation") or {}).values())
            else:
                names.append(str(tag))
        title = str(detail.get("title") or "")
        metadata = " ".join([title, *names, *translations]).casefold()
        restricted = int(detail.get("xRestrict") or detail.get("x_restrict") or 0)
        bookmarks = int(detail.get("bookmarkCount") or candidate.get("bookmarkCount") or 0)
        urls = detail.get("urls") or {}
        original = urls.get("original") or ""
        url_candidates = []
        for url in (original, urls.get("regular"), urls.get("large"), candidate.get("url")):
            if isinstance(url, str) and url.startswith("https://i.pximg.net/") \
                    and url not in url_candidates:
                url_candidates.append(url)
        return {"illust_id": iid, "url": original or (url_candidates[0] if url_candidates else ""),
                "url_candidates": url_candidates, "title": title,
                "source": "pixiv", "page": f"https://www.pixiv.net/artworks/{iid}",
                "author": str(detail.get("userName") or candidate.get("userName") or ""),
                "tags": ", ".join(names + translations),
                "rating": "r18" if restricted else "sfw", "xRestrict": restricted,
                "aiType": detail.get("aiType", candidate.get("aiType")),
                "width": int(detail.get("width") or candidate.get("width") or 0),
                "height": int(detail.get("height") or candidate.get("height") or 0),
                "bookmark_count": bookmarks, "bookmarkCount": bookmarks,
                "view_count": int(detail.get("viewCount") or 0),
                "relevance": sum(term in metadata for term in terms) / max(1, len(terms)),
                "need_referer": True, "referer": PIXIV_REFERER,
                "_metadata": metadata}

    async def search_report(self, query: str, n: int = 6, allow_r18: bool = False,
                            *, popular=None, min_bookmarks: int = 100,
                            min_side: int = 800, candidate_limit: int = 24,
                            min_relevance: float = 0.5, exclude_ai: bool = True,
                            variety: bool = False) -> dict:
        """收藏分层搜索 → 先用搜索结果里的字段粗筛 → 少量详情精筛 → 按质量排序。"""
        from .imagesearch import mentions_furry, _allowed
        report = {"provider": "pixiv(浏览器)", "query": query, "images": [],
                  "error": "", "errors": [], "stages": [], "filtered": {}, "candidates": 0,
                  "authenticated": False, "history_mode": "background_fetch"}

        def drop(reason):
            report["filtered"][reason] = report["filtered"].get(reason, 0) + 1

        try:
            n = max(0, min(24, int(n)))
            limit = max(0, min(30, int(candidate_limit)))
            if not str(query).strip() or not n or not limit:
                raise ValueError("empty_query_or_limit")
            min_bookmarks, min_side = max(0, int(min_bookmarks)), max(0, int(min_side))
            min_relevance = max(0.0, min(1.0, float(min_relevance)))
            self.ensure_chrome()
            state = await self.status()
            report["status"] = state
            report["authenticated"] = state.get("logged_in") is True
            if state.get("error") or not all(state.get(k) for k in
                                              ("ready", "page_on_pixiv", "logged_in")):
                raise RuntimeError(state.get("error") or "not_logged_in")
            tiers = _tiers(_popular_tag() if popular is None else popular)
            seen, images = set(), []
            # 凑一个比 n 大的合格池再挑，不是撞到前 n 张就停
            pool = max(n, min(limit, n * 3))
            terms = set(str(query).casefold().split())
            for tier in tiers:
                if len(images) >= pool or len(seen) >= limit:
                    break
                report["stages"].append({"query": query, "popular": tier})
                for page_number in (1, 2):
                    if len(seen) >= limit or len(images) >= pool:
                        break
                    # 标签是 AND 关系：标签一多就一条都不剩。零候选时按
                    # 全量 → 前三个 → 前两个的顺序收窄重试。
                    words = str(query).split()
                    attempts = [str(query)]
                    if len(words) > 3:
                        attempts.append(" ".join(words[:3]))
                    if len(words) > 1:
                        attempts.append(" ".join(words[:2]))
                    candidates, total = [], 0
                    try:
                        for attempt in dict.fromkeys(attempts):
                            page = page_number
                            body = await self._search_once(attempt, allow_r18, tier, page)
                            block = body.get("illustManga") or {}
                            total = int(block.get("total") or 0) if isinstance(block, dict) else 0
                            # 想要变化时，大结果集随机挑一页（每页 60 条），别每次都是同一批
                            if variety and page_number == 1 and total > 120:
                                page = random.randint(1, min(5, total // 60))
                                if page != 1:
                                    body = await self._search_once(attempt, allow_r18, tier, page)
                            for key in ("illustManga", "illust", "manga"):
                                blk = body.get(key) or {}
                                if isinstance(blk, dict):
                                    candidates.extend(blk.get("data") or [])
                            if candidates:
                                break
                    except Exception as exc:
                        report["errors"].append(f"search page {page_number}: {type(exc).__name__}: {exc}")
                        break
                    if variety:
                        random.shuffle(candidates)
                    for candidate in candidates:
                        if len(seen) >= limit or len(images) >= pool:
                            break
                        if not isinstance(candidate, dict):
                            continue
                        iid = str(candidate.get("id") or "")
                        if not iid.isdecimal() or iid in seen:
                            continue
                        # 粗筛：搜索结果自带的字段就够判断，不浪费详情请求
                        if exclude_ai and int(candidate.get("aiType") or 0) == 2:
                            seen.add(iid); drop("ai"); continue
                        if int(candidate.get("illustType") or 0) != 0:
                            seen.add(iid); drop("not_illust"); continue
                        cw, ch = int(candidate.get("width") or 0), int(candidate.get("height") or 0)
                        if cw and ch and min(cw, ch) < min_side:
                            seen.add(iid); drop("resolution"); continue
                        seen.add(iid)
                        report["candidates"] = len(seen)
                        try:
                            item = await self._detail_item(iid, candidate, terms)
                        except Exception as exc:
                            report["errors"].append(f"detail {iid}: {type(exc).__name__}: {exc}")
                            continue
                        metadata = item.pop("_metadata")
                        reason = ("furry" if mentions_furry(metadata) else
                                  "ai" if exclude_ai and int(item.get("aiType") or 0) == 2 else
                                  "r18" if not allow_r18 and (item["xRestrict"] or re.search(r"r-?18", metadata)) else
                                  "rating" if not _allowed(item, allow_r18) else
                                  "relevance" if item["relevance"] < min_relevance else
                                  "bookmarks" if item["bookmark_count"] < min_bookmarks else
                                  "resolution" if min(item["width"], item["height"]) < min_side else
                                  "missing_original" if not item["url_candidates"] else "")
                        if reason:
                            drop(reason)
                            continue
                        item["thumb"] = candidate.get("url") or ""
                        images.append(item)
            report["candidates"] = len(seen)
            images.sort(key=lambda item: (item["relevance"], _quality(item),
                                         item["width"] * item["height"]), reverse=True)
            if variety and len(images) > n:
                # 在质量前列里随机挑，避免同一个需求永远是同几张
                top = images[:max(n, min(len(images), n * 2))]
                images = random.sample(top, n)
                images.sort(key=_quality, reverse=True)
            report["images"] = images[:n]
            if not images:
                report["error"] = "; ".join(report["errors"]) or "no_matching_images: " + json.dumps(report["filtered"])
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            report["errors"].append(report["error"])
        return report

    async def search(self, query: str, n: int = 6,
                     allow_r18: bool = False, **options) -> list[dict]:
        return (await self.search_report(query, n, allow_r18, **options))["images"]

    # ------------------------------------------------------------ 主页 / 排行榜
    async def _fetch_raw(self, url):
        """同源 JSON，但不要求 body 包装（ranking.php 直接返回 contents）。"""
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "www.pixiv.net"
                or parsed.port not in (None, 443) or parsed.username or parsed.fragment):
            raise RuntimeError("pixiv_origin_rejected")
        try:
            value = await self._evaluate(
                "(async () => { const r = await fetch(" + json.dumps(url) +
                ", {credentials:'include', headers:{Accept:'application/json'}});"
                "if (!r.ok) throw new Error('http_' + r.status); return await r.json(); })()")
        except Exception as exc:
            match = re.search(r"http_(\d{3})", str(exc))
            raise RuntimeError("http_" + match.group(1) if match
                               else "pixiv_request_failed: " + type(exc).__name__) from None
        if not isinstance(value, dict) or value.get("error"):
            raise RuntimeError("invalid_json_response")
        return value

    async def _home_candidates(self, source: str, allow_r18: bool) -> tuple[str, list[dict]]:
        """从排行榜或主页推荐取候选，统一成搜索结果的字段形状。"""
        if source == "recommend":
            data = await self._fetch_raw("https://www.pixiv.net/ajax/top/illust?mode=all&lang=zh")
            body = data.get("body") or {}
            thumbs = {str(t.get("id")): t for t in ((body.get("thumbnails") or {}).get("illust") or [])
                      if isinstance(t, dict)}
            ids = (((body.get("page") or {}).get("recommend") or {}).get("ids") or [])
            return "主页推荐", [thumbs[str(i)] for i in ids if str(i) in thumbs]
        mode = source if source in ("daily", "weekly", "monthly") else "daily"
        if allow_r18:
            mode += "_r18"
        data = await self._fetch_raw("https://www.pixiv.net/ranking.php?" + urllib.parse.urlencode(
            {"mode": mode, "content": "illust", "format": "json", "p": 1}))
        out = []
        for c in data.get("contents") or []:
            if not isinstance(c, dict):
                continue
            flags = c.get("illust_content_type") or {}
            out.append({"id": str(c.get("illust_id") or ""), "illustType": int(c.get("illust_type") or 0),
                        "width": int(c.get("width") or 0), "height": int(c.get("height") or 0),
                        "url": c.get("url") or "", "userName": c.get("user_name") or "",
                        "_furry": bool(flags.get("furry")), "_lo": bool(flags.get("lo")),
                        "_rank": int(c.get("rank") or 0)})
        return {"daily": "今日排行", "weekly": "本周排行", "monthly": "本月排行"}[mode.split("_")[0]], out

    async def home_report(self, n: int = 3, allow_r18: bool = False, *, source: str = "auto",
                          top: int = 20, min_bookmarks: int = 500, min_side: int = 1000,
                          exclude_ai: bool = True, variety: bool = True) -> dict:
        """不带标签时的默认来源：排行榜 / 主页推荐取前 top 个，再按搜索同一套规则过滤。"""
        from .imagesearch import mentions_furry, _allowed
        report = {"provider": "pixiv(浏览器)", "query": "", "images": [], "error": "",
                  "errors": [], "stages": [], "filtered": {}, "candidates": 0,
                  "authenticated": False, "history_mode": "background_fetch"}

        def drop(reason):
            report["filtered"][reason] = report["filtered"].get(reason, 0) + 1

        try:
            n = max(1, min(8, int(n)))
            self.ensure_chrome()
            state = await self.status()
            report["status"] = state
            report["authenticated"] = state.get("logged_in") is True
            if state.get("error") or not all(state.get(k) for k in ("ready", "page_on_pixiv", "logged_in")):
                raise RuntimeError(state.get("error") or "not_logged_in")
            if source == "auto":
                source = random.choice(("daily", "weekly", "monthly", "recommend"))
            label, candidates = await self._home_candidates(source, allow_r18)
            report["query"] = label
            report["stages"].append({"query": label, "popular": source})
            kept = []
            for c in candidates[:max(1, min(50, int(top)))]:
                iid = str(c.get("id") or "")
                if not iid.isdecimal():
                    continue
                if c.get("_furry"):
                    drop("furry"); continue
                if allow_r18 and c.get("_lo"):
                    drop("rating"); continue
                if exclude_ai and int(c.get("aiType") or 0) == 2:
                    drop("ai"); continue
                if int(c.get("illustType") or 0) != 0:
                    drop("not_illust"); continue
                cw, ch = int(c.get("width") or 0), int(c.get("height") or 0)
                if cw and ch and min(cw, ch) < min_side:
                    drop("resolution"); continue
                kept.append(c)
            report["candidates"] = len(kept)
            # 详情并发取，但别一次把调试端口灌满
            gate = asyncio.Semaphore(4)

            async def one(c):
                async with gate:
                    try:
                        return c, await self._detail_item(str(c["id"]), c, set())
                    except Exception as exc:
                        report["errors"].append(f"detail {c.get('id')}: {type(exc).__name__}: {exc}")
                        return c, None

            images = []
            for c, item in await asyncio.gather(*(one(c) for c in kept)):
                if item is None:
                    continue
                metadata = item.pop("_metadata")
                reason = ("furry" if mentions_furry(metadata) else
                          "ai" if exclude_ai and int(item.get("aiType") or 0) == 2 else
                          "r18" if not allow_r18 and (item["xRestrict"] or re.search(r"r-?18", metadata)) else
                          "rating" if not _allowed(item, allow_r18) else
                          "bookmarks" if item["bookmark_count"] < min_bookmarks else
                          "resolution" if min(item["width"], item["height"]) < min_side else
                          "missing_original" if not item["url_candidates"] else "")
                if reason:
                    drop(reason)
                    continue
                item["thumb"] = c.get("url") or ""
                images.append(item)
            images.sort(key=_quality, reverse=True)
            if variety and len(images) > n:
                images = random.sample(images[:max(n, min(len(images), n * 2))], n)
                images.sort(key=_quality, reverse=True)
            report["images"] = images[:n]
            if not images:
                report["error"] = "; ".join(report["errors"]) or "no_matching_images: " + json.dumps(report["filtered"])
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            report["errors"].append(report["error"])
        return report

    async def logged_in(self) -> bool:
        return bool((await self.status()).get("logged_in"))

    async def record_views(self, pages, dwell: float = 8.0) -> int:
        """在专用 Chrome 后台分页里真正打开作品页，让 Pixiv 账号留下浏览记录。

        背景 fetch 搜索不算「看过」；只有作品页被浏览器加载才会计入。
        只接受 https://www.pixiv.net/artworks/<数字>，开完即关。返回成功打开的数量。
        """
        urls = [u for u in dict.fromkeys(pages or [])
                if isinstance(u, str) and re.fullmatch(r"https://www\.pixiv\.net/artworks/\d+", u)]
        if not urls or not self.chrome_cdp_ready():
            return 0
        try:
            ws = urllib.parse.urlsplit(str(_local_get(
                f"http://{self.host}:{self.port}/json/version").get("webSocketDebuggerUrl") or ""))
        except Exception:
            return 0
        if not (ws.scheme == "ws" and ws.hostname == self.host and ws.port == self.port
                and ws.path.startswith("/devtools/browser/")):
            return 0
        conn, opened = None, []
        try:
            conn = await _connect(self.host, self.port, ws.path)
            cdp = _CDP(conn)
            for url in urls:
                try:
                    res = await cdp.call("Target.createTarget",
                                         {"url": url, "background": True}, timeout=10)
                    if res.get("targetId"):
                        opened.append(res["targetId"])
                except Exception as e:
                    log.warn(f"打开作品页失败：{type(e).__name__}")
            if opened:
                await asyncio.sleep(max(1.0, float(dwell)))
            for tid in opened:
                try:
                    await cdp.call("Target.closeTarget", {"targetId": tid}, timeout=10)
                except Exception:
                    pass
        except Exception as e:
            log.warn(f"记录 Pixiv 浏览失败：{type(e).__name__}")
        finally:
            if conn:
                try:
                    await conn.close()
                except Exception:
                    pass
        if opened:
            log.info(f"已在专用 Chrome 打开 {len(opened)} 个作品页（计入浏览记录）")
        return len(opened)

    async def open_login(self, url: str = "https://www.pixiv.net/") -> bool:
        """把瀏覽器導到登入頁。"""
        if not self.chrome_cdp_ready():
            return False
        conn = None
        try:
            _page, path = await self._page()
            if not path:
                return False
            conn = await _connect(self.host, self.port, path)
            cdp = _CDP(conn)
            await cdp.call("Page.enable")
            await cdp.call("Page.navigate", {"url": url})
            return True
        except Exception as e:
            log.warn(f"開啟登入頁失敗：{type(e).__name__} {e}")
            return False
        finally:
            if conn:
                try:
                    await conn.close()
                except Exception:
                    pass

    async def wait_login(self, timeout: float = 300.0,
                         poll: float = 3.0) -> bool:
        """等主人在這個瀏覽器視窗裡登入完。"""
        end = time.time() + timeout
        while time.time() < end:
            if await self.logged_in():
                return True
            await asyncio.sleep(poll)
        return False


async def _connect(host: str, port: int, path: str):
    from . import wsproto
    return await wsproto.connect(host, port, path)
