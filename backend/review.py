"""
在线精校（手机上也能用）：经内网穿透给飞书群成员访问。

- 中文稿 draft_zh.txt 按段落编辑，每段单独保存；保存时带上改之前的内容，
  别人已经改过这一段就返回冲突，不会互相覆盖。
- 英文清洗稿只作参考；按转写的时间戳给每段配上音频位置，点一下从这段开始听。
- 画面用直播录制时每隔几秒存的那些帧，跟着音频进度切换，用来分辨谁在说话。
- 任何一个人都可以定稿：重新生成 Word，标记完成，发到任务卡片的话题里。

精校记录存在任务目录的 review.json：
  draft_mtime   上次由精校页写入 draft_zh.txt 的时间；文件被别处改过就重置下面的对应关系
  en_index      第 i 段中文对应第几段英文（删段后保持对应）
  meta          段落内容摘要 → {by, at}，谁最后改的这一段
  finalized     {by, at}
"""
import hashlib
import json
import re
import subprocess
import threading
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import config, db, web_auth
from .integrations import task_cards
from .integrations.task_cards import cards
from .pipeline import live, stages
from .pipeline.recorder import frame_time
from .runner import State

DRAFT = "draft_zh.txt"
ENGLISH = "clean_en.txt"
REVIEW_FILE = "review.json"
AUDIO_FILE = "review_audio.m4a"
FRAME_NAME = re.compile(r"^f_\d{8}_\d{6}\.jpg$")
PREFIX = re.compile(r"^\s*[QqAa]\s*[:：]\s*")
WORD = re.compile(r"[a-z0-9']+")
BUSY_STATES = (State.QUEUED, State.PROCESSING)

_locks: Dict[int, threading.Lock] = defaultdict(threading.Lock)
_audio_lock = threading.Lock()
_times_cache: Dict[int, Tuple[tuple, List[Optional[float]]]] = {}


class Conflict(Exception):
    def __init__(self, paragraphs: List[str]):
        super().__init__("conflict")
        self.paragraphs = paragraphs


# ── 段落（纯函数） ────────────────────────────────────────────────────────────

def split_paragraphs(text: str) -> List[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def join_paragraphs(paragraphs: List[str]) -> str:
    return "\n\n".join(paragraphs) + "\n"


def fingerprint(paragraph: str) -> str:
    return hashlib.sha1(paragraph.encode("utf-8")).hexdigest()[:16]


def apply_edit(paragraphs: List[str], en_index: List[int], index: int, base: str,
               text: str) -> Tuple[List[str], List[int], str]:
    """
    把第 index 段从 base 改成 text（空 = 删除这一段），返回新的段落、英文对应关系和规整后的内容。
    第 index 段已经不是 base 时，按内容找到那一段（别人删段导致位置变化）；找不到就是冲突。
    """
    if not (0 <= index < len(paragraphs) and paragraphs[index] == base):
        found = [i for i, p in enumerate(paragraphs) if p == base]
        if len(found) != 1:
            raise Conflict(paragraphs)
        index = found[0]
    # 一段就是一行：粘贴进来的换行直接接上
    new = "".join(line.strip() for line in text.splitlines())
    paragraphs, en_index = list(paragraphs), list(en_index)
    if new:
        paragraphs[index] = new
    else:
        del paragraphs[index]
        del en_index[index]
    return paragraphs, en_index, new


def paragraph_times(english: List[str], transcript: Dict[str, Any]) -> List[Optional[float]]:
    """
    每段英文在音频里开始的时间：清洗稿和转写逐词对齐，取每段第一个对上的词的时间。
    对不上的段落沿用前一段的时间。
    """
    words: List[Tuple[str, float]] = []
    for seg in transcript.get("segments") or []:
        if seg.get("words"):
            for word in seg["words"]:
                words += [(token, word["start"]) for token in WORD.findall(word["word"].lower())]
        else:
            tokens = WORD.findall((seg.get("text") or "").lower())
            span = (seg["end"] - seg["start"]) / max(len(tokens), 1)
            words += [(token, seg["start"] + span * i) for i, token in enumerate(tokens)]
    tokens, owner = [], []
    for index, paragraph in enumerate(english):
        for token in WORD.findall(PREFIX.sub("", paragraph, count=1).lower()):
            tokens.append(token)
            owner.append(index)
    first: List[Optional[float]] = [None] * len(english)
    matcher = SequenceMatcher(None, tokens, [w for w, _ in words], autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            if first[owner[a + k]] is None:
                first[owner[a + k]] = words[b + k][1]
    previous: Optional[float] = None
    for index, value in enumerate(first):
        if value is None or (previous is not None and value < previous):
            first[index] = previous
        previous = first[index]
    return first


# ── 任务目录里的文件 ──────────────────────────────────────────────────────────

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def _load(task_id: int) -> Tuple[List[str], Dict[str, Any]]:
    """中文段落和精校记录；draft_zh.txt 被别处改过时重置英文对应关系。"""
    path = config.task_dir(task_id) / DRAFT
    if not path.exists():
        raise HTTPException(404, "还没有中文初稿")
    paragraphs = split_paragraphs(path.read_text(encoding="utf-8"))
    review = _read_json(config.task_dir(task_id) / REVIEW_FILE)
    if review.get("draft_mtime") != path.stat().st_mtime or len(review.get("en_index") or []) != len(paragraphs):
        review["en_index"] = list(range(len(paragraphs)))
    review.setdefault("meta", {})
    return paragraphs, review


def _save(task_id: int, paragraphs: List[str], review: Dict[str, Any]) -> None:
    directory = config.task_dir(task_id)
    path = directory / DRAFT
    path.write_text(join_paragraphs(paragraphs), encoding="utf-8")
    review["draft_mtime"] = path.stat().st_mtime
    live_ids = {fingerprint(p) for p in paragraphs}
    review["meta"] = {k: v for k, v in review["meta"].items() if k in live_ids}
    (directory / REVIEW_FILE).write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")


def _english_times(task_id: int, english: List[str]) -> List[Optional[float]]:
    directory = config.task_dir(task_id)
    transcript = directory / "transcript.json"
    if not transcript.exists():
        return [None] * len(english)
    key = (transcript.stat().st_mtime, (directory / ENGLISH).stat().st_mtime)
    cached = _times_cache.get(task_id)
    if cached and cached[0] == key:
        return cached[1]
    times = paragraph_times(english, json.loads(transcript.read_text(encoding="utf-8")))
    _times_cache[task_id] = (key, times)
    return times


def frames(task_id: int, duration: Optional[float]) -> List[Dict[str, Any]]:
    """直播时存下的画面，换算成精校音频（截取出来的采访）里的时间。"""
    state = live.load_state(task_id)
    recording, interview = state.get("recording"), state.get("interview")
    frame_dir = config.task_dir(task_id) / "frames"
    if not recording or not interview or not frame_dir.exists():
        return []
    clip_start = interview.get("clip_start")
    if clip_start is None:
        clip_start = max(0.0, interview["start"] - float(db.get_settings()["live"]["padding_seconds"]))
    origin = recording["started_at"] + clip_start
    result = []
    for path in sorted(frame_dir.glob("f_*.jpg")):
        t = frame_time(path) - origin
        if -5 <= t <= (duration if duration is not None else interview["end"] - clip_start) + 5:
            result.append({"t": round(t, 1), "name": path.name})
    return result


def payload(task_id: int) -> Dict[str, Any]:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    directory = config.task_dir(task_id)
    paragraphs, review = _load(task_id)
    english_path = directory / ENGLISH
    english = split_paragraphs(english_path.read_text(encoding="utf-8")) if english_path.exists() else []
    times = _english_times(task_id, english) if english else []
    transcript = _read_json(directory / "transcript.json")
    duration = transcript.get("duration")
    report = _read_json(directory / "names_report.json")
    items = []
    for index, paragraph in enumerate(paragraphs):
        en = review["en_index"][index]
        items.append({
            "zh": paragraph,
            "en": english[en] if en < len(english) else "",
            "start": times[en] if en < len(times) else None,
            "edited": review["meta"].get(fingerprint(paragraph)),
        })
    return {
        "task": {"id": task_id, "title": task["title"], "state": task["state"],
                 "subtitle": task_cards._subtitle(task)},
        "editable": task["state"] not in BUSY_STATES,
        "paragraphs": items,
        "has_audio": (directory / "audio.wav").exists(),
        "duration": duration,
        "frames": frames(task_id, duration),
        "suspects": report.get("suspects") or [],
        "unknown": report.get("unknown") or [],
        "finalized": review.get("finalized"),
    }


def edit(task_id: int, index: int, base: str, text: str, user: Dict[str, Any]) -> Dict[str, Any]:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["state"] in BUSY_STATES:
        raise HTTPException(409, "任务正在处理中，稍后再改")
    with _locks[task_id]:
        paragraphs, review = _load(task_id)
        paragraphs, review["en_index"], new = apply_edit(paragraphs, review["en_index"], index, base, text)
        if new:
            review["meta"][fingerprint(new)] = {"by": user["name"], "at": time.time()}
        _save(task_id, paragraphs, review)
    return payload(task_id)


def review_audio(task_id: int) -> Path:
    """精校用的音频：16kHz wav 转成小得多的 m4a，手机上加载快；audio.wav 更新后重新转。"""
    directory = config.task_dir(task_id)
    source, target = directory / "audio.wav", directory / AUDIO_FILE
    if not source.exists():
        raise HTTPException(404, "还没有音频")
    with _audio_lock:
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            partial = directory / f"{AUDIO_FILE}.part.m4a"
            command = [config.resolve_ffmpeg(db.get_settings()), "-y", "-hide_banner", "-loglevel", "error",
                       "-i", str(source), "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", str(partial)]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0:
                raise HTTPException(500, f"音频转换失败：{result.stderr.strip()[-200:]}")
            partial.replace(target)
    return target


def finalize(task_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
    """定稿：重新生成 Word，标记完成，发到任务卡片的话题里。"""
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["state"] in BUSY_STATES:
        raise HTTPException(409, "任务正在处理中")
    with _locks[task_id]:
        stages.stage_export(task_id)
        paragraphs, review = _load(task_id)
        review["finalized"] = {"by": user["name"], "at": time.time()}
        _save(task_id, paragraphs, review)
    db.update_task(task_id, state=State.DONE, error=None)
    db.add_event(task_id, f"{user['name']} 在线精校后定稿")
    sent = False
    if db.get_settings()["feishu"].get("enabled"):
        try:
            cards.alert(task_id, "定稿", f"{user['name']} 已定稿，Word 见下方")
            cards.send_file(task_id, stages.find_docx(task_id))
            sent = True
        except Exception as error:
            db.add_event(task_id, f"定稿的 Word 发送到飞书失败：{error}", level="warning")
    cards.wake()
    return {**payload(task_id), "sent": sent}


# ── 接口 ──────────────────────────────────────────────────────────────────────

router = APIRouter()


async def current_user(request: Request) -> Dict[str, Any]:
    """本机访问不需要登录；经穿透进来的必须飞书登录，并且在配置的群里。"""
    session = web_auth.read_session(request.cookies.get(web_auth.SESSION_COOKIE))
    if not web_auth.is_public(request.headers):
        return session or {"open_id": None, "name": "本机"}
    if session is None:
        raise HTTPException(401, "请先用飞书登录")
    feishu_settings = db.get_settings()["feishu"]
    try:
        member = await run_in_threadpool(web_auth.members.contains, feishu_settings, session["open_id"])
    except Exception as error:
        raise HTTPException(503, f"无法确认你是否在飞书群里：{error}")
    if not member:
        raise HTTPException(403, "你不在飞书群里，不能访问")
    return session


class ParagraphEdit(BaseModel):
    base: str
    text: str


@router.get("/api/review/me")
async def me(user: Dict[str, Any] = Depends(current_user)):
    return {"name": user["name"], "avatar": user.get("avatar")}


@router.get("/api/review/{task_id}")
async def get_review(task_id: int, user: Dict[str, Any] = Depends(current_user)):
    return {**await run_in_threadpool(payload, task_id), "user": {"name": user["name"]}}


@router.put("/api/review/{task_id}/paragraphs/{index}")
async def put_paragraph(task_id: int, index: int, body: ParagraphEdit, user: Dict[str, Any] = Depends(current_user)):
    try:
        return await run_in_threadpool(edit, task_id, index, body.base, body.text, user)
    except Conflict:
        raise HTTPException(409, "这一段刚被别人改过")


@router.post("/api/review/{task_id}/finalize")
async def post_finalize(task_id: int, user: Dict[str, Any] = Depends(current_user)):
    return await run_in_threadpool(finalize, task_id, user)


@router.get("/api/review/{task_id}/audio")
async def get_audio(task_id: int, _: Dict[str, Any] = Depends(current_user)):
    path = await run_in_threadpool(review_audio, task_id)
    return FileResponse(path, media_type="audio/mp4")


@router.get("/api/review/{task_id}/frames/{name}")
async def get_frame(task_id: int, name: str, _: Dict[str, Any] = Depends(current_user)):
    path = config.task_dir(task_id) / "frames" / name
    if not FRAME_NAME.match(name) or not path.is_file():
        raise HTTPException(404, "画面不存在")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


# ── 飞书登录 ──────────────────────────────────────────────────────────────────

def _redirect_uri(request: Request) -> str:
    base = (db.get_settings()["feishu"].get("public_url") or "").strip().rstrip("/")
    if not base:
        base = f"https://{request.headers.get('x-forwarded-host') or request.headers.get('host')}"
    return f"{base}/auth/feishu/callback"


def _message_page(title: str, text: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><div style='font-family:sans-serif;padding:32px 20px;text-align:center'>"
        f"<h3>{title}</h3><p style='color:#666'>{text}</p></div>", status_code=status)


@router.get("/auth/feishu/login")
async def login(request: Request, next: str = "/review/"):
    feishu_settings = db.get_settings()["feishu"]
    if not feishu_settings.get("app_id"):
        return _message_page("还没有配置飞书", "请先在本机的设置页填写飞书应用", 503)
    state = web_auth.new_state()
    response = RedirectResponse(web_auth.authorize_url(feishu_settings["app_id"], _redirect_uri(request), state))
    response.set_cookie(web_auth.STATE_COOKIE, f"{state}|{web_auth.safe_next(next)}", max_age=600,
                        httponly=True, secure=True, samesite="lax")
    return response


@router.get("/auth/feishu/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    saved_state, _, next_path = (request.cookies.get(web_auth.STATE_COOKIE) or "").partition("|")
    if not code or not state or state != saved_state:
        return _message_page("登录失败", "登录已过期或被拒绝，请回到飞书重新打开链接", 400)
    feishu_settings = db.get_settings()["feishu"]
    try:
        user = await run_in_threadpool(web_auth.fetch_user, feishu_settings, code, _redirect_uri(request))
        member = await run_in_threadpool(web_auth.members.contains, feishu_settings, user["open_id"])
    except Exception as error:
        return _message_page("登录失败", str(error), 502)
    if not member:
        return _message_page("没有权限", f"{user['name']}，你不在协作的飞书群里", 403)
    response = RedirectResponse(web_auth.safe_next(next_path))
    response.set_cookie(web_auth.SESSION_COOKIE, web_auth.make_session(user), max_age=web_auth.SESSION_DAYS * 86400,
                        httponly=True, secure=True, samesite="lax")
    response.delete_cookie(web_auth.STATE_COOKIE)
    return response
