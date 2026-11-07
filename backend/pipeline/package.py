"""
投稿信息：推荐标题、摘要、封面候选。

初稿发到飞书之后在后台生成（不耽误出初稿），只存在任务目录里，在线精校页上看、改、一键复制；
飞书里在定稿时才发：Word（摘要作为导语放在正文最前面）、选用的标题、封面候选图。

- 标题和摘要：比赛事实（赛事、赛段、比分、胜负）由代码从背景信息和直播比分算好，LLM 只负责
  从中文稿里提炼观点、套用范例格式。标题范例来自设置，定稿时选用的标题自动加进去。
- 封面候选：直播任务用采访期间截的高清画面（stills/），视频链接或上传的视频按采访时间截几帧；
  再从 HLTV 下载受访选手的现场照片和战队合照（img-cdn 在 Cloudflare 后面，要用浏览器下载）。

任务目录下的文件：
  package.json   {status: running|done, started_at, generated_at, facts, interviewee, interviewee_team, host,
                  event_zh, titles, title（选用的标题）, summary, covers: [{file, source, label}], warnings}
  covers/        封面候选图片
  stills/        直播采访期间截的高清画面（live.py）；视频任务截的帧也放在这里
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config, db
from ..integrations import feishu
from ..integrations.task_cards import cards, review_url
from ..sources import hltv_parse
from . import prompts
from .names import tighten_cjk_spacing

logger = logging.getLogger(__name__)

PACKAGE_FILE = "package.json"
COVER_NAME = re.compile(r"^[a-z]+_\d+\.(jpg|png)$")
MAX_TITLE_EXAMPLES = 30
MAX_SUMMARY_EXAMPLES = 6
# 封面候选：采访画面、选手照片、战队合照各取几张
INTERVIEW_COVERS = 4
PLAYER_COVERS = 4
TEAM_COVERS = 2
VIDEO_STILLS = 8
# 比赛结束后多久以内生成的摘要可以写「就在刚刚」
JUST_ENDED_SECONDS = 3 * 3600
STILL_TIMEOUT = 90

_lock = threading.Lock()


# ── package.json ─────────────────────────────────────────────────────────────

def load(task_id: int) -> Dict[str, Any]:
    path = config.task_dir(task_id) / PACKAGE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def save(task_id: int, package: Dict[str, Any]) -> None:
    path = config.task_dir(task_id) / PACKAGE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(package, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def update(task_id: int, title: Optional[str] = None, summary: Optional[str] = None) -> Dict[str, Any]:
    """精校页上改选标题、改摘要。"""
    with _lock:
        package = load(task_id)
        if title is not None:
            package["title"] = " ".join(title.split())
        if summary is not None:
            package["summary"] = " ".join(summary.split())
        save(task_id, package)
    return package


SUMMARY_NOTE = "（听译，可能存在出入）"
SUMMARY_NOTE_RE = re.compile(r"[（(]\s*听译[，,]\s*可能存在出入\s*[）)]")


def with_summary_note(summary: str) -> str:
    """摘要末尾带上「听译」备注：放在句号之后。不管 LLM 把备注写在句号前还是后，都统一挪到句号之后，只留一份。"""
    summary = summary.strip()
    if not summary:
        return summary
    body = SUMMARY_NOTE_RE.sub("", summary).strip().rstrip("。")
    return body + "。" + SUMMARY_NOTE


def chosen_title(package: Dict[str, Any]) -> str:
    return package.get("title") or (package.get("titles") or [""])[0]


# ── 比赛事实（纯函数） ────────────────────────────────────────────────────────

def match_facts(task: Dict[str, Any], live_state: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    context = task.get("context") or {}
    match = context.get("match") or {}
    teams = [t["name"] for t in context.get("teams") or [] if t.get("name")]
    if len(teams) != 2:
        teams = [t for t in (task.get("team1"), task.get("team2")) if t]
    fmt = match.get("format") or ""
    best_of = re.search(r"Best of (\d)", fmt)
    stage = " / ".join(part.strip() for part in fmt.split("*")[1:] if part.strip())

    snapshot = live_state.get("snapshot") or {}
    score = None
    if snapshot.get("status") == "over" and len(snapshot.get("map_wins") or []) == 2:
        score = list(snapshot["map_wins"])
    elif len(match.get("score") or []) == 2 and None not in match["score"]:
        score = list(match["score"])
    winner = loser = None
    if score and len(teams) == 2 and score[0] != score[1]:
        first_won = score[0] > score[1]
        winner, loser = (teams[0], teams[1]) if first_won else (teams[1], teams[0])
        score = f"{max(score)}-{min(score)}"
    else:
        score = None

    maps = []
    for m in snapshot.get("maps") or match.get("maps") or []:
        if m.get("team1_score") is not None and m.get("team2_score") is not None and m.get("status") != "live":
            maps.append(f"{m['name']} {m['team1_score']}-{m['team2_score']}")
    when = (match.get("unix_ms") or 0) / 1000 or task.get("match_time")
    now = now or time.time()
    identified = context.get("identified")
    # 直播间截下来的采访：类型由事后识别给出；旧的预约比赛任务都是赛后采访
    kind = (identified or {}).get("about_match") or "just_played"
    if identified is None:
        over_at = live_state.get("match_over_at")
    else:
        interview = live_state.get("interview") or {}
        origin = (live_state.get("recording") or {}).get("started_at")
        over_at = origin + float(interview.get("end") or 0) if origin and kind == "just_played" and score else None
    guess = (live_state.get("detection") or {}).get("interviewee") or next(
        iter((identified or {}).get("interviewees") or []), None)
    return {
        "event": match.get("event") or "",
        "stage": stage,
        "best_of": int(best_of.group(1)) if best_of else None,
        "teams": teams,
        "winner": winner,
        "loser": loser,
        "score": score,
        "maps": maps if len(teams) == 2 else [],
        "date": datetime.fromtimestamp(when, tz=prompts.BEIJING).strftime("%Y-%m-%d") if when else "",
        "just_ended": bool(over_at and now - over_at < JUST_ENDED_SECONDS),
        "interviewee_guess": guess,
        "interview_kind": kind,
        "interviewee_team": (identified or {}).get("team"),
        "opponent": (identified or {}).get("opponent"),
    }


def event_terms(event: str, terms: Dict[str, str]) -> List[Tuple[str, str]]:
    """术语表里出现在赛事名中的条目，长的在前（「PGL Masters Bucharest 2026」优先于「PGL」）。"""
    lowered = event.lower()
    found = [(en, zh) for en, zh in terms.items() if en and en.lower() in lowered]
    return sorted(found, key=lambda item: -len(item[0]))


def host_for(event: str, lines: List[str]) -> Tuple[Optional[str], List[str]]:
    """设置里的主持人：返回（这项赛事的主持人, 所有提到的主持人名字）。"""
    host, names = None, []
    for line in lines:
        keyword, sep, name = (line or "").partition("=")
        name = (name if sep else keyword).strip()
        if not name:
            continue
        names.append(name)
        if sep and host is None and keyword.strip() and keyword.strip().lower() in event.lower():
            host = name
    return host, list(dict.fromkeys(names))


def _clean_title(text: Any) -> str:
    text = re.sub(r"^\s*(\d+[.、)]|[-*•])\s*", "", str(text or ""))
    return tighten_cjk_spacing(" ".join(text.strip().strip("\"'“”「」《》").split()))


def apply_answer(answer: Dict[str, Any], facts: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """整理 LLM 的回答，检查摘要里的比分和队名。返回（字段, 警告）。"""
    titles = [t for t in (_clean_title(t) for t in answer.get("titles") or []) if t]
    summary = with_summary_note(tighten_cjk_spacing(" ".join(str(answer.get("summary") or "").split())))
    warnings = []
    if not titles:
        warnings.append("LLM 没有给出标题")
    if not summary:
        warnings.append("LLM 没有给出摘要")
    elif facts.get("winner"):
        missing = [name for name in (facts["winner"], facts["loser"]) if name.lower() not in summary.lower()]
        if missing:
            warnings.append(f"摘要里没有 {'、'.join(missing)}，请核对")
        a, b = facts["score"].split("-")
        if not re.search(rf"{a}\s*[-:：比]\s*{b}", summary):
            warnings.append(f"摘要里的比分和实际（{facts['score']}）对不上，请核对")
    fields = {
        "titles": list(dict.fromkeys(titles)),
        "summary": summary,
        "interviewee": answer.get("interviewee") or facts.get("interviewee_guess"),
        "interviewee_team": answer.get("interviewee_team"),
        "host": answer.get("host"),
        "event_zh": answer.get("event_zh"),
    }
    return fields, warnings


def pick_evenly(items: List[Any], count: int) -> List[Any]:
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)]


def remember_examples(title: str, summary: str) -> None:
    """定稿时把选用的标题和摘要加到范例的最前面。"""
    settings = db.get_settings()
    cfg = settings["package"]
    changed = False
    for key, value, limit in (("title_examples", title, MAX_TITLE_EXAMPLES),
                              ("summary_examples", summary, MAX_SUMMARY_EXAMPLES)):
        value = " ".join((value or "").split())
        if not value or (cfg.get(key) or [None])[0] == value:
            continue
        cfg[key] = [value] + [v for v in _lines(cfg.get(key)) if v != value][:limit - 1]
        changed = True
    if changed:
        db.save_settings(settings)


# ── 标题和摘要 ───────────────────────────────────────────────────────────────

def _lines(values: Any) -> List[str]:
    """设置页按行编辑的列表：去掉空行。"""
    return [v.strip() for v in values or [] if isinstance(v, str) and v.strip()]


def write_text_parts(task: Dict[str, Any], facts: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    from . import stages

    settings = db.get_settings()
    cfg = settings["package"]
    _, terms = stages._glossary()
    host, host_names = host_for(facts["event"], cfg.get("hosts") or [])
    user = prompts.build_package_prompt(
        facts, stages.read_text(task["id"], "draft_zh.txt"), _lines(cfg.get("title_examples")),
        _lines(cfg.get("summary_examples")), event_terms(facts["event"], terms), host, host_names,
    )
    stages.write_text(task["id"], "prompt_package.txt", user)
    answer = stages._parse_json_object(stages._llm_call(settings, "write", prompts.PACKAGE_SYSTEM, user))
    fields, warnings = apply_answer(answer, facts)
    event_zh = fields.get("event_zh")
    if event_zh and facts["event"] and not event_terms(facts["event"], terms):
        warnings.append(f"赛事中文名「{event_zh}」是 LLM 翻译的，可以把 {facts['event']} 加进术语表统一写法")
    return fields, warnings


# ── 封面 ─────────────────────────────────────────────────────────────────────

def _live_stills(task_id: int, state: Dict[str, Any]) -> List[Tuple[Path, float]]:
    """直播采访期间截的高清画面，只要采访区间内的。"""
    from .recorder import frame_time

    still_dir = config.task_dir(task_id) / "stills"
    recording, interview = state.get("recording"), state.get("interview")
    if not recording or not still_dir.exists():
        return []
    stills = [(path, frame_time(path) - recording["started_at"]) for path in sorted(still_dir.glob("f_*.jpg"))]
    if interview:
        stills = [(path, t) for path, t in stills if interview["start"] - 15 <= t <= interview["end"] + 15]
    return stills


def _video_source(task: Dict[str, Any], settings: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    """视频任务截帧用的输入：(ffmpeg 输入, 额外参数)。没有画面可截时返回 (None, [])。"""
    if task["source_type"] == "url" and task.get("source_url"):
        import yt_dlp

        options = {"quiet": True, "no_warnings": True, "noplaylist": True,
                   "format": "bestvideo[height<=1080]/best[height<=1080]/best"}
        proxy = config.proxy_or_none(settings)
        if proxy:
            options["proxy"] = proxy
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(task["source_url"], download=False)
        chosen = (info.get("requested_formats") or [info])[0]
        if not chosen.get("url") or chosen.get("vcodec") == "none":
            return None, []
        headers = "".join(f"{k}: {v}\r\n" for k, v in (chosen.get("http_headers") or {}).items())
        return chosen["url"], ["-headers", headers] if headers else []
    source = task.get("source_file")
    if task["source_type"] == "upload" and source and Path(source).exists():
        return source, []
    return None, []


def _video_stills(task: Dict[str, Any], settings: Dict[str, Any]) -> List[Tuple[Path, float]]:
    """视频链接或上传的视频：在采访时间范围内均匀截几帧。"""
    source, extra = _video_source(task, settings)
    transcript = config.task_dir(task["id"]) / "transcript.json"
    if source is None or not transcript.exists():
        return []
    duration = json.loads(transcript.read_text(encoding="utf-8")).get("duration") or 0
    if duration <= 0:
        return []
    still_dir = config.task_dir(task["id"]) / "stills"
    still_dir.mkdir(exist_ok=True)
    offset = task.get("clip_start") or 0
    env = dict(os.environ)
    proxy = config.proxy_or_none(settings)
    if proxy:
        env.update(http_proxy=proxy, https_proxy=proxy)
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    stills = []
    for index in range(VIDEO_STILLS):
        t = duration * (index + 0.5) / VIDEO_STILLS
        output = still_dir / f"v_{index + 1:02d}.jpg"
        command = [config.resolve_ffmpeg(settings), "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", f"{offset + t:.2f}", *extra, "-i", source, "-frames:v", "1", "-q:v", "2", str(output)]
        try:
            result = subprocess.run(command, capture_output=True, env=env, timeout=STILL_TIMEOUT,
                                    creationflags=creationflags)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and output.exists():
            stills.append((output, t))
    return stills


def _interview_shots(stills: List[Tuple[Path, float]]) -> List[Tuple[Path, float]]:
    """用直播镜头识别的模型挑出采访镜头；识别失败就不筛（去掉比赛画面这类明显不行的）。"""
    from . import live, stages

    settings = db.get_settings()
    kept = []
    for begin in range(0, len(stills), live.SHEET_SIZE):
        batch = stills[begin:begin + live.SHEET_SIZE]
        try:
            answer = stages._parse_json_object(stages._llm_call(
                settings, live.detect_step(settings), prompts.SHOT_SYSTEM, prompts.build_shot_prompt(len(batch)),
                images=[live.make_sheet(batch, max_width=480)]))
            labels = live.parse_shot_labels(answer, len(batch))
        except Exception as error:
            logger.warning("封面候选的镜头识别失败，不筛选：%s", error)
            return stills
        kept += [still for still, label in zip(batch, labels) if label == "interview"]
    return kept


def _image_suffix(data: bytes) -> str:
    return ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"


def _find_player(task: Dict[str, Any], name: str) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """
    受访选手的 HLTV ID 和所在战队（背景信息里的战队条目）。背景信息里没有 ID（旧任务）时从两队的战队页阵容里找：
    比赛刚结束时 HLTV 比赛页没有阵容栏（实测 IEM Beijing 2026 Legacy vs The MongolZ）。
    """
    from . import stages

    teams = [t for t in (task.get("context") or {}).get("teams") or [] if t.get("name")]
    lowered = name.lower()
    for team in teams:
        for player in team.get("player_details") or []:
            if (player.get("name") or "").lower() == lowered and player.get("id"):
                return int(player["id"]), team
    for team in teams:
        if not team.get("id"):
            continue
        page = hltv_parse.parse_team(stages.browser.fetch_sync(f"{hltv_parse.HLTV}/team/{team['id']}/team"))
        for player in page["roster"]:
            if player["name"].lower() == lowered and player.get("id"):
                return player["id"], team
    return None, None


def _hltv_pictures(task: Dict[str, Any], interviewee: Optional[str], team_name: Optional[str],
                   warnings: List[str]) -> List[Tuple[str, str, str]]:
    """HLTV 上的照片：[(地址, 来源 player|team, 说明)]。"""
    from . import stages

    pictures: List[Tuple[str, str, str]] = []
    team = None
    if interviewee:
        player_id, team = _find_player(task, interviewee)
        if player_id:
            player = hltv_parse.parse_player(stages.browser.fetch_sync(f"{hltv_parse.HLTV}/player/{player_id}/player"))
            pictures += [(p["url"], "player", f"HLTV · {interviewee}") for p in player["pictures"][:PLAYER_COVERS]]
            if len(pictures) < 2 and player.get("bodyshot"):
                pictures.append((player["bodyshot"], "player", f"HLTV · {interviewee} 定妆照"))
        else:
            warnings.append(f"HLTV 上没有找到选手 {interviewee}，封面里没有选手照片")
    teams = (task.get("context") or {}).get("teams") or []
    if team is None or not team.get("id"):
        team = next((t for t in teams if team_name and t.get("name", "").lower() == team_name.lower()), None)
    if team and team.get("id"):
        page = hltv_parse.parse_team(stages.browser.fetch_sync(f"{hltv_parse.HLTV}/team/{team['id']}/team"))
        label = f"HLTV · {team.get('name') or page['name']} 合照"
        # 选手照片里常有全队的照片，和战队合照重复（同一张图尺寸参数不同，比较去掉参数的地址）
        seen = {url.split("?")[0] for url, _, _ in pictures}
        pictures += [(p["url"], "team", label) for p in page["photos"]
                     if p["url"].split("?")[0] not in seen][:TEAM_COVERS]
    return pictures


def collect_covers(task: Dict[str, Any], live_state: Dict[str, Any], interviewee: Optional[str],
                   team_name: Optional[str], warnings: List[str]) -> List[Dict[str, Any]]:
    from . import stages

    settings = db.get_settings()
    cover_dir = config.task_dir(task["id"]) / "covers"
    cover_dir.mkdir(exist_ok=True)
    for old in cover_dir.iterdir():
        old.unlink()
    covers: List[Dict[str, Any]] = []

    try:
        if task["source_type"] == "live":
            stills = _interview_shots(_live_stills(task["id"], live_state))
        else:
            stills = _video_stills(task, settings)
        for index, (path, t) in enumerate(pick_evenly(stills, INTERVIEW_COVERS), 1):
            name = f"interview_{index}.jpg"
            (cover_dir / name).write_bytes(path.read_bytes())
            covers.append({"file": name, "source": "interview", "label": f"采访画面 {prompts.format_timestamp(t)}"})
        if not stills:
            warnings.append("没有可用的采访画面")
    except Exception as error:
        logger.exception("任务 %s 截取采访画面失败", task["id"])
        warnings.append(f"截取采访画面失败：{error}")

    try:
        pictures = _hltv_pictures(task, interviewee, team_name, warnings)
    except Exception as error:
        logger.exception("任务 %s 获取 HLTV 照片失败", task["id"])
        warnings.append(f"获取 HLTV 照片失败：{error}")
        pictures = []
    counts: Dict[str, int] = {}
    for url, source, label in pictures:
        try:
            data = stages.browser.download_sync(url)
        except Exception as error:
            warnings.append(f"下载 HLTV 照片失败：{error}")
            continue
        counts[source] = counts.get(source, 0) + 1
        name = f"{source}_{counts[source]}{_image_suffix(data)}"
        (cover_dir / name).write_bytes(data)
        covers.append({"file": name, "source": source, "label": label})
    return covers


# ── 生成 ─────────────────────────────────────────────────────────────────────

def generate(task_id: int) -> Dict[str, Any]:
    from . import live

    task = db.get_task(task_id)
    if task is None:
        raise RuntimeError("任务不存在")
    state = live.load_state(task_id) if task["source_type"] == "live" else {}
    facts = match_facts(task, state)
    package: Dict[str, Any] = {"status": "running", "started_at": time.time(), "facts": facts,
                               "titles": [], "summary": "", "covers": [], "warnings": []}
    with _lock:
        save(task_id, package)

    try:
        fields, warnings = write_text_parts(task, facts)
        package.update(fields)
        package["warnings"] += warnings
    except Exception as error:
        logger.exception("任务 %s 生成标题和摘要失败", task_id)
        package["warnings"].append(f"生成标题和摘要失败：{error}")
    with _lock:
        save(task_id, package)  # 标题摘要先给精校页看，封面还要一会儿

    interviewee = package.get("interviewee") or facts.get("interviewee_guess")
    package["covers"] = collect_covers(task, state, interviewee,
                                       package.get("interviewee_team") or facts.get("winner"), package["warnings"])
    package.update(status="done", generated_at=time.time())
    with _lock:
        # 生成期间有人在精校页改了标题、摘要：保留人改的
        edited = load(task_id)
        for key in ("title", "summary"):
            if edited.get(key) and edited[key] != package.get(key):
                package[key] = edited[key]
        save(task_id, package)
    return package


def send_covers(task_id: int, package: Dict[str, Any]) -> None:
    """定稿时把封面候选图发到卡片的话题里（标题和 Word 由定稿流程发）。"""
    cover_dir = config.task_dir(task_id) / "covers"
    for cover in package.get("covers") or []:
        try:
            cards.send_image(task_id, cover_dir / cover["file"])
        except Exception as error:
            logger.warning("任务 %s 封面 %s 发送失败：%s", task_id, cover["file"], error)


class PackageService:
    """每个任务同时只跑一个生成；在后台线程里跑，不占流水线。"""

    def __init__(self) -> None:
        self._running: set = set()
        self._guard = threading.Lock()

    def running(self, task_id: int) -> bool:
        return task_id in self._running

    def start(self, task_id: int) -> bool:
        with self._guard:
            if task_id in self._running:
                return False
            self._running.add(task_id)
        threading.Thread(target=self._run, args=(task_id,), name=f"package-{task_id}", daemon=True).start()
        return True

    def _run(self, task_id: int) -> None:
        try:
            package = generate(task_id)
            covers = len(package.get("covers") or [])
            db.add_event(task_id, f"投稿信息已生成：{len(package.get('titles') or [])} 个候选标题、{covers} 张封面候选"
                         + (f"（{'；'.join(package['warnings'])}）" if package.get("warnings") else ""),
                         level="warning" if package.get("warnings") else "info")
        except Exception as error:
            logger.exception("任务 %s 生成投稿信息失败", task_id)
            db.add_event(task_id, f"生成投稿信息失败：{error}", level="error")
            with _lock:
                package = load(task_id)
                package.update(status="done", generated_at=time.time())
                package.setdefault("warnings", []).append(f"生成失败：{error}")
                save(task_id, package)
        finally:
            with self._guard:
                self._running.discard(task_id)


service = PackageService()
