"""
本地服务入口：REST API + 前端静态资源 + 后处理流水线。

启动：python -m backend   （默认 http://127.0.0.1:8000）
"""
import asyncio
import io
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, llm
from .integrations import feishu
from .pipeline import live, match_sources, stages
from .pipeline.watcher import MatchWatcher
from .runner import STAGES, STAGE_KEYS, PipelineRunner, State
from .sources import bo3, hltv_parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

runner = PipelineRunner()
watcher = MatchWatcher(stages.hltv, live.manager, runner)
LIVE_TASK_STATES = (State.RESERVED, State.WATCHING, State.RECORDING, State.INTERVIEW_DETECTING, State.NO_INTERVIEW)


async def _notify_failure(task_id: int, message: str) -> None:
    task = db.get_task(task_id)
    await run_in_threadpool(feishu.notify, db.get_settings(), f"【处理失败】{task['title']}\n{message}")


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

    live.manager.bind(enqueue_threadsafe)
    await run_in_threadpool(live.manager.resume_all)
    watcher.start()
    logger.info("服务已启动，数据目录：%s", config.DATA_DIR)
    yield
    await watcher.stop()
    await runner.stop()
    await run_in_threadpool(live.manager.stop_all)
    await stages.browser.close()


app = FastAPI(title="Auto-Translate-to-News", lifespan=lifespan)


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
    saved = db.save_settings(settings)
    _configure_browser(saved)
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
    try:
        await run_in_threadpool(lambda: feishu.FeishuClient(body).send_text("Auto-Translate-to-News 测试消息"))
    except Exception as error:
        raise HTTPException(400, str(error))
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
    await run_in_threadpool(live.manager.stop, task_id)
    db.delete_task(task_id)
    shutil.rmtree(config.TASKS_DIR / str(task_id), ignore_errors=True)
    stages.progress.pop(task_id, None)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/upload")
async def upload_source(task_id: int, file: UploadFile = File(...)):
    task = _require_task(task_id)
    if task["state"] in (State.QUEUED, State.PROCESSING):
        raise HTTPException(409, "任务正在处理中")
    # 直播任务改为人工上传音频时，停止监视和录制
    await run_in_threadpool(live.manager.stop, task_id, "改为上传音频")
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
    if task["state"] in LIVE_TASK_STATES:
        # 取消预约 / 停止监视和录制
        await run_in_threadpool(live.manager.stop, task_id, "已取消")
        if task["match_hltv_id"]:
            await stages.hltv.unwatch_match(int(task["match_hltv_id"]))
        db.update_task(task_id, state=State.CANCELLED)
        db.add_event(task_id, "已取消", level="warning")
        return db.get_task(task_id)
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


# ── 直播：监视、录制、采访截取 ────────────────────────────────────────────────

class StreamRequest(BaseModel):
    url: Optional[str] = None
    name: Optional[str] = None
    provider: Optional[str] = None


class CaptureRequest(BaseModel):
    start: float
    end: float


def _require_live(task_id: int) -> Dict[str, Any]:
    task = _require_task(task_id)
    if task["source_type"] != "live":
        raise HTTPException(400, "不是直播任务")
    return task


class SourceRequest(BaseModel):
    # bo3.gg 比赛链接或 slug；留空表示重新自动匹配
    bo3: str = ""


@app.get("/api/tasks/{task_id}/live")
async def live_status(task_id: int):
    task = _require_live(task_id)
    status = await run_in_threadpool(live.manager.status, task_id)
    status["streams"] = await run_in_threadpool(live.candidate_streams, task, status)
    return status


@app.put("/api/tasks/{task_id}/live/source")
async def live_set_source(task_id: int, body: SourceRequest):
    """手动指定 bo3.gg 上对应的比赛（自动按队名匹配失败时）。"""
    task = _require_live(task_id)
    slug = body.bo3.strip().rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        match_sources.set_source_id(task_id, "bo3", None)
    else:
        client = bo3.Bo3Client(config.proxy_or_none(db.get_settings()))
        try:
            match = await client.get_match(slug)
        except Exception as error:
            raise HTTPException(400, f"读取 bo3.gg 比赛失败：{error}")
        swapped = bo3.same_team(task["team1"] or "", [match["team2"]["name"]])
        match_sources.set_source_id(task_id, "bo3", {"slug": slug, "swapped": swapped})
        db.add_event(task_id, f"已手动关联 bo3.gg 比赛：{slug}")
    watcher.check_soon(task_id)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/live/match-over")
async def live_match_over(task_id: int):
    """人工确认比赛已结束（比分数据源不可用时），开始按赛后流程监听采访。"""
    task = _require_live(task_id)
    if live.load_state(task_id).get("match_over_at"):
        raise HTTPException(409, "已经记录为比赛结束")
    if task["state"] not in (State.WATCHING, State.RECORDING):
        raise HTTPException(409, "只有监视中或录制中的任务可以标记比赛结束")
    await watcher.mark_over(task_id)
    return db.get_task(task_id)


@app.post("/api/tasks/{task_id}/live/check")
async def live_check(task_id: int):
    _require_live(task_id)
    watcher.check_soon(task_id)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/live/record")
async def live_record(task_id: int, body: StreamRequest):
    """立即开始录制，或切换直播源（不填 url 时自动选官方流）。"""
    task = _require_live(task_id)
    if task["state"] in (State.QUEUED, State.PROCESSING):
        raise HTTPException(409, "任务正在处理中")
    stream = body.model_dump() if body.url else None
    if stream is None:
        state = live.load_state(task_id)
        stream = state.get("stream") or live.pick_stream(live.candidate_streams(task, state))
    if not stream or not stream.get("url"):
        raise HTTPException(400, "没有找到官方直播源，请填写直播链接")
    try:
        await run_in_threadpool(live.manager.start, task_id, stream)
    except RuntimeError as error:
        raise HTTPException(400, str(error))
    if task["state"] in (State.RESERVED, State.WATCHING):
        db.update_task(task_id, state=State.RECORDING)
    elif task["state"] not in (State.RECORDING, State.INTERVIEW_DETECTING):
        # 已判定无采访、或已截取过，又重新录：直接进入采访监听
        live.manager.mark_match_over(task_id)
        db.update_task(task_id, state=State.INTERVIEW_DETECTING)
    return await run_in_threadpool(live.manager.status, task_id)


@app.post("/api/tasks/{task_id}/live/capture")
async def live_capture(task_id: int, body: CaptureRequest):
    _require_live(task_id)
    try:
        await run_in_threadpool(live.manager.capture, task_id, body.start, body.end, "manual")
    except (RuntimeError, ValueError) as error:
        raise HTTPException(400, str(error))
    return db.get_task(task_id)


@app.post("/api/tasks/{task_id}/live/no-interview")
async def live_no_interview(task_id: int):
    _require_live(task_id)
    await run_in_threadpool(live.manager.no_interview, task_id, "人工确认本场无采访")
    return db.get_task(task_id)


@app.get("/api/tasks/{task_id}/live/audio")
async def live_audio(task_id: int, start: float, end: float):
    """试听录音的某一段（相对录制开始的秒数，最长 10 分钟）。"""
    _require_live(task_id)
    state = live.load_state(task_id)
    if not state.get("recording"):
        raise HTTPException(404, "还没有录音")
    end = min(end, start + 600)
    chunks = sorted(live.manager.chunk_dir(task_id).glob("c_*.wav"))
    if live.manager.is_recording(task_id):
        chunks = chunks[:-1]
    timeline = live.chunk_timeline(chunks, state["recording"]["started_at"])
    buffer = io.BytesIO()
    try:
        await run_in_threadpool(live.cut_audio, timeline, start, end, buffer)
    except ValueError as error:
        raise HTTPException(400, str(error))
    return Response(buffer.getvalue(), media_type="audio/wav")


# ── HLTV 比赛 ─────────────────────────────────────────────────────────────────

@app.get("/api/matches")
async def list_matches(refresh: bool = False, days: int = 5):
    try:
        matches = await stages.hltv.list_matches(refresh=refresh)
    except Exception as error:
        raise HTTPException(502, f"抓取 HLTV 比赛列表失败：{error}")
    horizon = (time.time() + days * 86400) * 1000
    result = []
    for match in matches:
        if match["unix_ms"] and match["unix_ms"] > horizon:
            continue
        task = db.find_task_by_match(match["id"])
        result.append({**match, "task_id": task["id"] if task else None})
    return {"fetched_at": stages.hltv.cached_at("matches"), "matches": result}


class ReserveRequest(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None


@app.post("/api/matches/{match_id}/reserve")
async def reserve_match(match_id: int, body: ReserveRequest):
    existing = db.find_task_by_match(match_id)
    if existing:
        raise HTTPException(409, f"这场比赛已经预约过（任务 #{existing['id']}）")
    listed = next((m for m in (await stages.hltv.list_matches()) if m["id"] == match_id), None)
    title = body.title
    if not title:
        if listed:
            teams = " vs ".join(t["name"] or "TBD" for t in (listed["team1"], listed["team2"]))
            title = f"{teams} · {listed['event']['name']}" if listed["event"]["name"] else teams
        else:
            title = f"HLTV 比赛 {match_id}"
    task = db.create_task(
        title=title,
        state=State.RESERVED,
        source_type="live",
        match_hltv_id=str(match_id),
        match_time=(listed["unix_ms"] / 1000) if listed and listed["unix_ms"] else None,
        team1=(listed["team1"]["name"] or None) if listed else None,
        team2=(listed["team2"]["name"] or None) if listed else None,
        notes=body.notes,
    )
    db.add_event(task["id"], "已预约比赛")
    # 立即抓取背景信息；只跑这一阶段，完成后回到「已预约」
    runner.enqueue(task["id"], "context", "context")
    return db.get_task(task["id"])


# ── 前端静态资源（npm run build 后生成 dist/） ─────────────────────────────────

if config.WEB_DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=config.WEB_DIST_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        candidate = config.WEB_DIST_DIR / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(config.WEB_DIST_DIR / "index.html")
