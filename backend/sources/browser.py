"""
用真实 Chrome 访问 HLTV，解决 Cloudflare 拦截。

- Playwright 驱动本机安装的 Chrome（channel="chrome"），有头模式，持久化用户目录
  （data/browser-profile），cf_clearance 等 cookie 会一直保存，过一次验证后长期有效。
- 不要固定视口大小、不要改 locale：实测这两项会让页面尺寸/语言和真实窗口对不上，
  Cloudflare 的验证就一直过不去（人工点了也会反复弹出）；去掉后 20 秒左右自动通过。
- 所有浏览器操作在一个专用线程里串行执行（Playwright 同步 API 不能跨线程），
  异步代码通过 fetch() 等待结果。
- 两次访问之间至少间隔 MIN_INTERVAL 秒并加随机抖动，避免高频触发验证。
- 遇到验证页先等它自动通过；超过 AUTO_WAIT 秒仍未通过，调用 on_challenge 回调
  （发飞书通知）请人在弹出的 Chrome 窗口里手动完成验证，最多再等 MANUAL_WAIT 秒。
- 空闲超过 IDLE_CLOSE 秒自动关闭浏览器，下次访问时重新打开（有监视中的比赛时不关闭）。
- 监视比赛时为每场比赛单独开一个标签页并一直保持打开（watch()）：比赛页的实时比分板
  通过 websocket 自己更新，读取 DOM 不产生请求；只在需要时（换图、比赛结束等）才刷新。
"""
import asyncio
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MIN_INTERVAL = 3.0
JITTER = 2.0
AUTO_WAIT = 45  # 实测自动通过约 20 秒
MANUAL_WAIT = 300
IDLE_CLOSE = 600
NAV_TIMEOUT_MS = 45000
SCOREBOARD_WAIT_MS = 15000


class HLTVBlockedError(RuntimeError):
    """Cloudflare 验证在等待时间内没有通过。"""


def is_hltv_page(html: str) -> bool:
    """真正的 HLTV 页面：<html> 上有 data-client-country-iso，验证页没有。"""
    return "data-client-country-iso" in html[:3000]


def is_challenge(title: str, html: str) -> bool:
    # 验证页的文字随浏览器语言变化（中文系统下标题是"请稍候…"），所以主要看语言无关的脚本标记
    lowered = html[:20000].lower()
    return (
        "just a moment" in title.lower()
        or "请稍候" in title
        or "_cf_chl_opt" in lowered  # 只在验证页里有；/cdn-cgi/challenge-platform/ 正常页面也会加载
        or "challenge-error-title" in lowered
        or "cf-challenge" in lowered
    )


class BrowserFetcher:
    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
        self.proxy: Optional[str] = None
        self.headless = False
        self.on_challenge: Optional[Callable[[str], None]] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hltv-browser")
        self._playwright = None
        self._context = None
        self._page = None
        self._last_nav = 0.0
        self._last_used = 0.0
        self._idle_timer: Optional[threading.Timer] = None
        # 监视中的比赛：key → (标签页, 上次加载时间)
        self._watched: Dict[str, Tuple[object, float]] = {}

    def configure(self, proxy: Optional[str], headless: bool = False) -> None:
        """修改代理或有头/无头模式后，下次访问时用新配置重开浏览器。"""
        if (proxy, headless) != (self.proxy, self.headless):
            self.proxy, self.headless = proxy, headless
            self._executor.submit(self._close)

    async def fetch(self, url: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._fetch_sync, url)

    def fetch_sync(self, url: str) -> str:
        return self._executor.submit(self._fetch_sync, url).result()

    async def watch(self, key: str, url: str, reload: bool = False) -> Tuple[str, float]:
        """读取监视标签页的当前内容；标签页不存在或 reload=True 时（重新）加载。返回 (html, 上次加载时间)。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._watch_sync, key, url, reload)

    async def unwatch(self, key: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._unwatch_sync, key)

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._close)

    # ── 以下方法只在浏览器线程中执行 ─────────────────────────────────────────

    def _ensure_page(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        self._close()
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        options = {
            "user_data_dir": str(self.profile_dir),
            "channel": "chrome",
            "headless": self.headless,
            "no_viewport": True,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.proxy:
            options["proxy"] = {"server": self.proxy}
        logger.info("启动 Chrome（代理: %s，%s）", self.proxy or "无", "无头" if self.headless else "有头")
        self._context = self._playwright.chromium.launch_persistent_context(**options)
        self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def _throttle(self) -> None:
        wait = self._last_nav + MIN_INTERVAL + random.uniform(0, JITTER) - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_nav = time.time()

    def _wait_challenge(self, page, url: str) -> str:
        deadline = time.time() + AUTO_WAIT
        notified = False
        while True:
            try:
                html = page.content()
                title = page.title()
            except Exception as error:
                # 验证通过后页面会自动跳转，跳转过程中读不到内容，稍后重试
                if "navigating" not in str(error).lower():
                    raise
                page.wait_for_timeout(1000)
                continue
            # 以"拿到了真正的 HLTV 页面"为准；Chrome 渲染后的验证页不一定带验证标记
            if is_hltv_page(html) or (not is_challenge(title, html) and "hltv.org" not in page.url):
                return html
            if time.time() > deadline:
                if notified:
                    raise HLTVBlockedError("HLTV 的 Cloudflare 验证长时间未通过，请在弹出的 Chrome 窗口中完成验证后重试")
                notified = True
                deadline = time.time() + MANUAL_WAIT
                logger.warning("HLTV 验证未自动通过，等待人工处理：%s", url)
                if self.on_challenge:
                    try:
                        self.on_challenge(url)
                    except Exception:
                        logger.exception("验证提醒发送失败")
                if self.headless:
                    raise HLTVBlockedError("无头模式下无法人工完成 Cloudflare 验证，请在设置中关闭无头模式")
            page.wait_for_timeout(1500)

    def _fetch_sync(self, url: str) -> str:
        self._cancel_idle_timer()
        try:
            page = self._ensure_page()
            self._throttle()
            logger.info("HLTV 浏览器访问: %s", url)
            page.goto(url, wait_until="domcontentloaded")
            html = self._wait_challenge(page, url)
            # 部分内容（比分、时间）由脚本渲染，稍等片刻再取
            page.wait_for_timeout(800)
            try:
                return page.content()
            except Exception:
                return html
        except HLTVBlockedError:
            raise
        except Exception:
            # 浏览器被手动关掉等情况：下次重开
            self._close()
            raise
        finally:
            self._last_used = time.time()
            self._schedule_idle_close()

    def _watch_sync(self, key: str, url: str, reload: bool) -> Tuple[str, float]:
        self._cancel_idle_timer()
        try:
            self._ensure_page()
            entry = self._watched.get(key)
            page = entry[0] if entry and not entry[0].is_closed() else None
            if page is None or reload:
                if page is None:
                    page = self._context.new_page()
                self._throttle()
                logger.info("HLTV 监视页%s: %s", "刷新" if entry else "打开", url)
                page.goto(url, wait_until="domcontentloaded")
                self._wait_challenge(page, url)
                self._watched[key] = (page, time.time())
                # 进行中的比赛等实时比分板连上 scorebot
                if (page.evaluate("() => document.querySelector('.countdown')?.textContent || ''") or "").strip().upper() == "LIVE":
                    try:
                        page.wait_for_selector(".scoreboard .currentRoundText", state="attached",
                                               timeout=SCOREBOARD_WAIT_MS)
                    except Exception:
                        logger.info("实时比分板未出现（可能在换图间隙）: %s", url)
            return page.content(), self._watched[key][1]
        except HLTVBlockedError:
            raise
        except Exception:
            self._unwatch_sync(key)
            raise
        finally:
            self._last_used = time.time()
            self._schedule_idle_close()

    def _unwatch_sync(self, key: str) -> None:
        entry = self._watched.pop(key, None)
        if entry:
            try:
                entry[0].close()
            except Exception:
                pass

    def _schedule_idle_close(self) -> None:
        self._idle_timer = threading.Timer(IDLE_CLOSE, lambda: self._executor.submit(self._close_if_idle))
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _close_if_idle(self) -> None:
        if not self._watched and time.time() - self._last_used >= IDLE_CLOSE - 1:
            logger.info("HLTV 浏览器空闲，关闭")
            self._close()

    def _close(self) -> None:
        for closer in (lambda: self._context and self._context.close(),
                       lambda: self._playwright and self._playwright.stop()):
            try:
                closer()
            except Exception:
                pass
        self._context = self._page = self._playwright = None
        self._watched.clear()
