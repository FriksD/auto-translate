"""
bo3.gg 数据源：公开 JSON 接口 api.bo3.gg，不需要 key，也没有 Cloudflare 验证（bo3.gg 网站本身有）。

接口没有文档，字段随时可能变化，所以解析集中在 normalize_match，出错时由比赛监视退回 HLTV。
实测（2026-10）：
- 比赛列表 /matches 支持按 status 过滤，不支持按日期过滤；单场 /matches/{slug}?with=games,streams。
- 每张图（games）有 upcoming / current / finished 状态和胜方，打完的图有比分。
- 普通比赛没有回合级实时比分（live_coverage=false），只能知道"哪张图正在打"。
- streams 里 official=true 的是赛事方的直播（含各语言官方二路），未开赛的比赛通常还是空的。
- 队名和 HLTV 不完全一致（如 slug 里的 mouz-cs2、falcons-esports），按规范化后的名字匹配。
"""
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

API = "https://api.bo3.gg/api/v1"
SITE = "https://bo3.gg"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
# 和预约时间相差多少以内算同一场（赛程经常顺延）
MATCH_TIME_TOLERANCE = 12 * 3600
FILLER_WORDS = {"team", "esports", "esport", "gaming", "club", "gg", "cs", "cs2", "csgo", "go"}


class Bo3Error(RuntimeError):
    pass


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def norm_team(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    kept = [w for w in words if w not in FILLER_WORDS]
    return "".join(kept or words)


def team_names(team: Optional[Dict[str, Any]]) -> List[str]:
    if not team:
        return []
    names = [team.get("name") or "", (team.get("slug") or "").replace("-", " ")]
    names += [clan.get("clan_name") or "" for clan in team.get("team_clans") or []]
    return [n for n in names if n]


def same_team(name: str, candidates: List[str]) -> bool:
    target = norm_team(name)
    if not target:
        return False
    for candidate in candidates:
        other = norm_team(candidate)
        if not other:
            continue
        if other == target or (min(len(other), len(target)) >= 4 and (other in target or target in other)):
            return True
    return False


def parse_time(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def find_match(results: List[Dict[str, Any]], team1: str, team2: str,
               start_ts: Optional[float]) -> Optional[Tuple[Dict[str, Any], bool]]:
    """在比赛列表里找同一场比赛，返回 (比赛, 队伍顺序是否和预约时相反)。"""
    best = None
    for match in results:
        names1, names2 = team_names(match.get("team1")), team_names(match.get("team2"))
        if same_team(team1, names1) and same_team(team2, names2):
            swapped = False
        elif same_team(team1, names2) and same_team(team2, names1):
            swapped = True
        else:
            continue
        begin = parse_time(match.get("start_date"))
        gap = abs(begin - start_ts) if begin and start_ts else 0
        if gap > MATCH_TIME_TOLERANCE:
            continue
        if best is None or gap < best[0]:
            best = (gap, match, swapped)
    return (best[1], best[2]) if best else None


def map_display_name(raw: str) -> str:
    if not raw:
        return "TBA"  # 还没选图
    name = raw.lower()
    if name.startswith("de_"):
        name = name[3:]
    return {"dust2": "Dust2"}.get(name, name.capitalize())


def normalize_stream(stream: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = stream.get("raw_url") or ""
    provider = next((p for p in ("twitch", "youtube", "kick") if p in url), None)
    if not url or provider is None:
        return None
    official = bool(stream.get("official"))
    language = stream.get("language") or ""
    return {
        "provider": provider,
        "name": stream.get("name") or url.rstrip("/").rsplit("/", 1)[-1],
        # 和 HLTV 的标记方式一致：赛事方英文主直播记为 "Other"
        "country": "Other" if official and language in ("en", "") else (language or "?"),
        "official": official,
        "language": language,
        "viewers": stream.get("viewers_number") or 0,
        "url": url,
        "source": "bo3",
    }


def normalize_match(raw: Dict[str, Any], swapped: bool = False) -> Dict[str, Any]:
    """转换成和 HLTV 解析结果相同的结构；swapped=True 时交换两队，和预约时的 team1/team2 对齐。"""
    team_ids = [raw.get("team1_id"), raw.get("team2_id")]
    teams = [{"name": (raw.get(key) or {}).get("name") or "TBD"} for key in ("team1", "team2")]
    scores = [raw.get("team1_score"), raw.get("team2_score")]
    if swapped:
        team_ids.reverse()
        teams.reverse()
        scores.reverse()

    maps = []
    for game in sorted(raw.get("games") or [], key=lambda g: g.get("number") or 0):
        status = {"current": "live", "finished": "finished"}.get(game.get("status"), "upcoming")
        winner_id = (game.get("winner_team_clan") or {}).get("team_id")
        winner = team_ids.index(winner_id) + 1 if status == "finished" and winner_id in team_ids else None
        s1 = s2 = None
        if winner and game.get("winner_clan_score") is not None:
            high, low = game["winner_clan_score"], game.get("loser_clan_score")
            s1, s2 = (high, low) if winner == 1 else (low, high)
        maps.append({"name": map_display_name(game.get("map_name")), "team1_score": s1, "team2_score": s2,
                     "status": status, "winner": winner, "began_at": parse_time(game.get("begin_at"))})

    status = raw.get("status")
    if status in ("finished", "defwin"):
        state = "over"
    elif any(m["status"] != "upcoming" for m in maps):
        state = "live"
    else:
        state = "upcoming"
    streams = [s for s in (normalize_stream(x) for x in raw.get("streams") or []) if s]
    return {
        "status": state,
        "best_of": raw.get("bo_type") or len(maps) or 1,
        "maps": maps,
        "team1": {**teams[0], "score": scores[0]},
        "team2": {**teams[1], "score": scores[1]},
        "event": (raw.get("tournament") or {}).get("name"),
        "start_time": parse_time(raw.get("start_date")),
        "streams": streams,
        # 普通比赛没有回合级实时比分，只知道哪张图正在打
        "live": None,
        "has_round_score": False,
        "url": f"{SITE}/matches/{raw.get('slug')}",
        "slug": raw.get("slug"),
        "source": "bo3",
        "loaded_at": time.time(),
    }


# ── 客户端 ───────────────────────────────────────────────────────────────────

class Bo3Client:
    def __init__(self, proxy: Optional[str] = None, timeout: float = 20):
        self.proxy = proxy
        self.timeout = timeout

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(proxy=self.proxy, timeout=self.timeout,
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
            response = await client.get(f"{API}{path}", params=params)
        if response.status_code != 200:
            raise Bo3Error(f"bo3.gg 返回 {response.status_code}：{path}")
        try:
            return response.json()
        except ValueError:
            raise Bo3Error(f"bo3.gg 返回的不是 JSON：{path}")

    async def list_matches(self, statuses: str, sort: str, limit: int = 100) -> List[Dict[str, Any]]:
        data = await self._get("/matches", {
            "page[limit]": str(limit), "sort": sort, "with": "teams",
            "filter[matches.status][in]": statuses, "filter[discipline_id][eq]": "1",
        })
        return data.get("results") or []

    async def find(self, team1: str, team2: str, start_ts: Optional[float]) -> Optional[Tuple[str, bool]]:
        """按队名和开赛时间找到 bo3.gg 上的同一场比赛，返回 (slug, 队伍顺序是否相反)。"""
        results = await self.list_matches("current,upcoming", "start_date")
        if not start_ts or start_ts < time.time():
            results += await self.list_matches("finished", "-start_date", 50)
        found = find_match(results, team1, team2, start_ts)
        return (found[0]["slug"], found[1]) if found else None

    async def get_match(self, slug: str, swapped: bool = False) -> Dict[str, Any]:
        raw = await self._get(f"/matches/{slug}", {"with": "games,streams"})
        return normalize_match(raw, swapped)
