"""
HLTV数据客户端

使用 curl-cffi 伪造 Chrome TLS 指纹绕过 Cloudflare，
直接解析 HLTV 页面，支持战队/选手查询和近期比赛。
"""
import asyncio
import logging
import time
import urllib.parse
from typing import Optional, List, Dict, Any

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

HLTV = "https://www.hltv.org"
CACHE_TTL = 3600  # 缓存 1 小时
HLTV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": HLTV,
}


class HLTVBlockedError(RuntimeError):
    """Raised when HLTV returns a Cloudflare challenge page."""


class HLTVScraper:
    def __init__(self):
        self._session: Optional[AsyncSession] = None
        self._teams_cache: Optional[List[Dict]] = None
        self._teams_ts: float = 0
        self._players_cache: Optional[Dict[str, Dict]] = None  # nickname → {id, team}
        self._players_ts: float = 0

    async def initialize(self):
        """在 FastAPI lifespan 启动时调用。"""
        self._session = AsyncSession(impersonate="chrome", headers=HLTV_HEADERS)
        logger.info("HLTV 客户端初始化完成（curl-cffi / Chrome TLS 指纹）")

    async def close(self):
        """在 FastAPI lifespan 退出时调用。"""
        if self._session:
            await self._session.close()
            self._session = None

    def _ensure_initialized(self):
        if not self._session:
            raise RuntimeError("HLTVScraper 未初始化，请先调用 initialize()")

    async def _get(self, url: str) -> Optional[BeautifulSoup]:
        """发起 GET 请求并返回 BeautifulSoup 对象。"""
        try:
            r = await self._session.get(url, timeout=20)
            if r.status_code != 200:
                if self._is_cloudflare_challenge(r.text):
                    logger.warning("HLTV Cloudflare challenge %s: %s", r.status_code, url)
                    raise HLTVBlockedError(
                        "HLTV 请求被 Cloudflare 拦截，请稍后重试或检查网络环境"
                    )
                logger.warning("HTTP %s: %s", r.status_code, url)
                return None
            return BeautifulSoup(r.text, "lxml")
        except HLTVBlockedError:
            raise
        except Exception as e:
            logger.error("请求失败 %s: %s", url, e)
            return None

    @staticmethod
    def _is_cloudflare_challenge(html: str) -> bool:
        """判断响应是否为 Cloudflare challenge 页面。"""
        lowered = html[:2000].lower()
        return (
            "just a moment" in lowered
            or "challenge-error-title" in lowered
            or "enable javascript and cookies" in lowered
        )

    # ── 缓存层 ────────────────────────────────────────────────────────────────

    async def _get_top_teams(self) -> List[Dict]:
        """
        从 /ranking/teams 抓取 Top 30，结果缓存 1 小时。
        返回: [{'id', 'rank', 'title', 'points'}]
        """
        if self._teams_cache and time.time() - self._teams_ts < CACHE_TTL:
            return self._teams_cache

        soup = await self._get(f"{HLTV}/ranking/teams")
        if not soup:
            return self._teams_cache or []

        teams = []
        for team in soup.find_all("div", class_="ranked-team standard-box"):
            try:
                rank = team.find("span", class_="position").text.lstrip("#")
                title_div = (
                    team.find("div", class_="teamLine sectionTeamPlayers teamLineExpanded")
                    or team.find("div", class_="teamLine sectionTeamPlayers")
                )
                title = title_div.find("span", class_="name").text
                points = title_div.find("span", class_="points").text.split()[0].lstrip("(")
                tid = team.find("a", class_="details moreLink")["href"].split("/")[-1]
                teams.append({"id": tid, "rank": rank, "title": title, "points": points})
            except (AttributeError, KeyError, TypeError):
                pass

        if teams:
            self._teams_cache = teams
            self._teams_ts = time.time()
        return teams

    async def _get_player_map(self) -> Dict[str, Dict]:
        """
        并发拉取 Top 15 战队的详情页，构建 nickname→{id, team} 映射，缓存 1 小时。
        """
        if self._players_cache and time.time() - self._players_ts < CACHE_TTL:
            return self._players_cache

        teams = await self._get_top_teams()
        player_map: Dict[str, Dict] = {}

        async def fetch_team_players(t: Dict):
            slug = t["title"].lower().replace(" ", "-")
            soup = await self._get(f"{HLTV}/team/{t['id']}/{slug}")
            if not soup:
                return
            player_div = soup.find("div", class_="bodyshot-team g-grid")
            if not player_div:
                return
            for a in player_div.find_all("a"):
                nick_span = a.find("span", class_="text-ellipsis bold")
                pid = a["href"].split("/")[2] if a.get("href") else None
                if nick_span and pid:
                    player_map[nick_span.text] = {"id": pid, "team": t["title"]}

        await asyncio.gather(*[fetch_team_players(t) for t in teams[:15]])

        if player_map:
            self._players_cache = player_map
            self._players_ts = time.time()
        return player_map

    async def _search_hltv_entity(
        self, query: str, entity_type: str
    ) -> Optional[Dict[str, str]]:
        """
        Fallback: 通过HLTV搜索页面查找战队或选手（不受Top30/15限制）。
        entity_type: 'team' 或 'player'
        返回: {'id': str, 'slug': str, 'name': str} 或 None
        """
        url = f"{HLTV}/search?query={urllib.parse.quote(query)}"
        soup = await self._get(url)
        if not soup:
            return None

        prefix = f"/{entity_type}/"
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(prefix):
                parts = href.split("/")
                # href格式: /team/6667/natus-vincere → ['', 'team', '6667', 'natus-vincere']
                if len(parts) >= 4 and parts[2].isdigit():
                    entity_id = parts[2]
                    slug = parts[3]
                    name_div = a.find("div", class_="name")
                    display_name = name_div.get_text(strip=True) if name_div else slug
                    logger.info("HLTV搜索找到 %s: %s (id=%s)", entity_type, display_name, entity_id)
                    return {"id": entity_id, "slug": slug, "name": display_name}
        return None

    # ── 公开接口 ───────────────────────────────────────────────────────────────

    async def search_teams(self, query: str) -> List[Dict[str, Any]]:
        """从 Top 30 排名中模糊搜索战队。"""
        self._ensure_initialized()
        try:
            teams = await self._get_top_teams()
            q = query.lower()
            return [
                {"name": t["title"], "id": t["id"], "ranking": t["rank"]}
                for t in teams if q in t["title"].lower()
            ][:5]
        except Exception as e:
            logger.error("搜索战队失败: %s", e)
            return []

    async def search_players(self, query: str) -> List[Dict[str, Any]]:
        """从 Top 15 战队的阵容中模糊搜索选手。"""
        self._ensure_initialized()
        try:
            player_map = await self._get_player_map()
            q = query.lower()
            return [
                {"name": nick, "id": info["id"], "team": info["team"]}
                for nick, info in player_map.items() if q in nick.lower()
            ][:5]
        except Exception as e:
            logger.error("搜索选手失败: %s", e)
            return []

    async def get_team_info(self, team_name: str) -> Dict[str, Any]:
        """
        获取战队详细信息。

        Returns:
            name, ranking, points, players, stats(rank/weeks_top30/avg_age/coach)
        """
        self._ensure_initialized()
        try:
            teams = await self._get_top_teams()
            t_lower = team_name.lower()
            entry = next((t for t in teams if t_lower in t["title"].lower()), None)
            if not entry:
                logger.info("战队 '%s' 不在Top 30，尝试HLTV搜索...", team_name)
                found = await self._search_hltv_entity(team_name, "team")
                if not found:
                    return {"error": f"未在Top 30中找到战队，且HLTV搜索无结果: {team_name}"}
                entry = {
                    "id": found["id"],
                    "title": found["name"],
                    "rank": "N/A",
                    "points": "N/A",
                }
                slug = found["slug"]
            else:
                slug = entry["title"].lower().replace(" ", "-")
            soup = await self._get(f"{HLTV}/team/{entry['id']}/{slug}")
            if not soup:
                return {"error": "无法获取战队详情页"}

            # 现役阵容
            players = []
            player_div = soup.find("div", class_="bodyshot-team g-grid")
            if player_div:
                players = [
                    a.find("span", class_="text-ellipsis bold").text
                    for a in player_div.find_all("a")
                    if a.find("span", class_="text-ellipsis bold")
                ]

            # 为每个选手获取真实姓名（并发请求）
            player_details: List[Dict[str, Any]] = []
            if players:
                async def fetch_player_detail(nick: str) -> Dict[str, Any]:
                    try:
                        # 构建选手URL（需要从页面获取player id）
                        for a in player_div.find_all("a"):
                            nick_span = a.find("span", class_="text-ellipsis bold")
                            if nick_span and nick_span.text == nick:
                                href = a.get("href", "")
                                if href:
                                    pid = href.split("/")[2]
                                    player_url = f"{HLTV}/player/{pid}/{nick.lower()}"
                                    p_soup = await self._get(player_url)
                                    if p_soup:
                                        name_div = p_soup.find("div", class_="playerRealname")
                                        real_name = name_div.get_text(strip=True) if name_div else ""
                                        nat_img = name_div.find("img") if name_div else None
                                        nationality = nat_img["title"] if nat_img else ""
                                        stat_spans = p_soup.find_all("span", class_="statsVal")
                                        stat_keys = ["rating", "dpr", "kast_pct"]
                                        stats = {k: stat_spans[i].get_text(strip=True) for i, k in enumerate(stat_keys) if i < len(stat_spans)}
                                        return {"name": nick, "real_name": real_name, "nationality": nationality, "stats": stats}
                                    break
                    except Exception as e:
                        logger.warning("获取选手 %s 详情失败: %s", nick, e)
                    return {"name": nick, "real_name": "", "nationality": "", "stats": {}}

                player_details = await asyncio.gather(*[fetch_player_detail(nick) for nick in players])

            # 统计数据（按位置解析）
            stat_labels = ["world_ranking", "weeks_top30", "avg_age", "coach"]
            stats: Dict[str, Any] = {}
            for i, stat in enumerate(soup.find_all("div", class_="profile-team-stat"), 1):
                try:
                    if i == 1:
                        a = stat.find("a")
                        stats["world_ranking"] = a.text.lstrip("#") if a else None
                    elif i == 2:
                        span = stat.find("span", class_="right")
                        stats["weeks_top30"] = span.text if span else None
                    elif i == 3:
                        span = stat.find("span", class_="right")
                        stats["avg_age"] = span.text if span else None
                    elif i == 4:
                        span = stat.find("span", class_="bold a-default")
                        stats["coach"] = span.text.strip("'") if span else None
                except AttributeError:
                    pass

            return {
                "name": entry["title"],
                "ranking": entry["rank"],
                "points": entry["points"],
                "players": players,
                "player_details": player_details,
                "stats": stats,
            }
        except HLTVBlockedError as e:
            logger.error("获取战队信息被拦截: %s", e)
            return {"error": str(e)}
        except Exception as e:
            logger.error("获取战队信息失败: %s", e)
            return {"error": str(e)}

    async def get_player_info(self, player_name: str) -> Dict[str, Any]:
        """
        获取选手详细信息。

        先从 Top 15 战队阵容中找到选手 ID，再拉取个人页。
        Returns:
            name, real_name, team, nationality, age, stats(rating/dpr/kast%)
        """
        self._ensure_initialized()
        try:
            player_map = await self._get_player_map()
            p_lower = player_name.lower()
            match = next(
                ((nick, info) for nick, info in player_map.items() if p_lower in nick.lower()),
                None,
            )
            if not match:
                logger.info("选手 '%s' 不在Top 15阵容，尝试HLTV搜索...", player_name)
                found = await self._search_hltv_entity(player_name, "player")
                if not found:
                    return {"error": f"未在Top 15阵容中找到选手，且HLTV搜索无结果: {player_name}"}
                player_url = f"{HLTV}/player/{found['id']}/{found['slug']}"
                display_nick = found["name"]
            else:
                nick, info = match
                player_url = f"{HLTV}/player/{info['id']}/{nick.lower()}"
                display_nick = nick

            soup = await self._get(player_url)
            if not soup:
                return {"error": "无法获取选手详情页"}

            # 基本信息
            name_div = soup.find("div", class_="playerRealname")
            real_name = name_div.get_text(strip=True) if name_div else ""
            nat_img = name_div.find("img") if name_div else None
            nationality = nat_img["title"] if nat_img else ""

            team_row = soup.find("div", class_="playerInfoRow playerTeam")
            fallback_team = info["team"] if match else player_name
            team = (
                team_row.find("a").get_text(strip=True)
                if team_row and team_row.find("a")
                else fallback_team
            )

            age_row = soup.find("div", class_="playerInfoRow playerAge")
            age_text = (
                age_row.find("span", class_="listRight").get_text(strip=True)
                if age_row else ""
            )
            age = int(age_text.split()[0]) if age_text else None

            # 统计数据（Rating / DPR / KAST%）
            stat_spans = soup.find_all("span", class_="statsVal")
            stat_keys = ["rating", "dpr", "kast_pct"]
            stats = {k: stat_spans[i].get_text(strip=True) for i, k in enumerate(stat_keys) if i < len(stat_spans)}

            return {
                "name": display_nick,
                "real_name": real_name,
                "team": team,
                "nationality": nationality,
                "age": age,
                "stats": stats,
            }
        except HLTVBlockedError as e:
            logger.error("获取选手信息被拦截: %s", e)
            return {"error": str(e)}
        except Exception as e:
            logger.error("获取选手信息失败: %s", e)
            return {"error": str(e)}

    async def get_recent_matches(self, team_name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        从 /results 页面获取战队近期比赛结果。

        Returns:
            [{'id', 'team1', 'team2', 'score', 'event'}]
        """
        self._ensure_initialized()
        try:
            soup = await self._get(f"{HLTV}/results")
            if not soup:
                return []

            t_lower = team_name.lower()
            results = []
            for res in soup.select(".results-sublist .a-reset"):
                href = res.get("href", "")
                if not href or href == "/forums":
                    continue
                teams = res.find_all("td", class_="team-cell")
                score = res.find("td", class_="result-score")
                if not teams or not score:
                    continue
                team1 = teams[0].get_text(strip=True)
                team2 = teams[1].get_text(strip=True)
                if t_lower not in team1.lower() and t_lower not in team2.lower():
                    continue
                event_elem = res.find("span", class_="event-name")
                results.append({
                    "id": href.split("/")[2],
                    "team1": team1,
                    "team2": team2,
                    "score": score.get_text(strip=True),
                    "event": event_elem.get_text(strip=True) if event_elem else "",
                })
                if len(results) >= limit:
                    break
            return results
        except Exception as e:
            logger.error("获取近期比赛失败: %s", e)
            return []
