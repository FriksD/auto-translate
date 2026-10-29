"""
从 HLTV 组装任务的背景信息（context）。

以比赛页为主：比赛当时的阵容（含真名、国籍、替补）、排名、BP、地图比分、
双方近期战绩、交手记录、直播源。战队页补充教练、近 3 个月图池、相关新闻——
这些是"当前"数据，只有比赛发生在最近 RECENT_DAYS 天内时才使用。

没有比赛 ID 的手动任务按队名搜索战队页，使用当前阵容。
"""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from ..sources.hltv import HLTVClient

logger = logging.getLogger(__name__)

RECENT_DAYS = 30
NEWS_DAYS = 14
NEWS_LIMIT = 6


def _team_entry(name: str, team_id: Optional[int], players: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": team_id,
        "name": name,
        "players": [p["name"] for p in players],
        "player_details": [
            {k: p.get(k, "") for k in ("name", "real_name", "nationality", "standin_for")} for p in players
        ],
        "stats": {},
    }


def _apply_team_page(entry: Dict[str, Any], team: Dict[str, Any], reference_ms: Optional[int],
                     use_roster: bool) -> None:
    coach = team.get("coach") or {}
    if coach.get("nick"):
        entry["stats"]["coach"] = coach["nick"]
        entry["coach_full"] = coach.get("full", "")
    entry.setdefault("hltv_rank", team.get("hltv_rank"))
    entry.setdefault("vrs_rank", team.get("vrs_rank"))
    entry["map_stats"] = team.get("map_stats") or []
    entry["recent_results"] = team.get("recent_results") or []
    since = (reference_ms or time.time() * 1000) - NEWS_DAYS * 86400 * 1000
    entry["news"] = [n for n in team.get("news") or [] if (n.get("unix_ms") or 0) >= since][:NEWS_LIMIT]
    if use_roster:
        entry["players"] = [p["name"] for p in team.get("roster") or []]


def _match_summary(match: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": match["id"],
        "url": match["url"],
        "event": match["event"]["name"],
        "format": match["format"],
        "unix_ms": match["unix_ms"],
        "status": match["status"],
        "score": [match["team1"].get("score"), match["team2"].get("score")],
        "veto": match["veto"],
        "maps": match["maps"],
        "head_to_head": match["head_to_head"],
        "streams": match["streams"],
    }


async def build_from_match(client: HLTVClient, match_id: int) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    match = await client.get_match(match_id, refresh=True)
    recent = match["unix_ms"] and abs(time.time() * 1000 - match["unix_ms"]) < RECENT_DAYS * 86400 * 1000

    teams = []
    for side in ("team1", "team2"):
        info = match[side]
        if not info.get("name"):
            warnings.append(f"{side} 尚未确定（TBD），开赛前会再次刷新")
            continue
        entry = _team_entry(info["name"], info.get("id"), info.get("players") or [])
        entry["hltv_rank"] = info.get("hltv_rank")
        entry["vrs_rank"] = info.get("vrs_rank")
        entry["past_matches"] = (match.get("past_matches") or {}).get(side, [])[:8]
        if info.get("id") and recent:
            try:
                team = await client.get_team(info["id"])
                # 比赛页还没公布阵容时，用战队页的当前阵容
                _apply_team_page(entry, team, match["unix_ms"], use_roster=not entry["players"])
            except Exception as error:
                warnings.append(f"{info['name']} 战队页抓取失败：{error}")
        teams.append(entry)

    if not recent:
        warnings.append("比赛不在最近 30 天内，图池、新闻、教练等当前数据未使用，阵容以比赛页为准")
    return {"match": _match_summary(match), "teams": teams}, warnings


async def build_from_team_names(client: HLTVClient, names: List[str]) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    teams = []
    for name in names:
        try:
            found = await client.search_team(name)
            if not found:
                warnings.append(f"HLTV 搜索不到战队：{name}")
                teams.append({"name": name, "players": [], "stats": {}, "error": "搜索不到"})
                continue
            team = await client.get_team(found["id"])
            entry = _team_entry(team["name"], found["id"], [])
            entry["hltv_rank"] = team.get("hltv_rank")
            entry["vrs_rank"] = team.get("vrs_rank")
            _apply_team_page(entry, team, None, use_roster=True)
            teams.append(entry)
        except Exception as error:
            warnings.append(f"抓取 {name} 失败：{error}")
            teams.append({"name": name, "players": [], "stats": {}, "error": str(error)})
    return {"teams": teams}, warnings
