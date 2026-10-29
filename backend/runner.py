"""
任务状态机与后处理流水线执行器。

状态（state）是任务的粗粒度生命周期；阶段（stage）是 processing 状态下
流水线正在执行的步骤。所有阶段串行执行（共用一块 GPU），任何阶段都可以
单独重跑，后续阶段随之重跑。
"""
import asyncio
import inspect
import logging
import traceback
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, Union

from . import db

logger = logging.getLogger(__name__)


class State:
    # 第三阶段（直播监视、录音）启用的状态
    RESERVED = "reserved"
    WATCHING = "watching"
    RECORDING = "recording"
    INTERVIEW_DETECTING = "interview_detecting"
    NO_INTERVIEW = "no_interview"
    # 后处理
    DRAFT = "draft"  # 已创建，尚未开始（如等待上传音频）
    QUEUED = "queued"
    PROCESSING = "processing"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 后处理流水线阶段，按执行顺序排列
STAGES: List[Tuple[str, str]] = [
    ("context", "抓取背景信息"),
    ("ingest", "获取音频"),
    ("transcribe", "语音转写"),
    ("clean", "英文清洗"),
    ("write", "中文成稿"),
    ("export", "生成 Word"),
    ("deliver", "飞书发送"),
]
STAGE_KEYS = [key for key, _ in STAGES]
STAGE_LABELS = dict(STAGES)

StageFn = Callable[[int], Union[None, Awaitable[None]]]


class TaskCancelled(Exception):
    pass


class PipelineRunner:
    def __init__(self) -> None:
        self._queue: "asyncio.Queue[Tuple[int, str, Optional[str], str]]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._stage_fns: Dict[str, StageFn] = {}
        self._on_failure: Optional[Callable[[int, str], Awaitable[None]]] = None
        self.current_task_id: Optional[int] = None

    def register(self, stage: str, fn: StageFn) -> None:
        if stage not in STAGE_KEYS:
            raise ValueError(f"未知阶段: {stage}")
        self._stage_fns[stage] = fn

    def on_failure(self, fn: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_failure = fn

    def start(self) -> None:
        self._recover_interrupted()
        self._worker = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()

    def enqueue(self, task_id: int, from_stage: str = STAGE_KEYS[0], to_stage: Optional[str] = None) -> None:
        """从 from_stage 开始执行；指定 to_stage 时只执行到该阶段为止，完成后恢复原状态。"""
        if from_stage not in STAGE_KEYS or (to_stage and to_stage not in STAGE_KEYS):
            raise ValueError("未知阶段")
        if to_stage and STAGE_KEYS.index(to_stage) < STAGE_KEYS.index(from_stage):
            raise ValueError("结束阶段不能早于开始阶段")
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        if task["state"] in (State.QUEUED, State.PROCESSING):
            raise ValueError("任务正在排队或处理中")
        db.update_task(task_id, state=State.QUEUED, stage=from_stage, error=None, cancel_requested=False)
        scope = f"从「{STAGE_LABELS[from_stage]}」开始"
        if to_stage:
            scope += f"，到「{STAGE_LABELS[to_stage]}」为止"
        db.add_event(task_id, f"加入队列，{scope}", stage=from_stage)
        self._queue.put_nowait((task_id, from_stage, to_stage, task["state"]))

    @staticmethod
    def check_cancelled(task_id: int) -> None:
        task = db.get_task(task_id)
        if task and task["cancel_requested"]:
            raise TaskCancelled()

    @staticmethod
    def _recover_interrupted() -> None:
        """服务重启时，把上次没跑完的任务标记为失败，等待人工重试。"""
        for task in db.list_tasks():
            if task["state"] in (State.QUEUED, State.PROCESSING):
                db.update_task(task["id"], state=State.FAILED, error="服务重启，处理被中断")
                db.add_event(task["id"], "服务重启，处理被中断，可从当前阶段重试", level="error", stage=task["stage"])

    async def _loop(self) -> None:
        while True:
            task_id, from_stage, to_stage, previous_state = await self._queue.get()
            try:
                await self._run(task_id, from_stage, to_stage, previous_state)
            except Exception:  # 兜底，保证 worker 不退出
                logger.exception("任务 %s 执行器异常", task_id)
            finally:
                self.current_task_id = None
                self._queue.task_done()

    async def _run(self, task_id: int, from_stage: str, to_stage: Optional[str], previous_state: str) -> None:
        task = db.get_task(task_id)
        if task is None:
            return
        if task["cancel_requested"]:
            db.update_task(task_id, state=State.CANCELLED)
            db.add_event(task_id, "已取消", level="warning")
            return

        self.current_task_id = task_id
        db.update_task(task_id, state=State.PROCESSING)
        stage = from_stage
        try:
            end = STAGE_KEYS.index(to_stage) + 1 if to_stage else len(STAGE_KEYS)
            for stage in STAGE_KEYS[STAGE_KEYS.index(from_stage):end]:
                self.check_cancelled(task_id)
                db.update_task(task_id, stage=stage)
                db.add_event(task_id, f"开始：{STAGE_LABELS[stage]}", stage=stage)
                fn = self._stage_fns.get(stage)
                if fn is None:
                    db.add_event(task_id, "该阶段未实现，跳过", level="warning", stage=stage)
                    continue
                if inspect.iscoroutinefunction(fn):
                    await fn(task_id)
                else:
                    await asyncio.to_thread(fn, task_id)
                db.add_event(task_id, f"完成：{STAGE_LABELS[stage]}", stage=stage)
        except TaskCancelled:
            db.update_task(task_id, state=State.CANCELLED)
            db.add_event(task_id, "已取消", level="warning", stage=stage)
            return
        except Exception as error:
            message = f"{STAGE_LABELS[stage]}失败：{error}"
            logger.error("任务 %s %s\n%s", task_id, message, traceback.format_exc())
            db.update_task(task_id, state=State.FAILED, error=message)
            db.add_event(task_id, message, level="error", stage=stage)
            if self._on_failure:
                try:
                    await self._on_failure(task_id, message)
                except Exception:
                    logger.exception("失败通知发送失败")
            return

        if to_stage and to_stage != STAGE_KEYS[-1]:
            db.update_task(task_id, state=previous_state, stage=None)
            return
        db.update_task(task_id, state=State.REVIEW, stage=None)
        db.add_event(task_id, "初稿已完成，等待人工精校")
