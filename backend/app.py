"""
本地服务入口：REST API + 前端静态资源 + 后处理流水线。

启动：python -m backend   （默认 http://127.0.0.1:8000）
"""
import asyncio
import concurrent.futures
import io
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, llm, review, web_auth
from .integrations import feishu, schedule_card, task_cards
from .integrations.feishu_ws import FeishuEventClient
from .integrations.monitor_cards import PLATFORM_LABELS, monitor_cards, replace_stream
from .integrations.task_cards import cards
from .pipeline import identify, live, monitor, package, stages
from .runner import STAGES, STAGE_KEYS, PipelineRunner, State
from .sources import hltv_parse, stream_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

runner = PipelineRunner()


async def _notify_failure(task_id: int, message: str) -> None:
    await run_in_threadpool(cards.alert, task_id, "处理失败", message)


def _configure_browser(settings: Dict[str, Any]) -> None:
    stages.browser.configure(config.proxy_or_none(settings), bool(settings["hltv"].get("headless")))


def _on_hltv_challenge(url: str) -> None:
    feishu.notify(
        db.get_settings(),
        f"【HLTV 需要人工验证】请在本机弹出的 Chrome 窗口中完成 Cloudflare 验证\n{url}",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    _configure_browser(db.get_settings())
    stages.browser.on_challenge = _on_hltv_challenge
    stages.register(runner)
    stages.on_delivered = package.service.start
    runner.on_failure(_notify_failure)
    runner.start()

    loop = asyncio.get_running_loop()

    def enqueue_threadsafe(task_id: int, stage: str) -> None:
        def enqueue() -> None:
            try:
                runner.enqueue(task_id, stage)
            except ValueError as error:
                db.add_event(task_id, f"无法开始后处理：{error}", level="error")
        loop.call_soon_threadsafe(enqueue)

    monitor.manager.bind(enqueue_threadsafe)
    monitor.manager.start()
    global _main_loop
    _main_loop = loop
    cards.start()
    monitor_cards.start()
    card_callbacks.start(db.get_settings()["feishu"])
    logger.info("服务已启动，数据目录：%s", config.DATA_DIR)
    yield
    cards.stop()
    monitor_cards.stop()
    await runner.stop()
    await run_in_threadpool(monitor.manager.stop_all)
    await stages.browser.close()


app = FastAPI(title="Auto-Translate-to-News", lifespan=lifespan)


@app.middleware("http")
async def public_boundary(request: Request, call_next):
    """经内网穿透进来的请求只能访问精校页相关的路径（见 web_auth.py）。"""
    if web_auth.is_public(request.headers) and not web_auth.public_path_allowed(request.url.path):
        return Response("Not Found", status_code=404)
    return await call_next(request)


def _require_task(task_id: int) -> Dict[str, Any]:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


# ── 基础 ──────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "current_task": runner.current_task_id}


@app.get("/api/meta")
async def meta():
    return {"stages": [{"key": k, "label": label} for k, label in STAGES]}


# ── 设置 ──────────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return db.get_settings()


@app.put("/api/settings")
async def put_settings(settings: Dict[str, Any]):
    previous = db.get_settings()
    saved = db.save_settings(settings)
    _configure_browser(saved)
    card_callbacks.start(saved["feishu"])
    if previous["feishu"].get("public_url") != saved["feishu"].get("public_url"):
        db.invalidate_cards()  # 已发出的卡片补上 / 更新在线精校按钮
    cards.wake()
    return saved


class ProviderTest(BaseModel):
    provider: Dict[str, Any]


@app.post("/api/llm/test")
async def test_llm(body: ProviderTest):
    settings = db.get_settings()
    try:
        reply = await run_in_threadpool(
            llm.complete, body.provider, "You are a helpful assistant.", "Reply with exactly: OK",
            proxy=config.proxy_or_none(settings),
        )
    except Exception as error:
        raise HTTPException(400, f"调用失败：{error}")
    return {"reply": reply[:200]}


@app.post("/api/feishu/test")
async def test_feishu(body: Dict[str, Any]):
    """发一张示例卡片并在它下面回复话题；卡片上的按钮可以用来检查长连接回调是否连通。"""
    def send() -> None:
        client = feishu.FeishuClient(body)
        message_id = client.send_card(task_cards.demo_card())
        client.reply_text(message_id, "【测试】需要处理的提醒会像这样回复在卡片下面")
    try:
        await run_in_threadpool(send)
    except Exception as error:
        raise HTTPException(400, str(error))
    card_callbacks.start({**body, "enabled": True})
    return {"ok": True}


# ── 术语表 ────────────────────────────────────────────────────────────────────

class GlossaryEntry(BaseModel):
    id: Optional[int] = None
    kind: str
    term: str
    target: str
    note: Optional[str] = None


@app.get("/api/glossary")
async def list_glossary():
    return db.list_glossary()


@app.post("/api/glossary")
async def save_glossary(entry: GlossaryEntry):
    if entry.kind not in ("alias", "term"):
        raise HTTPException(400, "kind 只能是 alias 或 term")
    if not entry.term.strip() or not entry.target.strip():
        raise HTTPException(400, "词条和目标都不能为空")
    return db.upsert_glossary(entry.model_dump())


@app.delete("/api/glossary/{entry_id}")
async def delete_glossary(entry_id: int):
    db.delete_glossary(entry_id)
    return {"ok": True}


# ── 任务 ──────────────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    title: str
    source_type: str = "url"  # url | file
    match_url: Optional[str] = None
    source_url: Optional[str] = None
    team1: Optional[str] = None
    team2: Optional[str] = None
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    notes: Optional[str] = None
    start: bool = True


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    source_url: Optional[str] = None
    team1: Optional[str] = None
    team2: Optional[str] = None
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    notes: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


class RunRequest(BaseModel):
    from_stage: str = STAGE_KEYS[0]
    to_stage: Optional[str] = None


class TextUpdate(BaseModel):
    text: str


def _task_files(task_id: int) -> List[Dict[str, Any]]:
    directory = config.task_dir(task_id)
    return [
        {"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
        for p in sorted(directory.iterdir()) if p.is_file()
    ]


@app.get("/api/tasks")
async def list_tasks():
    tasks = db.list_tasks()
    for task in tasks:
        task["progress"] = stages.progress.get(task["id"])
    return tasks


@app.post("/api/tasks")
async def create_task(body: TaskCreate):
    if body.source_type not in ("url", "file"):
        raise HTTPException(400, "source_type 只能是 url 或 file")
    if body.source_type == "url" and not (body.source_url or "").strip():
        raise HTTPException(400, "请填写视频链接")
    fields = body.model_dump(exclude={"start", "match_url"})
    if body.match_url:
        match_id = hltv_parse.match_id_from_url(body.match_url)
        if not match_id:
            raise HTTPException(400, "无法从链接中识别 HLTV 比赛 ID")
        fields["match_hltv_id"] = str(match_id)
    task = db.create_task(state=State.DRAFT, **fields)
    db.add_event(task["id"], "任务已创建")
    cards.track(task["id"])
    # 文件任务等上传完成后再开始
    if body.source_type == "url" and body.start:
        runner.enqueue(task["id"])
    return db.get_task(task["id"])


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: int):
    task = _require_task(task_id)
    task["events"] = db.list_events(task_id)
    task["files"] = _task_files(task_id)
    task["progress"] = stages.progress.get(task_id)
    texts = {}
    for name in stages.TEXT_ARTIFACTS:
        path = config.task_dir(task_id) / name
        texts[name] = path.read_text(encoding="utf-8") if path.exists() else None
    task["texts"] = texts
    report = config.task_dir(task_id) / "names_report.json"
    task["names_report"] = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
    transcript = config.task_dir(task_id) / "transcript.json"
    task["transcript"] = json.loads(transcript.read_text(encoding="utf-8")) if transcript.exists() else None
    docx = stages.find_docx(task_id)
    task["docx"] = docx.name if docx else None
    return task


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: int, body: TaskUpdate):
    _require_task(task_id)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return db.get_task(task_id)
    return db.update_task(task_id, **fields)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    task = _require_task(task_id)
    if task["state"] in (State.QUEUED, State.PROCESSING):
        raise HTTPException(409, "任务正在处理中，请先取消")
    db.delete_task(task_id)
    shutil.rmtree(config.TASKS_DIR / str(task_id), ignore_errors=True)
    stages.progress.pop(task_id, None)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/upload")
async def upload_source(task_id: int, file: UploadFile = File(...)):
    task = _require_task(task_id)
    if task["state"] in (State.QUEUED, State.PROCESSING):
        raise HTTPException(409, "任务正在处理中")
    suffix = Path(file.filename or "").suffix or ".bin"
    directory = config.task_dir(task_id)
    for old in directory.glob("source.*"):
        old.unlink()
    target = directory / f"source{suffix}"
    with open(target, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    db.update_task(task_id, source_type="file", source_file=str(target))
    db.add_event(task_id, f"已上传文件 {file.filename}")
    runner.enqueue(task_id)
    return db.get_task(task_id)


@app.post("/api/tasks/{task_id}/run")
async def run_task(task_id: int, body: RunRequest):
    _require_task(task_id)
    try:
        runner.enqueue(task_id, body.from_stage, body.to_stage)
    except ValueError as error:
        raise HTTPException(400, str(error))
    return db.get_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    task = _require_task(task_id)
    if task["state"] not in (State.QUEUED, State.PROCESSING):
        raise HTTPException(400, "任务不在处理中")
    db.update_task(task_id, cancel_requested=True)
    db.add_event(task_id, "已请求取消，当前步骤结束后停止", level="warning")
    return db.get_task(task_id)


@app.post("/api/tasks/{task_id}/done")
async def mark_done(task_id: int):
    _require_task(task_id)
    db.update_task(task_id, state=State.DONE)
    db.add_event(task_id, "已标记为完成")
    return db.get_task(task_id)


@app.put("/api/tasks/{task_id}/texts/{name}")
async def save_text(task_id: int, name: str, body: TextUpdate):
    _require_task(task_id)
    if name not in stages.TEXT_ARTIFACTS:
        raise HTTPException(400, "不可编辑的文件")
    stages.write_text(task_id, name, body.text)
    db.add_event(task_id, f"人工修改了 {name}")
    return {"ok": True}


@app.get("/api/tasks/{task_id}/files/{name}")
async def download_file(task_id: int, name: str):
    _require_task(task_id)
    path = (config.task_dir(task_id) / name).resolve()
    if path.parent != config.task_dir(task_id).resolve() or not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=path.name)


# ── 直播间截下来的采访 ──────────────────────────────────────────────────────

@app.get("/api/tasks/{task_id}/live")
async def live_status(task_id: int):
    """采访来自哪个直播间、什么时间（live.json），以及截取范围内的直播转写。"""
    task = _require_task(task_id)
    if task["source_type"] != "live":
        raise HTTPException(400, "不是直播任务")
    return {**live.load_state(task_id), "transcript": live.read_transcript(task_id)}


# ── 直播间监视 ────────────────────────────────────────────────────────────────

class MonitorStream(BaseModel):
    url: str
    label: Optional[str] = ""
    proxy: Optional[bool] = None  # 不填按域名判断：国内平台直连
    language: Optional[str] = None  # 转写语言，"" 为自动识别；不填按平台默认


class MonitorBody(BaseModel):
    title: Optional[str] = None
    event_id: Optional[str] = None
    event_name: Optional[str] = None
    streams: Optional[List[MonitorStream]] = None
    enabled: Optional[bool] = None


def _require_monitor(monitor_id: int) -> Dict[str, Any]:
    found = db.get_monitor(monitor_id)
    if found is None:
        raise HTTPException(404, "直播间监视不存在")
    return found


def _monitor_fields(body: MonitorBody) -> Dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if "streams" in fields:
        try:
            fields["streams"] = monitor.normalize_streams(fields["streams"] or [])
        except ValueError as error:
            raise HTTPException(400, str(error))
    return fields


@app.get("/api/monitors")
async def list_monitors():
    return [monitor.manager.status(m) for m in db.list_monitors()]


@app.post("/api/monitors")
async def create_monitor(body: MonitorBody):
    fields = _monitor_fields(body)
    if not fields.get("streams"):
        raise HTTPException(400, "请至少填写一条直播链接")
    fields["title"] = (fields.get("title") or fields.get("event_name") or "").strip() or "直播间"
    created = db.create_monitor(**fields)
    monitor.manager.wake()
    monitor_cards.wake()
    return monitor.manager.status(created)


@app.get("/api/monitors/events")
async def monitor_events():
    """HLTV 赛程里的赛事（新建直播间监视时选）。HLTV 打不开时返回空列表，可以手填赛事名。"""
    try:
        matches = await stages.hltv.list_matches()
    except Exception as error:
        return {"events": [], "error": f"读取 HLTV 赛程失败：{error}"}
    events: Dict[str, Dict[str, Any]] = {}
    for match in matches:
        event = match.get("event") or {}
        if not event.get("name"):
            continue
        entry = events.setdefault(str(event.get("id") or event["name"]), {
            "id": event.get("id"), "name": event["name"], "matches": 0, "stars": 0, "first_ms": None})
        entry["matches"] += 1
        entry["stars"] = max(entry["stars"], match.get("stars") or 0)
        if match.get("unix_ms") and (entry["first_ms"] is None or match["unix_ms"] < entry["first_ms"]):
            entry["first_ms"] = match["unix_ms"]
    ordered = sorted(events.values(), key=lambda e: (-e["stars"], e["first_ms"] or 0))
    return {"events": ordered, "error": None}


class MonitorCapture(BaseModel):
    start: float
    end: float
    interviewees: List[str] = []


def _monitor_session(monitor_id: int, index: int) -> "monitor.StreamSession":
    try:
        return monitor.manager.session(_require_monitor(monitor_id), index)
    except IndexError as error:
        raise HTTPException(404, str(error))


@app.get("/api/monitors/{monitor_id}/streams/{index}/audio")
async def monitor_audio(monitor_id: int, index: int, start: float, end: float):
    """试听某条链接录音的一段（相对这一轮录制开始的秒数，最长 10 分钟）。"""
    session = _monitor_session(monitor_id, index)
    buffer = io.BytesIO()
    try:
        await run_in_threadpool(session.audio, start, min(end, start + 600), buffer)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error))
    return Response(buffer.getvalue(), media_type="audio/wav")


@app.post("/api/monitors/{monitor_id}/streams/{index}/capture")
async def monitor_capture(monitor_id: int, index: int, body: MonitorCapture):
    """手动标注一段采访：截下来生成任务。"""
    session = _monitor_session(monitor_id, index)
    capture = {"start": body.start, "end": body.end, "source": "manual",
               "interviewees": [n.strip() for n in body.interviewees if n.strip()], "team": None}
    try:
        return await run_in_threadpool(session.capture, capture)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error))


@app.get("/api/monitors/{monitor_id}")
async def get_monitor(monitor_id: int, tail: int = 200):
    return monitor.manager.status(_require_monitor(monitor_id), transcript_tail=tail)


@app.put("/api/monitors/{monitor_id}")
async def update_monitor(monitor_id: int, body: MonitorBody):
    _require_monitor(monitor_id)
    fields = _monitor_fields(body)
    updated = db.update_monitor(monitor_id, **fields) if fields else db.get_monitor(monitor_id)
    monitor.manager.wake()
    monitor_cards.wake()
    return monitor.manager.status(updated)


@app.delete("/api/monitors/{monitor_id}")
async def delete_monitor(monitor_id: int):
    _require_monitor(monitor_id)
    db.update_monitor(monitor_id, enabled=False, windows=[])
    await run_in_threadpool(monitor.manager.reconcile)
    db.delete_monitor(monitor_id)
    return {"ok": True}


# ── HLTV 比赛 ─────────────────────────────────────────────────────────────────

@app.get("/api/matches")
async def list_matches(refresh: bool = False, days: int = 5):
    try:
        matches = await stages.hltv.list_matches(refresh=refresh)
    except Exception as error:
        raise HTTPException(502, f"抓取 HLTV 比赛列表失败：{error}")
    horizon = (time.time() + days * 86400) * 1000
    monitors = db.list_monitors()
    result = []
    for match in matches:
        if match["unix_ms"] and match["unix_ms"] > horizon:
            continue
        found = monitor.find_reservation(monitors, match["id"])
        result.append({**match, "monitor_id": found[0]["id"] if found else None,
                       "monitor_title": found[0]["title"] if found else None,
                       "window": {"start": found[1]["start"], "end": found[1]["end"]} if found else None})
    return {"fetched_at": stages.hltv.cached_at("matches"), "matches": result}


@app.post("/api/matches/{match_id}/reserve")
async def reserve_match(match_id: int):
    return await _reserve(match_id)


@app.delete("/api/matches/{match_id}/reserve")
async def unreserve_match(match_id: int):
    return _unreserve(match_id)


async def _reserve(match_id: int, reserved_by: Optional[str] = None) -> Dict[str, Any]:
    """
    预约比赛 = 给这场比赛所属赛事的直播间加一段时间窗（开赛前 15 分钟到开赛后 5 小时），时间窗里自动监视。
    赛事还没有直播间就新建一个，链接先从 HLTV 比赛页找官方流（后台进行），在飞书直播间卡片下提醒确认。
    预约不生成任务，任务只来自截到的采访。reserved_by：在飞书里预约的人（open_id）。
    """
    monitors = db.list_monitors()
    existing = monitor.find_reservation(monitors, match_id)
    if existing:
        raise HTTPException(409, f"这场比赛已经预约过（直播间「{existing[0]['title']}」）")
    try:
        listed = next((m for m in (await stages.hltv.list_matches()) if m["id"] == match_id), None)
    except Exception as error:
        raise HTTPException(502, f"抓取 HLTV 比赛列表失败：{error}")
    if listed is None:
        raise HTTPException(404, "HLTV 赛程里找不到这场比赛")
    event = listed.get("event") or {}
    teams = " vs ".join(t["name"] or "TBD" for t in (listed["team1"], listed["team2"]))
    now = time.time()
    found = monitor.find_event_monitor(monitors, event.get("id"), event.get("name") or "")
    created = found is None
    if created:
        found = db.create_monitor(title=event.get("name") or teams, event_id=str(event["id"]) if event.get("id") else None,
                                  event_name=event.get("name") or None, streams=[])
        db.add_monitor_event(found["id"], f"预约 {teams} 时新建直播间")
    window = monitor.reservation_window(match_id, listed["unix_ms"] / 1000 if listed.get("unix_ms") else None, teams,
                                        db.get_settings()["monitor"], now, reserved_by)
    # 顺便清掉一天前就结束的时间窗
    windows = [w for w in found["windows"] if float(w.get("end") or 0) > now - 86400] + [window]
    updated = db.update_monitor(found["id"], windows=windows)
    span = f"{_clock(window['start'], True)}–{_clock(window['end'])}"
    db.add_monitor_event(found["id"], f"{'在飞书' if reserved_by else ''}预约 {teams}：{span} 自动监视")
    monitor.manager.wake()
    monitor_cards.wake()
    if not updated["streams"]:
        asyncio.create_task(_fill_official_stream(found["id"], match_id))
    return {"title": teams, "monitor": updated, "window": window, "created": created,
            "needs_stream": not updated["streams"]}


def _unreserve(match_id: int) -> Dict[str, Any]:
    found = monitor.find_reservation(db.list_monitors(), match_id)
    if found is None:
        raise HTTPException(404, "这场比赛没有预约")
    owner, window = found
    updated = db.update_monitor(owner["id"], windows=monitor.drop_reservation(owner["windows"], match_id))
    db.add_monitor_event(owner["id"], f"取消预约 {window.get('title') or match_id}")
    monitor.manager.wake()
    monitor_cards.wake()
    return {"title": window.get("title") or str(match_id), "monitor": updated}


def _clock(ts: Optional[float], with_date: bool = False) -> str:
    return time.strftime("%m-%d %H:%M" if with_date else "%H:%M", time.localtime(ts)) if ts else "?"


async def _fill_official_stream(monitor_id: int, match_id: int) -> None:
    """新建的直播间还没有链接：从 HLTV 比赛页找官方流填上，在飞书直播间卡片下提醒人确认（国内可换成 B 站）。"""
    stream = None
    try:
        match = await stages.hltv.get_match(match_id)
        stream = monitor.official_stream(match.get("streams") or [])
    except Exception as error:
        logger.warning("读取 HLTV 比赛 %s 的直播流失败：%s", match_id, error)
    found = db.get_monitor(monitor_id)
    if found is None or found["streams"]:
        return
    if stream:
        db.update_monitor(monitor_id, streams=monitor.normalize_streams([{"url": stream["url"]}]))
        db.add_monitor_event(monitor_id, f"从 HLTV 填上官方直播流：{stream.get('name') or ''} {stream['url']}")
        text = (f"【请确认直播链接】已从 HLTV 填上官方直播流 {stream['url']}（走代理）。"
                f"国内有 B 站等直播的话，请在卡片上换成国内链接，省代理流量")
    else:
        db.add_monitor_event(monitor_id, "HLTV 比赛页上没找到官方直播流", "warning")
        text = "【请填直播链接】HLTV 上没找到这场比赛的官方直播流，请在卡片上填一个（国内可用 B 站）"
    monitor.manager.wake()
    await run_in_threadpool(monitor_cards.alert, monitor_id, text)


# ── 飞书卡片按钮 ──────────────────────────────────────────────────────────────

CARD_ACTIONS = {"retry": "从失败处重试", "done": "标记完成"}
# 飞书要求 3 秒内响应卡片回调，操作没做完就先回"已提交"
CARD_ACTION_TIMEOUT = 2.5
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def _aliases() -> Dict[str, str]:
    return {e["term"]: e["target"] for e in db.list_glossary() if e["kind"] == "alias"}


async def _schedule_card(view: Dict[str, Any], refresh: bool = False) -> Dict[str, Any]:
    data = await list_matches(refresh=refresh)
    return schedule_card.build_schedule_card(data["matches"], view, _aliases(), data["fetched_at"])


async def _update_schedule(message_id: str, view: Dict[str, Any], refresh: bool = False) -> None:
    card = await _schedule_card(view, refresh)
    await run_in_threadpool(feishu.client_for(db.get_settings()).update_card, message_id, card)


async def _card_action(value: Dict[str, Any], form: Dict[str, Any], operator: str, message_id: str) -> Optional[str]:
    """执行卡片上的按钮，复用对应的接口；失败时抛出 HTTPException。"""
    action = str(value.get("action") or "")
    view = value.get("view") or {}
    if action == "schedule":
        await _update_schedule(message_id, view, bool(value.get("refresh")))
        return None
    if action == "reserve":
        result = await _reserve(int(value["match_id"]), reserved_by=operator or None)
        await _update_schedule(message_id, view)
        return _reserved_reply(result)
    if action == "unreserve":
        result = _unreserve(int(value["match_id"]))
        await _update_schedule(message_id, view)
        return f"已取消预约 {result['title']}"
    if action.startswith("monitor_"):
        return await _monitor_card_action(action, int(value["monitor_id"]), form)
    label = CARD_ACTIONS.get(action)
    if label is None:
        raise HTTPException(400, f"未知操作：{action}")
    task_id = int(value.get("task_id"))
    if task_id == task_cards.DEMO_TASK_ID:
        return f"卡片按钮已连通（{label}）"
    task = _require_task(task_id)
    if action == "retry":
        if task["state"] != State.FAILED:
            raise HTTPException(409, "任务不是失败状态")
        await run_task(task_id, RunRequest(from_stage=task["stage"] or STAGE_KEYS[0]))
        message = "已重新开始处理"
    else:
        await mark_done(task_id)
        message = "已标记完成"
    db.add_event(task_id, f"飞书卡片操作：{label}")
    cards.wake()
    return message


async def _monitor_card_action(action: str, monitor_id: int, form: Dict[str, Any]) -> str:
    """直播间卡片上的按钮和换链接表单。"""
    found = _require_monitor(monitor_id)
    if action == "monitor_on":
        db.update_monitor(monitor_id, enabled=True)
        db.add_monitor_event(monitor_id, "在飞书卡片上开始监视")
        message = "已开始监视"
    elif action == "monitor_off":
        db.update_monitor(monitor_id, enabled=False, windows=[])
        db.add_monitor_event(monitor_id, "在飞书卡片上停止监视")
        message = "已停止监视"
    elif action == "monitor_stream":
        url = str(form.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "请填写直播链接")
        _set_monitor_stream(found, url, form.get("slot"), form.get("proxy"))
        db.add_monitor_event(monitor_id, f"在飞书卡片上换了直播链接：{url}")
        message = "已保存直播链接"
    else:
        raise HTTPException(400, f"未知操作：{action}")
    monitor.manager.wake()
    monitor_cards.wake()
    return message


def _set_monitor_stream(found: Dict[str, Any], url: str, slot: Optional[str] = None,
                        proxy: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
    try:
        streams = monitor.normalize_streams(replace_stream(found["streams"], url, slot, proxy))
    except ValueError as error:
        raise HTTPException(400, str(error))
    return db.update_monitor(found["id"], streams=streams, **fields)


def _watch_target(url: str, name: str) -> Optional[Dict[str, Any]]:
    """「监视 <链接> [赛事名]」加到哪个直播间：同名赛事 → 已经有这条链接的 → 最近开着的 → 没有就新建。"""
    monitors = db.list_monitors()
    if name:
        lowered = name.lower()
        return next((m for m in monitors if lowered in ((m.get("event_name") or "").lower(), m["title"].lower())
                     or identify.same_event_name(name, m.get("event_name") or "")), None)
    key = monitor.stream_key(url)
    found = next((m for m in monitors if any(monitor.stream_key(s["url"]) == key for s in m["streams"])), None)
    if found:
        return found
    now = time.time()
    on = sorted((m for m in monitors if monitor.is_on(m, now)), key=lambda m: m["updated_at"], reverse=True)
    return on[0] if on else None


def _on_card_action(value: Dict[str, Any], form: Dict[str, Any], operator: str, message_id: str) -> Optional[str]:
    """长连接线程里调用：把操作交给服务的事件循环执行。"""
    if _main_loop is None:
        raise RuntimeError("服务还没启动完成")
    future = asyncio.run_coroutine_threadsafe(_card_action(value, form, operator, message_id), _main_loop)
    try:
        return future.result(timeout=CARD_ACTION_TIMEOUT)
    except concurrent.futures.TimeoutError:
        return "已提交，卡片稍后更新"


# ── 飞书机器人命令（群里 @机器人 或私聊） ─────────────────────────────────────

def _reserved_reply(result: Dict[str, Any]) -> str:
    window = result["window"]
    text = (f"已预约 {result['title']}：{_clock(window['start'], True)}–{_clock(window['end'])} "
            f"自动监视直播间「{result['monitor']['title']}」，截到的采访会各自发进度卡片")
    if result["needs_stream"]:
        text += "；这个赛事还没有直播链接，正在从 HLTV 找官方流，结果发在直播间卡片下"
    return text


def _command_allowed(event: Dict[str, Any], feishu_settings: Dict[str, Any]) -> bool:
    """只响应配置的那个群和私聊：预约出来的进度卡片都发到配置的群里。"""
    if event.get("chat_type") == "p2p":
        return True
    return feishu_settings.get("receive_id_type") == "chat_id" and event.get("chat_id") == feishu_settings.get("receive_id")


async def _handle_command(event: Dict[str, Any]) -> None:
    settings = db.get_settings()
    if not feishu.enabled(settings) or not _command_allowed(event, settings["feishu"]):
        return
    client = feishu.client_for(settings)
    message_id = event["message_id"]

    async def reply(text: str) -> None:
        await run_in_threadpool(client.reply_text, message_id, text, False)

    command, args = schedule_card.parse_command(event.get("text") or "")
    try:
        if command == "help":
            await reply(schedule_card.HELP_TEXT)
        elif command == "unknown":
            await reply(f"没看懂「{args['text'][:50]}」\n\n{schedule_card.HELP_TEXT}")
        elif command == "monitors":
            await reply(await _send_monitor_cards())
        elif command == "watch_usage":
            await reply("用法：监视 <直播链接> [赛事名]，例如：监视 https://live.bilibili.com/35 IEM Beijing 2026")
        elif command == "watch":
            await reply(await _watch(args["url"], args.get("name") or ""))
        elif command == "reserve_url":
            match_id = hltv_parse.match_id_from_url(args["url"])
            if not match_id:
                await reply("无法从链接中识别 HLTV 比赛 ID")
                return
            await reply(_reserved_reply(await _reserve(match_id, reserved_by=event.get("sender") or None)))
        else:
            card = await _schedule_card(args)
            await run_in_threadpool(client.reply_card, message_id, card)
    except HTTPException as error:
        await reply(f"操作失败：{error.detail}")
    except Exception as error:
        logger.exception("处理飞书命令出错")
        await reply(f"操作失败：{error}")


MONITOR_CARD_LIMIT = 5


async def _send_monitor_cards() -> str:
    """「直播间」：开着的直播间（再补几个最近的）各发一张新卡片到群里。"""
    now = time.time()
    monitors = db.list_monitors()
    if not monitors:
        return "还没有直播间。发「监视 <直播链接> [赛事名]」新建一个，或者在网页的「直播间」页新建"
    picked = sorted(monitors, key=lambda m: (not monitor.is_on(m, now), -m["updated_at"]))[:MONITOR_CARD_LIMIT]
    for found in picked:
        await run_in_threadpool(monitor_cards.sync, found["id"], True)
    on = sum(1 for m in monitors if monitor.is_on(m, now))
    return f"已把 {len(picked)} 个直播间的卡片发到群里（{on} 个正在监视）"


async def _watch(url: str, name: str) -> str:
    """「监视 <链接> [赛事名]」：给直播间加/换链接并开始监视。"""
    found = _watch_target(url, name)
    if found is None:
        title = name or f"{PLATFORM_LABELS.get(stream_status.platform(url), '直播')} 直播间"
        found = db.create_monitor(title=title, event_name=name or None, streams=[])
        db.add_monitor_event(found["id"], "在飞书里新建直播间")
    updated = _set_monitor_stream(found, url, enabled=True)
    db.add_monitor_event(found["id"], f"在飞书里加了直播链接：{url}")
    # 同一个直播间换了赛事（B 站主舞台每个赛事都用）：之前开着、也在看这条链接的直播间停掉，免得录两份
    stopped = []
    for other in db.list_monitors():
        if other["id"] != found["id"] and monitor.is_on(other, time.time()) and any(
                monitor.stream_key(s["url"]) == monitor.stream_key(url) for s in other["streams"]):
            db.update_monitor(other["id"], enabled=False, windows=[])
            db.add_monitor_event(other["id"], f"这条链接改由「{updated['title']}」监视，这里停止监视：{url}")
            stopped.append(other["title"])
    monitor.manager.wake()
    monitor_cards.wake()
    await run_in_threadpool(monitor_cards.sync, found["id"], True)
    hint = "" if updated.get("event_name") else "；没填赛事名，识别受访者时找不到候选战队，可以在直播间页补上"
    moved = f"；「{'」「'.join(stopped)}」已停止监视" if stopped else ""
    return f"已开始监视「{updated['title']}」，共 {len(updated['streams'])} 条链接{moved}{hint}"


def _on_bot_message(event: Dict[str, Any]) -> None:
    """长连接线程里调用：交给服务的事件循环处理，立即返回。"""
    if _main_loop is not None:
        asyncio.run_coroutine_threadsafe(_handle_command(event), _main_loop)


card_callbacks = FeishuEventClient(_on_card_action, _on_bot_message)


app.include_router(review.router)


# ── 前端静态资源（npm run build 后生成 dist/） ─────────────────────────────────

if config.WEB_DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=config.WEB_DIST_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        candidate = config.WEB_DIST_DIR / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(config.WEB_DIST_DIR / "index.html")
