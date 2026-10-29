"""
HLTV 页面解析（纯函数，输入 HTML，输出字典）。

按 2026-10 的页面结构编写，样本见 tests/fixtures/hltv/。HLTV 改版后优先看这里。
"""
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Tag

HLTV = "https://www.hltv.org"
FULL_NAME = re.compile(r"^(?P<first>.*?)\s*'(?P<nick>[^']+)'\s*(?P<last>.*)$")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text(el: Optional[Tag]) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _int(text: str) -> Optional[int]:
    match = re.search(r"-?\d+", text or "")
    return int(match.group(0)) if match else None


def _path_id(href: str, kind: str) -> Optional[int]:
    match = re.search(rf"/{kind}/(\d+)", href or "")
    return int(match.group(1)) if match else None


def split_full_name(full: str) -> Dict[str, str]:
    """"Justinas 'jL' Lekavicius" → {nick: jL, real_name: Justinas Lekavicius}"""
    match = FULL_NAME.match(full.strip())
    if not match:
        return {"nick": full.strip(), "real_name": ""}
    real = " ".join(x for x in (match.group("first"), match.group("last")) if x)
    return {"nick": match.group("nick"), "real_name": real}


# ── 比赛列表 /matches ─────────────────────────────────────────────────────────

def parse_matches(html: str) -> List[Dict[str, Any]]:
    soup = _soup(html)
    matches: Dict[int, Dict[str, Any]] = {}
    for wrapper in soup.select(".match-wrapper[data-match-id]"):
        match_id = int(wrapper["data-match-id"])
        link = wrapper.select_one('a[href^="/matches/"]')
        names = [_text(n) for n in wrapper.select(".match-teamname")]
        event = wrapper.select_one(".match-event[data-event-headline]")
        time_el = wrapper.select_one(".match-time[data-unix]")
        stage = wrapper.select_one(".match-stage")
        metas = [_text(m) for m in wrapper.select(".match-meta")]
        live = wrapper.get("live") == "true"
        entry = {
            "id": match_id,
            "url": HLTV + link["href"] if link else f"{HLTV}/matches/{match_id}/-",
            "unix_ms": int(time_el["data-unix"]) if time_el else None,
            "live": live,
            "lan": wrapper.get("lan") == "true",
            "stars": int(wrapper.get("data-stars") or 0),
            "team1": {"id": _int(wrapper.get("team1", "")), "name": names[0] if len(names) > 0 else ""},
            "team2": {"id": _int(wrapper.get("team2", "")), "name": names[1] if len(names) > 1 else ""},
            "event": {
                "id": _int(wrapper.get("data-event-id", "")),
                "name": event["data-event-headline"] if event else "",
            },
            "format": next((m for m in metas if re.fullmatch(r"bo\d", m)), ""),
            "stage": _text(stage),
        }
        # 置顶区和正文会重复出现同一场比赛，保留信息更全的一条
        existing = matches.get(match_id)
        if existing is None or (not existing["event"]["name"] and entry["event"]["name"]):
            matches[match_id] = entry
    return sorted(matches.values(), key=lambda m: (not m["live"], m["unix_ms"] or 0))


# ── 比赛页 /matches/<id>/<slug> ───────────────────────────────────────────────

def _parse_lineup(lineup: Tag) -> Dict[str, Any]:
    head = lineup.select_one(".box-headline")
    team_link = head.select_one('a[href^="/team/"]') if head else None
    head_text = _text(head)
    hltv_rank = re.search(r"HLTV:\s*#(\d+)", head_text)
    vrs_rank = re.search(r"VRS:\s*#(\d+)", head_text)

    # 已结束和未开赛的比赛页结构略有不同：选手 ID 可能在 data-player-id 上，也可能只在 /player/ 链接里
    def player_id(cell: Tag) -> Optional[str]:
        marked = cell.select_one("[data-player-id]")
        if marked:
            return marked["data-player-id"]
        link = cell.select_one('a[href^="/player/"]')
        found = _path_id(link["href"], "player") if link else None
        return str(found) if found else None

    images = {}
    for cell in lineup.select("td.player.player-image"):
        pid = player_id(cell)
        photo = cell.select_one("img.player-photo")
        standin = cell.select_one(".lineup-standin")
        if pid:
            images[pid] = {
                "full": photo.get("title", "") if photo else "",
                "standin_for": (standin.get("title") or "").replace("Substituting for", "").strip() if standin else "",
            }

    players = []
    for cell in lineup.select("td.player:not(.player-image)"):
        pid = player_id(cell)
        nick = _text(cell.select_one(".text-ellipsis")) or _text(cell)
        if not pid or not nick:
            continue
        flag = cell.select_one("img.flag")
        image = images.get(pid, {})
        real_name = split_full_name(image["full"])["real_name"] if image.get("full") else ""
        players.append({
            "id": int(pid),
            "name": nick,
            "real_name": real_name,
            "nationality": flag.get("title", "") if flag else "",
            "standin_for": image.get("standin_for", ""),
        })
    return {
        "id": _path_id(team_link["href"], "team") if team_link else None,
        "name": _text(team_link),
        "hltv_rank": int(hltv_rank.group(1)) if hltv_rank else None,
        "vrs_rank": int(vrs_rank.group(1)) if vrs_rank else None,
        "players": players,
    }


def _parse_past_matches(box: Tag) -> List[Dict[str, Any]]:
    rows = []
    for row in box.select("tr"):
        opponent = row.select_one(".past-matches-teamname a")
        score = row.select_one(".past-matches-score a")
        fmt = row.select_one(".past-matches-map a")
        if not opponent or not score:
            continue
        classes = score.get("class") or []
        rows.append({
            "opponent": _text(opponent),
            "score": _text(score),
            "result": "won" if "won" in classes else "lost" if "lost" in classes else "draw",
            "format": _text(fmt),
            "when": _text(row.select_one(".past-matches-time-ago")),
            "url": HLTV + score["href"] if score.get("href") else "",
        })
    return rows


def parse_match(html: str) -> Dict[str, Any]:
    soup = _soup(html)
    box = soup.select_one(".teamsBox")
    if box is None:
        raise ValueError("不是 HLTV 比赛页（找不到 teamsBox）")

    teams = []
    for side in box.select(".team"):
        link = side.select_one('a[href^="/team/"]')
        score_el = side.select_one(".won, .lost, .tie")
        teams.append({
            "id": _path_id(link["href"], "team") if link else None,
            "name": _text(side.select_one(".teamName")),
            "score": _int(_text(score_el)) if score_el else None,
            "result": (score_el.get("class") or [None])[0] if score_el else None,
        })
    while len(teams) < 2:
        teams.append({"id": None, "name": "", "score": None, "result": None})

    time_el = box.select_one(".time[data-unix]")
    event_link = box.select_one('.event a[href^="/events/"]')
    countdown = _text(box.select_one(".countdown"))

    format_text = _text(soup.select_one(".veto-box .preformatted-text"))
    veto = []
    for box_el in soup.select(".veto-box")[1:]:
        veto.extend(_text(line) for line in box_el.select(".padding > div") if _text(line))

    maps = []
    for holder in soup.select(".mapholder"):
        name = _text(holder.select_one(".mapname"))
        if not name or name == "TBA":
            continue
        scores = [_int(_text(s)) for s in holder.select(".results-team-score")]
        picked = holder.select_one(".results-left.pick, .results-right.pick")
        picked_by = None
        if picked is not None:
            picked_by = teams[0]["name"] if "results-left" in picked.get("class", []) else teams[1]["name"]
        maps.append({
            "name": name,
            "team1_score": scores[0] if len(scores) > 0 else None,
            "team2_score": scores[1] if len(scores) > 1 else None,
            "picked_by": picked_by,
        })

    lineups = [_parse_lineup(l) for l in soup.select(".lineups .lineup")]
    for team, lineup in zip(teams, lineups):
        team.update({k: lineup[k] for k in ("hltv_rank", "vrs_rank", "players")})
        if not team["name"]:
            team["name"] = lineup["name"]

    # 「Team」视图是按战队统计的近期比赛（「Core」按核心阵容）
    past = {}
    grid = soup.select_one(".past-matches-grid[data-past-matches-team]") or soup.select_one(".past-matches-grid")
    if grid:
        for index, team_box in enumerate(grid.select(".past-matches-box")[:2]):
            past[f"team{index + 1}"] = _parse_past_matches(team_box)

    h2h = None
    h2h_box = soup.select_one(".head-to-head")
    if h2h_box:
        numbers = [_int(_text(b)) for b in h2h_box.select(".bold")]
        if len(numbers) >= 3:
            h2h = {"team1_wins": numbers[0], "overtimes": numbers[1], "team2_wins": numbers[2]}

    streams = []
    for stream in soup.select(".streams .stream-box[data-stream-provider]"):
        embed = stream.select_one(".stream-box-embed")
        flag = stream.select_one("img.stream-flag")
        external = stream.select_one(".external-stream a[href]")
        viewers = stream.select_one(".viewers.gtSmartphone-only")
        streams.append({
            "provider": stream["data-stream-provider"],
            "name": _text(embed),
            "country": flag.get("title", "") if flag else "",
            "viewers": _int(_text(viewers)) if viewers else None,
            "url": external["href"] if external else "",
        })

    status = "over" if countdown.lower() == "match over" else "live" if countdown.lower() == "live" else "upcoming"
    return {
        "team1": teams[0],
        "team2": teams[1],
        "unix_ms": int(time_el["data-unix"]) if time_el else None,
        "event": {
            "id": _path_id(event_link["href"], "events") if event_link else None,
            "name": _text(event_link),
        },
        "status": status,
        "format": format_text,
        "veto": veto,
        "maps": maps,
        "past_matches": past,
        "head_to_head": h2h,
        "streams": streams,
    }


# ── 战队页 /team/<id>/<slug> ──────────────────────────────────────────────────

def _parse_map_stats(stats_box: Tag) -> List[Dict[str, Any]]:
    result = []
    for row in stats_box.select(".map-statistics-row"):
        extended = row.find_next_sibling(class_="map-statistics-extended")
        ext_text = _text(extended)
        picks = re.search(r"Picks\s+(\d+)% of (\d+)", ext_text)
        bans = re.search(r"Bans\s+(\d+)% of (\d+)", ext_text)
        record = re.search(r"(\d+) Win (\d+) Draws? (\d+) Loss", ext_text)
        result.append({
            "map": _text(row.select_one(".map-statistics-row-map-mapname")),
            "win_pct": _text(row.select_one(".map-statistics-row-win-percentage")),
            "pickban": _text(row.select_one(".pickban")),
            "pick_pct": int(picks.group(1)) if picks else None,
            "ban_pct": int(bans.group(1)) if bans else None,
            "wins": int(record.group(1)) if record else None,
            "losses": int(record.group(3)) if record else None,
        })
    return result


def parse_team(html: str) -> Dict[str, Any]:
    soup = _soup(html)
    name = _text(soup.select_one(".profile-team-name"))
    if not name:
        raise ValueError("不是 HLTV 战队页（找不到队名）")

    stats: Dict[str, Any] = {}
    coach = None
    for stat in soup.select(".profile-team-stat"):
        text = _text(stat)
        if text.startswith("Coach"):
            coach_link = stat.select_one('a[href^="/coach/"]')
            full = _text(coach_link) or text.replace("Coach", "", 1).strip()
            coach = {**split_full_name(full), "full": full}
        elif text.startswith("Valve ranking"):
            stats["vrs_rank"] = _int(text.split("#", 1)[1]) if "#" in text else None
        elif text.startswith("World ranking"):
            stats["hltv_rank"] = _int(text.split("#", 1)[1]) if "#" in text else None
        elif text.startswith("Average player age"):
            stats["avg_age"] = text.rsplit(" ", 1)[-1]

    roster = []
    for row in soup.select(".players-table tbody tr"):
        nick = _text(row.select_one(".playersBox-playernick .text-ellipsis, .playersBox-playernick"))
        status = _text(row.select_one(".player-status"))
        if nick:
            roster.append({"name": nick, "status": status})
    if not roster:
        roster = [{"name": _text(a), "status": ""} for a in soup.select(".bodyshot-team .text-ellipsis.bold")]

    stats_box = soup.find(id="statsBox")
    recent = []
    if stats_box:
        for item in stats_box.select(".last-5-matches a.highlighted-stat"):
            recent.append({
                "opponent": _text(item.select_one(".highlighted-team-name")),
                "result": _text(item.select_one(".highlighted-match-status")).lower(),
                "url": HLTV + item["href"],
            })

    news = []
    news_box = soup.find(id="newsBox")
    if news_box:
        for article in news_box.select("a.subTab-newsArticle")[:15]:
            date = article.select_one(".subTab-newsDate")
            title = _text(article).replace(_text(date), "", 1).strip() if date else _text(article)
            news.append({
                "title": title,
                "url": HLTV + article["href"],
                "unix_ms": int(date["data-unix"]) if date and date.get("data-unix") else None,
            })

    return {
        "name": name,
        "coach": coach,
        **stats,
        "roster": roster,
        "recent_results": recent,
        "map_stats": _parse_map_stats(stats_box) if stats_box else [],
        "news": news,
    }


# ── 搜索 /search?query= ───────────────────────────────────────────────────────

def parse_search_team(html: str) -> Optional[Dict[str, Any]]:
    soup = _soup(html)
    for link in soup.select('a[href^="/team/"]'):
        team_id = _path_id(link["href"], "team")
        if team_id:
            return {"id": team_id, "name": _text(link), "url": HLTV + link["href"]}
    return None


def match_id_from_url(url: str) -> Optional[int]:
    return _path_id(url, "matches")
