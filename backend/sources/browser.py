"""
用真实 Chrome 访问 HLTV，解决 Cloudflare 拦截。

- Playwright 驱动本机安装的 Chrome（channel="chrome"），有头模式，持久化用户目录
  （data/browser-profile），cf_clearance 等 cookie 会一直保存，过一次验证后长期有效。
- 所有浏览器操作在一个专用线程里串行执行（Playwright 同步 API 不能跨线程），
  异步代码通过 fetch() 等待结果。
- 两次访问之间至少间隔 MIN_INTERVAL 秒并加随机抖动，避免高频触发验证。
- 遇到验证页先等它自动通过；超过 AUTO_WAIT 秒仍未通过，调用 on_challenge 回调
  （发飞书通知）请人在弹出的 Chrome 窗口里手动完成验证，最多再等 MANUAL_WAIT 秒。
- 空闲超过 IDLE_CLOSE 秒自动关闭浏览器，下次访问时重新打开。
"""
import asyncio
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MIN_INTERVAL = 3.0
JITTER = 2.0
AUTO_WAIT = 20
MANUAL_WAIT = 300
IDLE_CLOSE = 600
NAV_TIMEOUT_MS = 45000


class HLTVBlockedError(RuntimeError):
    """Cloudflare 验证在等待时间内没有通过。"""


def is_challenge(title: str, html: str) -> bool:
    lowered = html[:5000].lower()
    return (
        "just a moment" in title.lower()
        or "challenge-error-title" in lowered
        or "cf-challenge" in lowered
        or "enable javascript and cookies to continue" in lowered
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
            "viewport": {"width": 1280, "height": 860},
            "locale": "en-US",
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
            if not is_challenge(title, html):
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

    def _schedule_idle_close(self) -> None:
        self._idle_timer = threading.Timer(IDLE_CLOSE, lambda: self._executor.submit(self._close_if_idle))
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _close_if_idle(self) -> None:
        if time.time() - self._last_used >= IDLE_CLOSE - 1:
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
