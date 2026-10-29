"""
本地服务入口：REST API + 前端静态资源 + 后处理流水线。

启动：python -m backend   （默认 http://127.0.0.1:8000）
"""
import json
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, llm
from .integrations import feishu
from .pipeline import stages
from .runner import STAGES, STAGE_KEYS, PipelineRunner, State

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

runner = PipelineRunner()


async def _notify_failure(task_id: int, message: str) -> None:
    task = db.get_task(task_id)
    await run_in_threadpool(feishu.notify, db.get_settings(), f"【处理失败】{task['title']}\n{message}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    settings = db.get_settings()
    await stages.hltv.initialize(config.proxy_or_none(settings))
    stages.register(runner)
    runner.on_failure(_notify_failure)
    runner.start()
    logger.info("服务已启动，数据目录：%s", config.DATA_DIR)
    yield
    await runner.stop()
    await stages.hltv.close()


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
    old_proxy = config.proxy_or_none(db.get_settings())
    saved = db.save_settings(settings)
    if config.proxy_or_none(saved) != old_proxy:
        await stages.hltv.initialize(config.proxy_or_none(saved))
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
    fields = body.model_dump(exclude={"start"})
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


# ── 前端静态资源（npm run build 后生成 dist/） ─────────────────────────────────

if config.WEB_DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=config.WEB_DIST_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        candidate = config.WEB_DIST_DIR / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(config.WEB_DIST_DIR / "index.html")
