import asyncio
import sys
import unittest
from pathlib import Path


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

from services.hltv_scraper import HLTVScraper


class FakeResponse:
    def __init__(self, status_code, text, url="https://www.hltv.org/search?query=vitality"):
        self.status_code = status_code
        self.text = text
        self.url = url


class FakeSession:
    async def get(self, url, timeout):
        return FakeResponse(
            403,
            """
            <!DOCTYPE html>
            <html lang="en-US">
              <head><title>Just a moment...</title></head>
              <body>
                <div id="challenge-error-title">
                  Enable JavaScript and cookies to continue
                </div>
              </body>
            </html>
            """,
            url,
        )


class HLTVScraperTest(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_sets_browser_user_agent(self):
        scraper = HLTVScraper()
        await scraper.initialize()
        try:
            user_agent = scraper._session.headers.get("User-Agent")
            self.assertIsNotNone(user_agent)
            self.assertIn("Mozilla/5.0", user_agent)
            self.assertIn("Chrome", user_agent)
        finally:
            await scraper.close()

    async def test_get_raises_clear_error_for_cloudflare_challenge(self):
        scraper = HLTVScraper()
        scraper._session = FakeSession()

        with self.assertLogs("services.hltv_scraper", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "Cloudflare"):
                await scraper._get("https://www.hltv.org/search?query=vitality")

        self.assertIn("HLTV Cloudflare challenge", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
