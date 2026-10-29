"""
Whisper语音识别服务 - 使用faster-whisper进行GPU加速转录
"""
import logging
from typing import Any, Callable, Dict, Optional
from faster_whisper import WhisperModel, BatchedInferencePipeline

logger = logging.getLogger(__name__)


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
    def __init__(self, model: str, device: str = "cuda", compute_type: str = "float16"):
        """
        Args:
            model: 本地模型目录或 faster-whisper 模型名（如 large-v3、large-v3-turbo）
            device: 设备 (cuda, cpu)
            compute_type: 计算精度，cuda 推荐 float16 / int8_float16
        """
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.model = None

    def matches(self, model: str, device: str, compute_type: str) -> bool:
        return (self.model_name, self.device, self.compute_type) == (model, device, compute_type)

    def _load_model(self):
        """延迟加载模型，CUDA 不可用时退回 CPU。"""
        if self.model is not None:
            return
        logger.info("加载 Whisper 模型: %s (%s, %s)", self.model_name, self.device, self.compute_type)
        try:
            base_model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        except Exception as error:
            if self.device == "cpu":
                raise
            logger.warning("CUDA 加载失败，退回 CPU: %s", error)
            self.device, self.compute_type = "cpu", "int8"
            base_model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        self.model = BatchedInferencePipeline(model=base_model)
        logger.info("Whisper 模型加载完成")

    def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
        progress_callback: Optional[Callable[..., None]] = None,
        initial_prompt: Optional[str] = None,
        beam_size: int = 1,
    ) -> Dict[str, Any]:
        """
        转录音频文件

        Args:
            audio_path: 音频文件路径
            language: 语言代码 (如 'en', 'zh')，None 为自动检测
            initial_prompt: 提示词，用于提升选手名、队名等专有名词的识别率

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
            beam_size=beam_size,
            initial_prompt=initial_prompt,
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
