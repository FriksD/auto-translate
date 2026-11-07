"""
直播间截下来的采访：事后识别受访者、战队和对应的比赛，生成背景信息。

直播间监视不绑定比赛，截取时只有 LLM 猜的受访者和战队。抓取背景信息这一步：
1. 候选战队：HLTV 赛程里这个赛事的队伍，加上 bo3.gg 上采访前后几小时内有比赛的队伍，
   再加上这个直播间之前识别出的队伍。HLTV 访问不了也不要紧，bo3.gg 不走浏览器。
2. LLM 读采访的滚动转写，从候选里认出受访者、所属战队、对手，以及采访说的是哪场比赛：
   刚打完的（just_played）、图间（between_maps）、赛前（upcoming）、和具体比赛无关（general）。
3. 刚打完或图间的采访，到 bo3.gg 找这支队采访前后的比赛；对手对得上（LLM 没说对手时只有一场候选）才算确认，
   确认了才把比分写进背景信息。否则只写赛事，摘要不写比分，并提示人工核对。
4. 受访者战队（和对手）的阵容照旧从 HLTV 抓（context.build_from_team_names）。
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .. import config, db
from ..sources import bo3
from ..sources.hltv import HLTVClient
from . import context as context_builder
from . import prompts

logger = logging.getLogger(__name__)

# bo3.gg 上采访前后多久内有比赛的队伍算候选
NEARBY_SECONDS = 4 * 3600
# 刚打完的比赛：开赛时间最早在采访前多久
MATCH_LOOKBACK = 8 * 3600
ABOUT_MATCH = ("just_played", "between_maps", "upcoming", "general", "unknown")

IDENTIFY_SYSTEM = (
    "You identify who is being interviewed in a CS2 esports broadcast interview and which match it is about. "
    "You answer with strict JSON only."
)


# ── 纯函数 ───────────────────────────────────────────────────────────────────

# 赛事名缩写：bo3.gg 写全称（Intel Extreme Masters Beijing 2026），HLTV 和人工填的常用缩写
EVENT_ABBREVIATIONS = {"iem": "intel extreme masters", "epl": "esl pro league", "bmc": "blast major championship"}
# 只有 S / A 级赛事有直播采访；赛事名对不上时，附近比赛只取这些
MAIN_TIERS = ("s", "a")


def event_words(name: Any) -> List[str]:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    expanded: List[str] = []
    for word in words:
        expanded += EVENT_ABBREVIATIONS.get(word, word).split()
    return expanded


def same_event_name(a: Any, b: Any) -> bool:
    """
    较短的赛事名里的词都出现在另一个里：IEM Beijing 2026 ≈ Intel Extreme Masters Beijing 2026。
    两边都至少要有两个英文词：「IEM北京」只剩一个 IEM，会和所有 IEM 赛事对上。
    """
    if min(len(re.findall(r"[a-z0-9]+", str(x or "").lower())) for x in (a, b)) < 2:
        return False
    wa, wb = event_words(a), event_words(b)
    short, long = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return set(short) <= set(long)


def same_event(name: str, event: Dict[str, Any], event_id: Optional[str]) -> bool:
    if event_id and str(event.get("id") or "") == str(event_id):
        return True
    return same_event_name(name, event.get("name"))


def event_teams(matches: List[Dict[str, Any]], event_name: str, event_id: Optional[str]) -> List[str]:
    """HLTV 赛程里属于这个赛事的队伍。"""
    names = []
    for match in matches:
        if not same_event(event_name, match.get("event") or {}, event_id):
            continue
        for key in ("team1", "team2"):
            name = (match.get(key) or {}).get("name")
            if name and name.upper() != "TBD":
                names.append(name)
    return names


def bo3_event_teams(results: List[Dict[str, Any]], event_name: str, at: float) -> List[str]:
    """
    bo3.gg 列表里的候选队伍：赛事名对得上的比赛的队伍；一场都对不上（没填赛事名、填的中文名）时，
    退回开赛时间在 at 前后 NEARBY_SECONDS 内的 S / A 级比赛的队伍（全列出来会混进几十支三线队伍）。
    """
    same = [m for m in results if event_name and same_event_name(event_name, (m.get("tournament") or {}).get("name"))]
    if same:
        return nearby_teams(same, at, window=None)
    return nearby_teams([m for m in results if str(m.get("tier") or "").lower() in MAIN_TIERS], at)


def nearby_teams(results: List[Dict[str, Any]], at: float, window: Optional[float] = NEARBY_SECONDS) -> List[str]:
    """bo3.gg 列表里开赛时间在 at 前后 window 秒内的比赛的队伍（window=None 不限时间）。"""
    names = []
    for match in results:
        begin = bo3.parse_time(match.get("start_date"))
        if window is not None and (begin is None or abs(begin - at) > window):
            continue
        for key in ("team1", "team2"):
            name = (match.get(key) or {}).get("name")
            if name:
                names.append(name)
    return names


def unique(names: List[str]) -> List[str]:
    seen, result = set(), []
    for name in names:
        key = bo3.norm_team(name)
        if key and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def build_identify_prompt(lines: List[Dict[str, Any]], event: str, candidates: List[str], guess: Dict[str, Any],
                          when: str) -> str:
    teams = ", ".join(candidates) or "(unknown)"
    names = ", ".join(guess.get("interviewees") or []) or "unknown"
    return f"""Event: {event or 'unknown'}
Interview time: {when} (Beijing time)
Teams playing at this event or around this time: {teams}
First guess from the live monitor: interviewee {names}, team {guess.get('team') or 'unknown'} (may be wrong).

Transcript of the interview (machine-generated, names may be misspelled):
{prompts.format_live_lines(lines)}

Answer with JSON:
{{"interviewees": [player or coach names, spelled as the official player handle], "team": the interviewees' team, "opponent": the opponent the interview is about or null, "about_match": "just_played" | "between_maps" | "upcoming" | "general" | "unknown", "result": "won" | "lost" | null, "confidence": 0.0-1.0, "note": short reason}}

Rules:
- Prefer team names from the list above, spelled exactly as listed. Use another name only if the transcript clearly says so.
- "just_played": right after a match the team just finished (talks about the match they won or lost). "between_maps": during a series, between maps. "upcoming": before a match (talks about the coming opponent). "general": not about a specific match (feature, desk guest, event in general). "unknown": cannot tell.
- "opponent" is the team they just played (just_played / between_maps) or are about to play (upcoming); null if not mentioned or unclear.
- "result" only for just_played: whether the interviewee's team won that match."""


def parse_identity(answer: Dict[str, Any], candidates: List[str]) -> Dict[str, Any]:
    """整理 LLM 的回答：队名对齐到候选里的写法，about_match 限定取值。"""
    def align(name: Any) -> Optional[str]:
        name = str(name or "").strip()
        if not name or name.lower() in ("null", "none", "unknown"):
            return None
        for candidate in candidates:
            if bo3.same_team(name, [candidate]):
                return candidate
        return name

    names = answer.get("interviewees") or []
    if isinstance(names, str):
        names = [names]
    about = str(answer.get("about_match") or "unknown").strip().lower()
    result = str(answer.get("result") or "").strip().lower()
    team = align(answer.get("team"))
    opponent = align(answer.get("opponent"))
    if opponent and team and bo3.norm_team(opponent) == bo3.norm_team(team):
        opponent = None
    return {
        "interviewees": [str(n).strip() for n in names if str(n).strip()],
        "team": team,
        "opponent": opponent,
        "about_match": about if about in ABOUT_MATCH else "unknown",
        "result": result if result in ("won", "lost") else None,
        "confidence": answer.get("confidence"),
        "note": str(answer.get("note") or ""),
    }


def pick_match(results: List[Dict[str, Any]], team: str, opponent: Optional[str], about: str,
               at: float) -> Optional[Tuple[Dict[str, Any], bool]]:
    """
    在 bo3.gg 的比赛列表里找采访说的那场：返回 (比赛, 受访队是不是 team2)。
    刚打完：已结束、开赛在采访前 MATCH_LOOKBACK 内；图间：进行中。LLM 给了对手就必须对得上；
    没给对手时只有一场候选才算确认（同一天可能打两场）。
    """
    if about not in ("just_played", "between_maps") or not team:
        return None
    wanted = ("finished", "defwin") if about == "just_played" else ("current",)
    found = []
    for match in results:
        if match.get("status") not in wanted:
            continue
        begin = bo3.parse_time(match.get("start_date"))
        if begin is None or not at - MATCH_LOOKBACK <= begin <= at + 600:
            continue
        names1, names2 = bo3.team_names(match.get("team1")), bo3.team_names(match.get("team2"))
        if bo3.same_team(team, names1):
            swapped, other = False, names2
        elif bo3.same_team(team, names2):
            swapped, other = True, names1
        else:
            continue
        if opponent and not bo3.same_team(opponent, other):
            continue
        found.append((begin, match, swapped))
    if not found or (not opponent and len(found) > 1):
        return None
    _, match, swapped = max(found, key=lambda item: item[0])
    return match, swapped


def context_match(normalized: Dict[str, Any], event_fallback: str) -> Dict[str, Any]:
    """bo3.gg 的比赛 → 背景信息里 match 的结构（和 HLTV 抓的一致，match_facts 读这些字段）。"""
    over = normalized.get("status") == "over"
    scores = [normalized["team1"].get("score"), normalized["team2"].get("score")]
    return {
        "event": normalized.get("event") or event_fallback,
        "format": f"Best of {normalized.get('best_of') or 1}",
        "score": scores if over and None not in scores else None,
        "maps": [{k: m.get(k) for k in ("name", "team1_score", "team2_score", "status", "winner")}
                 for m in normalized.get("maps") or []],
        "unix_ms": int(normalized["start_time"] * 1000) if normalized.get("start_time") else None,
        "status": normalized.get("status"),
        "url": normalized.get("url"),
        "source": "bo3",
    }


# ── 抓取背景信息 ──────────────────────────────────────────────────────────────

def _read_lines(task_id: int) -> List[Dict[str, Any]]:
    path = config.task_dir(task_id) / "live_transcript.jsonl"
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    return lines


def _live_state(task_id: int) -> Dict[str, Any]:
    path = config.task_dir(task_id) / "live.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


async def _candidates(client: HLTVClient, bo3_client: bo3.Bo3Client, monitor: Dict[str, Any], at: float,
                      known: List[str], warnings: List[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
    names: List[str] = []
    try:
        names += event_teams(await client.list_matches(), monitor.get("event_name") or monitor.get("title") or "",
                             monitor.get("event_id"))
    except Exception as error:
        logger.warning("读取 HLTV 赛程失败：%s", error)
    results: List[Dict[str, Any]] = []
    try:
        results = (await bo3_client.list_matches("current,upcoming", "start_date")
                   + await bo3_client.list_matches("finished", "-start_date", 100))
        names += bo3_event_teams(results, monitor.get("event_name") or monitor.get("title") or "", at)
    except Exception as error:
        warnings.append(f"读取 bo3.gg 比赛列表失败，无法确认采访对应的比赛：{error}")
    return unique(known + names), results


async def build_for_interview(task: Dict[str, Any], client: HLTVClient,
                              known_teams: List[str]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """返回 (背景信息, 任务字段更新, 警告)。"""
    from . import stages

    settings = db.get_settings()
    warnings: List[str] = []
    state = _live_state(task["id"])
    monitor = db.get_monitor((task["context"].get("monitor") or {}).get("id") or 0) or {}
    event_name = monitor.get("event_name") or (task["context"].get("monitor") or {}).get("event_name") or ""
    origin = (state.get("recording") or {}).get("started_at") or task["created_at"]
    at = origin + float((state.get("interview") or {}).get("start") or 0)
    guess = state.get("detection") or {}
    bo3_client = bo3.Bo3Client(config.proxy_or_none(settings))
    candidates, results = await _candidates(client, bo3_client, monitor, at, known_teams, warnings)

    lines = _read_lines(task["id"])
    identity = {"interviewees": guess.get("interviewees") or [], "team": guess.get("team"), "opponent": None,
                "about_match": "unknown", "result": None}
    if lines:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(at))
        user = build_identify_prompt(lines, event_name, candidates, guess, when)
        stages.write_text(task["id"], "prompt_identify.txt", user)
        try:
            identity = parse_identity(stages._parse_json_object(
                stages._llm_call(settings, "clean", IDENTIFY_SYSTEM, user)), candidates)
        except Exception as error:
            warnings.append(f"识别受访者失败，沿用直播监视的判断：{error}")
    else:
        warnings.append("没有采访的滚动转写，沿用直播监视的判断")

    team, opponent = identity.get("team"), identity.get("opponent")
    match = {"event": event_name} if event_name else {}
    picked = pick_match(results, team, opponent, identity["about_match"], at) if team else None
    if picked:
        raw, swapped = picked
        try:
            normalized = await bo3_client.get_match(raw["slug"], swapped)
            match = context_match(normalized, event_name)
            opponent = normalized["team2"]["name"]  # get_match 按 swapped 把受访队放在 team1
        except Exception as error:
            warnings.append(f"读取 bo3.gg 比赛详情失败：{error}")
    elif identity["about_match"] in ("just_played", "between_maps"):
        warnings.append("没能确认采访对应的是哪场比赛，摘要不写比分；如有需要请在背景信息里补充")

    names = [n for n in (team, opponent) if n]
    built: Dict[str, Any] = {"teams": []}
    if names:
        built, team_warnings = await context_builder.build_from_team_names(client, names)
        warnings += team_warnings
    else:
        warnings.append("没能识别受访者所在战队；名词规范化只用术语表，可在背景信息里补充队伍")

    context = {**task["context"], **built, "match": match, "identified": identity}
    players = {p.lower() for t in built.get("teams") or [] for p in t.get("players") or []}
    missing = [n for n in identity["interviewees"] if players and n.lower() not in players]
    if missing:
        warnings.append(f"受访者 {'、'.join(missing)} 不在识别出的战队阵容里，请核对")
    updates: Dict[str, Any] = {"context": context, "team1": team, "team2": opponent}
    if match.get("unix_ms"):
        updates["match_time"] = match["unix_ms"] / 1000
    who = " / ".join(identity["interviewees"]) or "选手"
    event = match.get("event") or event_name
    kind = {"just_played": "赛后采访", "between_maps": "图间采访", "upcoming": "赛前采访"}.get(
        identity["about_match"], "采访")
    updates["title"] = f"{event} {who}{(' (' + team + ')') if team else ''} {kind}".strip()
    return context, updates, warnings
