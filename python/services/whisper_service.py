"""
Whisper语音识别服务 - 使用faster-whisper进行GPU加速转录
"""
import os
import logging
from typing import Optional, List, Dict, Any, Callable
from faster_whisper import WhisperModel, BatchedInferencePipeline

logger = logging.getLogger(__name__)

# 本地模型路径：优先使用环境变量，否则使用项目目录下的 models/whisper
LOCAL_MODEL_PATH = os.environ.get(
    "WHISPER_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "models/whisper")
)


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--"

    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class WhisperService:
    def __init__(self, device: str = "cuda"):
        """
        初始化Whisper模型

        Args:
            device: 设备 (cuda, cpu)
        """
        self.device = device
        self.model = None

    def _load_model(self):
        """延迟加载模型"""
        if self.model is None:
            logger.info(f"加载本地Whisper模型: {LOCAL_MODEL_PATH}")
            base_model = WhisperModel(
                LOCAL_MODEL_PATH,
                device=self.device,
                compute_type="float16" if self.device == "cuda" else "int8"
            )
            self.model = BatchedInferencePipeline(model=base_model)
            logger.info("Whisper模型加载完成")

    def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        转录音频文件

        Args:
            audio_path: 音频文件路径
            language: 语言代码 (如 'en', 'zh')

        Returns:
            包含转录结果的字典
        """
        if progress_callback:
            progress_callback(**{
                "status": "running",
                "stage": "loading_model",
                "progress": 1,
                "message": "正在加载 Whisper 模型...",
                "processed_duration": 0,
                "total_duration": None,
                "segment_count": 0,
            })

        self._load_model()

        logger.info(f"开始转录: {audio_path}")

        if progress_callback:
            progress_callback(**{
                "status": "running",
                "stage": "preparing",
                "progress": 3,
                "message": "正在分析音频并检测语言...",
                "processed_duration": 0,
                "total_duration": None,
                "segment_count": 0,
            })

        segments, info = self.model.transcribe(
            audio_path,
            language=language,
            beam_size=1,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            condition_on_previous_text=False,
            batch_size=16,
        )

        total_duration = info.duration or 0

        if progress_callback:
            progress_callback(**{
                "status": "running",
                "stage": "transcribing",
                "progress": 5,
                "message": f"开始转写，语言: {info.language.upper()}，总时长 {format_duration(total_duration)}",
                "processed_duration": 0,
                "total_duration": total_duration,
                "segment_count": 0,
                "language": info.language,
                "language_probability": info.language_probability,
            })

        # 转换为可序列化的格式
        segments_list = []
        for index, segment in enumerate(segments, start=1):
            segments_list.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
                "words": [
                    {
                        "start": w.start,
                        "end": w.end,
                        "word": w.word,
                        "probability": w.probability
                    }
                    for w in (segment.words or [])
                ]
            })

            if progress_callback:
                processed_duration = min(segment.end, total_duration) if total_duration else segment.end
                raw_progress = (processed_duration / total_duration * 100) if total_duration else 0
                progress = min(99, max(5, raw_progress))
                progress_callback(**{
                    "status": "running",
                    "stage": "transcribing",
                    "progress": round(progress, 2),
                    "message": (
                        f"转写中 {format_duration(processed_duration)} / {format_duration(total_duration)}"
                        f" · 已生成 {index} 个片段"
                    ),
                    "processed_duration": processed_duration,
                    "total_duration": total_duration,
                    "segment_count": index,
                    "language": info.language,
                    "language_probability": info.language_probability,
                })

        logger.info(f"转录完成，共 {len(segments_list)} 个片段")

        if progress_callback:
            progress_callback(**{
                "status": "completed",
                "stage": "completed",
                "progress": 100,
                "message": f"转写完成，共 {len(segments_list)} 个片段",
                "processed_duration": total_duration,
                "total_duration": total_duration,
                "segment_count": len(segments_list),
                "language": info.language,
                "language_probability": info.language_probability,
            })

        return {
            "segments": segments_list,
            "language": info.language,
            "language_probability": info.language_probability,
            "duration": info.duration
        }

    def transcribe_with_timestamps(
        self,
        audio_path: str
    ) -> List[Dict[str, Any]]:
        """
        转录并返回带时间戳的结果

        Returns:
            带时间戳的转录片段列表
        """
        result = self.transcribe(audio_path)
        return result["segments"]
