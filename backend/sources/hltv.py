"""
HLTV 数据客户端：通过真实 Chrome（browser.py）抓取页面，用 hltv_parse.py 解析，带内存缓存。
"""
import logging
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from . import hltv_parse
from .browser import BrowserFetcher, HLTVBlockedError

logger = logging.getLogger(__name__)

HLTV = hltv_parse.HLTV
TTL_MATCH_LIST = 600
TTL_MATCH = 120
TTL_TEAM = 3600
TTL_SEARCH = 86400

__all__ = ["HLTVClient", "HLTVBlockedError"]


class HLTVClient:
    def __init__(self, fetcher: BrowserFetcher):
        self.fetcher = fetcher
        self._cache: Dict[str, Tuple[float, Any]] = {}

    async def _cached(self, key: str, ttl: int, url: str, parse, refresh: bool = False):
        hit = self._cache.get(key)
        if hit and not refresh and time.time() - hit[0] < ttl:
            return hit[1]
        value = parse(await self.fetcher.fetch(url))
        self._cache[key] = (time.time(), value)
        return value

    def cached_at(self, key: str) -> Optional[float]:
        hit = self._cache.get(key)
        return hit[0] if hit else None

    async def list_matches(self, refresh: bool = False) -> List[Dict[str, Any]]:
        return await self._cached("matches", TTL_MATCH_LIST, f"{HLTV}/matches", hltv_parse.parse_matches, refresh)

    async def get_match(self, match_id: int, refresh: bool = False) -> Dict[str, Any]:
        # slug 部分可以随便写，HLTV 会按 ID 定位比赛
        match = await self._cached(
            f"match:{match_id}", TTL_MATCH, f"{HLTV}/matches/{match_id}/match", hltv_parse.parse_match, refresh
        )
        return {**match, "id": match_id, "url": f"{HLTV}/matches/{match_id}/match"}

    async def watch_match(self, match_id: int, reload: bool = False) -> Dict[str, Any]:
        """读取监视标签页里的比赛页（含实时比分板），不经过缓存。"""
        url = f"{HLTV}/matches/{match_id}/match"
        html, loaded_at = await self.fetcher.watch(f"match:{match_id}", url, reload)
        match = hltv_parse.parse_match(html)
        return {**match, "id": match_id, "url": url,
                "scoreboard": hltv_parse.parse_scoreboard(html), "loaded_at": loaded_at}

    async def unwatch_match(self, match_id: int) -> None:
        await self.fetcher.unwatch(f"match:{match_id}")

    async def get_team(self, team_id: int, refresh: bool = False) -> Dict[str, Any]:
        team = await self._cached(
            f"team:{team_id}", TTL_TEAM, f"{HLTV}/team/{team_id}/team", hltv_parse.parse_team, refresh
        )
        return {**team, "id": team_id, "url": f"{HLTV}/team/{team_id}/team"}

    async def search_team(self, name: str) -> Optional[Dict[str, Any]]:
        url = f"{HLTV}/search?query={urllib.parse.quote(name)}"
        return await self._cached(f"search:{name.lower()}", TTL_SEARCH, url, hltv_parse.parse_search_team)
