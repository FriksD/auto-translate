"""
FastAPI 服务，提供语音识别、说话人分离、翻译等 API。
"""
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 加载 .env 文件（开发环境使用，生产环境由 Electron 传入环境变量）
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)

from services.diarization import DiarizationService
from services.hltv_scraper import HLTVScraper
from services.llm_service import LLMService
from services.whisper_service import WhisperService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

whisper_service: Optional[WhisperService] = None
diarization_service: Optional[DiarizationService] = None
hltv_scraper = HLTVScraper()
llm_service = LLMService()
transcription_progress_lock = threading.Lock()
transcription_progress: Dict[str, Any] = {
    "status": "idle",
    "stage": "idle",
    "progress": 0,
    "message": "等待开始转写",
    "audio_path": None,
    "processed_duration": 0,
    "total_duration": None,
    "segment_count": 0,
    "language": None,
    "language_probability": None,
    "error": None,
    "started_at": None,
    "updated_at": None,
}
diarization_progress_lock = threading.Lock()
diarization_progress: Dict[str, Any] = {
    "status": "idle",
    "stage": "idle",
    "progress": 0,
    "message": "等待开始说话人分离",
    "audio_path": None,
    "processed_duration": 0,
    "total_duration": None,
    "chunk_count": 0,
    "total_chunks": 0,
    "error": None,
    "started_at": None,
    "updated_at": None,
}


def set_transcription_progress(**updates: Any) -> None:
    with transcription_progress_lock:
        transcription_progress.update(updates)
        transcription_progress["updated_at"] = time.time()


def get_transcription_progress_snapshot() -> Dict[str, Any]:
    with transcription_progress_lock:
        return dict(transcription_progress)


def reset_transcription_progress(audio_path: str) -> None:
    now = time.time()
    with transcription_progress_lock:
        transcription_progress.clear()
        transcription_progress.update({
            "status": "running",
            "stage": "queued",
            "progress": 0,
            "message": "准备开始转写...",
            "audio_path": audio_path,
            "processed_duration": 0,
            "total_duration": None,
            "segment_count": 0,
            "language": None,
            "language_probability": None,
            "error": None,
            "started_at": now,
            "updated_at": now,
        })


def set_diarization_progress(**updates: Any) -> None:
    with diarization_progress_lock:
        diarization_progress.update(updates)
        diarization_progress["updated_at"] = time.time()


def get_diarization_progress_snapshot() -> Dict[str, Any]:
    with diarization_progress_lock:
        return dict(diarization_progress)


def reset_diarization_progress(audio_path: str) -> None:
    now = time.time()
    with diarization_progress_lock:
        diarization_progress.clear()
        diarization_progress.update({
            "status": "running",
            "stage": "queued",
            "progress": 0,
            "message": "准备开始说话人分离...",
            "audio_path": audio_path,
            "processed_duration": 0,
            "total_duration": None,
            "chunk_count": 0,
            "total_chunks": 0,
            "error": None,
            "started_at": now,
            "updated_at": now,
        })


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用启动时初始化服务。"""
    global whisper_service, diarization_service

    logger.info("初始化服务...")

    try:
        whisper_service = WhisperService()
        logger.info("Whisper 服务初始化完成")
    except Exception as error:
        logger.warning("Whisper 服务初始化失败: %s", error)

    try:
        diarization_service = DiarizationService()
        logger.info("说话人分离服务初始化完成")
    except Exception as error:
        logger.warning("说话人分离服务初始化失败: %s", error)

    try:
        await hltv_scraper.initialize()
    except Exception as error:
        logger.warning("HLTV 客户端初始化失败: %s", error)

    yield

    await hltv_scraper.close()


app = FastAPI(title="CS Interview Translator API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    """健康检查。"""
    return {
        "status": "ok",
        "whisper": whisper_service is not None,
        "diarization": diarization_service is not None,
    }


class TranscribeRequest(BaseModel):
    audio_path: str
    language: Optional[str] = None


class DiarizeRequest(BaseModel):
    audio_path: str
    num_speakers: Optional[int] = None


class TranscribeProgressRequest(BaseModel):
    audio_path: Optional[str] = None


class TranslateRequest(BaseModel):
    transcript: Dict[str, Any]
    speakers: Dict[str, str]
    context: Optional[Dict[str, Any]] = None
    llm_config: Dict[str, Any]


class PromptPreviewRequest(BaseModel):
    transcript: Dict[str, Any]
    speakers: Dict[str, str]
    context: Optional[Dict[str, Any]] = None


class HLTVRequest(BaseModel):
    query: str
    type: str  # 'team' or 'player'


class BailianModelsRequest(BaseModel):
    api_key: str


@app.post("/api/bailian/models")
async def get_bailian_models(request: BailianModelsRequest):
    """获取阿里百炼可用模型列表。"""
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                headers={
                    "Authorization": f"Bearer {request.api_key}",
                    "Content-Type": "application/json"
                },
                timeout=30.0
            )

            # 如果直接获取失败，返回预定义的模型列表
            if response.status_code != 200:
                # 返回百炼常用模型列表
                return {
                    "models": [
                        {"id": "qwen-turbo", "name": "通义千问-Turbo", "description": "快速响应，适合简单任务"},
                        {"id": "qwen-plus", "name": "通义千问-Plus", "description": "平衡性能与效果"},
                        {"id": "qwen-max", "name": "通义千问-Max", "description": "最强能力，复杂任务首选"},
                        {"id": "qwen-max-longcontext", "name": "通义千问-Max长文本", "description": "支持超长上下文"},
                        {"id": "qwen-long", "name": "通义千问-Long", "description": "长文本处理专家"},
                        {"id": "qwen2.5-72b-instruct", "name": "Qwen2.5-72B-Instruct", "description": "开源版本最强"},
                        {"id": "qwen2.5-32b-instruct", "name": "Qwen2.5-32B-Instruct", "description": "中等规模开源版"},
                        {"id": "qwen2.5-14b-instruct", "name": "Qwen2.5-14B-Instruct", "description": "轻量级开源版"},
                        {"id": "qwen2.5-7b-instruct", "name": "Qwen2.5-7B-Instruct", "description": "最小开源版"},
                        {"id": "deepseek-r1", "name": "DeepSeek-R1", "description": "深度求索推理模型"},
                        {"id": "deepseek-v3", "name": "DeepSeek-V3", "description": "深度求索通用模型"},
                        {"id": "yi-lightning", "name": "零一万物-Lightning", "description": "零一万物快速版"},
                        {"id": "yi-large", "name": "零一万物-Large", "description": "零一万物大模型"},
                    ]
                }

            return response.json()
    except Exception as error:
        logger.error("获取百炼模型列表失败: %s", error)
        # 返回默认模型列表
        return {
            "models": [
                {"id": "qwen-turbo", "name": "通义千问-Turbo", "description": "快速响应，适合简单任务"},
                {"id": "qwen-plus", "name": "通义千问-Plus", "description": "平衡性能与效果"},
                {"id": "qwen-max", "name": "通义千问-Max", "description": "最强能力，复杂任务首选"},
                {"id": "qwen-max-longcontext", "name": "通义千问-Max长文本", "description": "支持超长上下文"},
                {"id": "qwen-long", "name": "通义千问-Long", "description": "长文本处理专家"},
                {"id": "qwen2.5-72b-instruct", "name": "Qwen2.5-72B-Instruct", "description": "开源版本最强"},
                {"id": "qwen2.5-32b-instruct", "name": "Qwen2.5-32B-Instruct", "description": "中等规模开源版"},
                {"id": "qwen2.5-14b-instruct", "name": "Qwen2.5-14B-Instruct", "description": "轻量级开源版"},
                {"id": "qwen2.5-7b-instruct", "name": "Qwen2.5-7B-Instruct", "description": "最小开源版"},
                {"id": "deepseek-r1", "name": "DeepSeek-R1", "description": "深度求索推理模型"},
                {"id": "deepseek-v3", "name": "DeepSeek-V3", "description": "深度求索通用模型"},
                {"id": "yi-lightning", "name": "零一万物-Lightning", "description": "零一万物快速版"},
                {"id": "yi-large", "name": "零一万物-Large", "description": "零一万物大模型"},
            ]
        }


@app.post("/api/transcribe")
async def transcribe(request: TranscribeRequest):
    """语音转录。"""
    if not whisper_service:
        raise HTTPException(status_code=503, detail="Whisper 服务未初始化")

    if not os.path.exists(request.audio_path):
        raise HTTPException(status_code=404, detail="音频文件不存在")

    try:
        reset_transcription_progress(request.audio_path)
        return await run_in_threadpool(
            whisper_service.transcribe,
            request.audio_path,
            request.language,
            set_transcription_progress,
        )
    except Exception as error:
        set_transcription_progress(
            status="error",
            stage="error",
            message=f"转写失败: {error}",
            error=str(error),
        )
        logger.error("转录失败: %s", error)
        raise HTTPException(status_code=500, detail=str(error))


@app.post("/api/transcribe/progress")
async def get_transcribe_progress(request: TranscribeProgressRequest):
    """获取当前转写进度。"""
    progress = get_transcription_progress_snapshot()

    if request.audio_path and progress.get("audio_path") not in {None, request.audio_path}:
        return {
            "status": "idle",
            "stage": "idle",
            "progress": 0,
            "message": "等待当前音频开始转写",
            "audio_path": request.audio_path,
            "processed_duration": 0,
            "total_duration": None,
            "segment_count": 0,
            "language": None,
            "language_probability": None,
            "error": None,
            "started_at": None,
            "updated_at": None,
        }

    return progress


@app.post("/api/diarize")
async def diarize(request: DiarizeRequest):
    """说话人分离。"""
    if not diarization_service:
        raise HTTPException(status_code=503, detail="说话人分离服务未初始化")

    if not os.path.exists(request.audio_path):
        raise HTTPException(status_code=404, detail="音频文件不存在")

    try:
        reset_diarization_progress(request.audio_path)
        return await run_in_threadpool(
            diarization_service.diarize,
            request.audio_path,
            request.num_speakers,
            None,  # min_speakers
            None,  # max_speakers
            set_diarization_progress,
        )
    except Exception as error:
        set_diarization_progress(
            status="error",
            stage="error",
            message=f"说话人分离失败: {error}",
            error=str(error),
        )
        logger.error("说话人分离失败: %s", error)
        raise HTTPException(status_code=500, detail=str(error))


class DiarizeProgressRequest(BaseModel):
    audio_path: Optional[str] = None


@app.post("/api/diarize/progress")
async def get_diarize_progress(request: DiarizeProgressRequest):
    """获取当前说话人分离进度。"""
    progress = get_diarization_progress_snapshot()

    if request.audio_path and progress.get("audio_path") not in {None, request.audio_path}:
        return {
            "status": "idle",
            "stage": "idle",
            "progress": 0,
            "message": "等待当前音频开始说话人分离",
            "audio_path": request.audio_path,
            "processed_duration": 0,
            "total_duration": None,
            "chunk_count": 0,
            "total_chunks": 0,
            "error": None,
            "started_at": None,
            "updated_at": None,
        }

    return progress


@app.post("/api/translate")
async def translate(request: TranslateRequest):
    """翻译并生成新闻稿。"""
    try:
        article = llm_service.translate_to_news(
            transcript=request.transcript,
            speakers=request.speakers,
            context=request.context,
            config=request.llm_config,
        )
        return {"article": article}
    except Exception as error:
        logger.error("翻译失败: %s", error)
        status_code = getattr(error, "status_code", None)
        if not isinstance(status_code, int) or status_code < 400 or status_code > 599:
            status_code = 500
        raise HTTPException(status_code=status_code, detail=str(error))


@app.post("/api/prompt/preview")
async def preview_prompt(request: PromptPreviewRequest):
    """生成发送给 LLM 的提示词预览。"""
    try:
        prompt = llm_service.build_prompt_preview(
            transcript=request.transcript,
            speakers=request.speakers,
            context=request.context,
        )
        return {"prompt": prompt}
    except Exception as error:
        logger.error("生成提示词预览失败: %s", error)
        status_code = getattr(error, "status_code", None)
        if not isinstance(status_code, int) or status_code < 400 or status_code > 599:
            status_code = 500
        raise HTTPException(status_code=status_code, detail=str(error))


@app.post("/api/hltv")
async def get_hltv_info(request: HLTVRequest):
    """获取 HLTV 信息。"""
    try:
        if request.type == "team":
            return await hltv_scraper.get_team_info(request.query)
        if request.type == "player":
            return await hltv_scraper.get_player_info(request.query)
        raise HTTPException(status_code=400, detail="无效的类型")
    except Exception as error:
        logger.error("获取 HLTV 信息失败: %s", error)
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/api/hltv/search/{query}")
async def search_hltv(query: str):
    """搜索 HLTV。"""
    try:
        return {
            "teams": await hltv_scraper.search_teams(query),
            "players": await hltv_scraper.search_players(query),
        }
    except Exception as error:
        logger.error("搜索 HLTV 失败: %s", error)
        raise HTTPException(status_code=500, detail=str(error))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PYTHON_SERVER_PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
