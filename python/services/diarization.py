"""
说话人分离服务 - 使用pyannote.audio进行说话人识别
支持分段处理长音频，避免内存溢出和超时
"""
import logging
import os
from typing import Optional, Dict, Any, Union, Callable, List
import torch
import torchaudio
from pyannote.audio import Pipeline
from pyannote.core import Annotation, Segment, Timeline

logger = logging.getLogger(__name__)

# HuggingFace token - 需要在 https://huggingface.co/settings/tokens 获取
# 并接受模型使用条款: https://huggingface.co/pyannote/speaker-diarization-3.1
# 从环境变量 HF_TOKEN 读取，也可通过 DIARIZATION_MODEL_PATH 指定本地模型目录跳过认证
HF_TOKEN = os.environ.get("HF_TOKEN", "")

# 长音频分段处理的参数
CHUNK_DURATION = 600  # 每段最长10分钟
CHUNK_OVERLAP = 30    # 段之间重叠30秒，用于说话人连续性


class DiarizationService:
    def __init__(self):
        self.pipeline = None

    @staticmethod
    def _merge_consecutive_speaker_segments(segments):
        """合并时间上连续且说话人相同的片段。"""
        if not segments:
            return []

        sorted_segments = sorted(segments, key=lambda item: (item["start"], item["end"]))
        merged = [sorted_segments[0].copy()]

        for segment in sorted_segments[1:]:
            last = merged[-1]
            if segment["speaker"] == last["speaker"]:
                # 同一说话人连续发言：扩展结束时间并尽量保留更早的开始时间。
                last["start"] = min(last["start"], segment["start"])
                last["end"] = max(last["end"], segment["end"])
            else:
                merged.append(segment.copy())

        return merged

    def _load_pipeline(self):
        """延迟加载模型，优先使用本地路径，否则从 HuggingFace 下载。"""
        if self.pipeline is None:
            local_path = os.environ.get("DIARIZATION_MODEL_PATH", "")
            if local_path and os.path.isdir(local_path):
                logger.info("从本地加载说话人分离模型: %s", local_path)
                self.pipeline = Pipeline.from_pretrained(local_path)
            else:
                model_id = os.environ.get("DIARIZATION_MODEL_ID", "pyannote/speaker-diarization-3.1")
                logger.info("加载说话人分离模型: %s", model_id)
                self.pipeline = Pipeline.from_pretrained(model_id, token=HF_TOKEN)

            # 使用GPU如果可用
            if torch.cuda.is_available():
                self.pipeline = self.pipeline.to(torch.device("cuda"))
                logger.info("说话人分离模型已加载到GPU")
            else:
                logger.info("说话人分离模型已加载到CPU")

    def _diarize_chunk(
        self,
        waveform: torch.Tensor,
        sample_rate: int,
        chunk_start: float,
        chunk_end: float,
        num_speakers: Optional[int] = None,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        处理单个音频片段的说话人分离

        Args:
            waveform: 完整音频波形
            sample_rate: 采样率
            chunk_start: 片段开始时间（秒）
            chunk_end: 片段结束时间（秒）
            num_speakers, min_speakers, max_speakers: 说话人数量参数

        Returns:
            该片段的说话人分离结果列表
        """
        # 计算样本索引
        start_sample = int(chunk_start * sample_rate)
        end_sample = int(chunk_end * sample_rate)

        # 提取片段波形
        chunk_waveform = waveform[:, start_sample:end_sample]

        # 运行说话人分离
        audio_chunk = {"waveform": chunk_waveform, "sample_rate": sample_rate}
        result = self.pipeline(
            audio_chunk,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers
        )

        # 获取 Annotation 对象
        diarization = self._get_annotation(result)

        # 转换为可序列化格式，并调整时间戳为全局时间
        segments = []
        for segment, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "start": segment.start + chunk_start,
                "end": segment.end + chunk_start,
                "speaker": speaker
            })

        return segments

    def _get_annotation(self, result) -> Annotation:
        """从结果中获取 Annotation 对象"""
        if hasattr(result, "speaker_diarization"):
            return result.speaker_diarization
        elif hasattr(result, "annotation"):
            return result.annotation
        elif hasattr(result, "to_annotation"):
            return result.to_annotation()
        else:
            return result

    def _merge_chunk_results(
        self,
        all_segments: List[List[Dict[str, Any]]],
        total_duration: float,
    ) -> List[Dict[str, Any]]:
        """
        合并多个片段的结果，处理重叠区域的说话人一致性

        Args:
            all_segments: 所有片段的说话人分离结果
            total_duration: 音频总时长

        Returns:
            合并后的说话人分离结果
        """
        if not all_segments:
            return []

        # 展平所有片段
        flat_segments = []
        for chunk_segments in all_segments:
            flat_segments.extend(chunk_segments)

        if not flat_segments:
            return []

        # 按开始时间排序
        flat_segments.sort(key=lambda x: x["start"])

        # 合并连续的相同说话人片段
        merged = self._merge_consecutive_speaker_segments(flat_segments)

        return merged

    def diarize(
        self,
        audio: Union[str, Dict[str, Any]],
        num_speakers: Optional[int] = None,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        对音频进行说话人分离，支持分段处理长音频

        Args:
            audio: 音频文件路径，或包含 {"waveform": Tensor, "sample_rate": int} 的字典
            num_speakers: 指定说话人数量
            min_speakers: 最小说话人数
            max_speakers: 最大说话人数
            progress_callback: 进度回调函数

        Returns:
            包含说话人信息的字典:
            {
                "segments": [{"start": float, "end": float, "speaker": str}, ...],
                "num_speakers": int,
                "speakers": [str, ...]
            }
        """
        self._load_pipeline()

        label = audio if isinstance(audio, str) else "waveform input"
        logger.info(f"开始说话人分离: {label}")

        if progress_callback:
            progress_callback(**{
                "status": "running",
                "stage": "loading_audio",
                "progress": 1,
                "message": "正在加载音频...",
                "processed_duration": 0,
                "total_duration": None,
                "chunk_count": 0,
                "total_chunks": 0,
            })

        # 如果是文件路径，先加载并重采样到16kHz
        waveform = None
        sample_rate = None
        if isinstance(audio, str):
            waveform, sample_rate = torchaudio.load(audio)
            target_sample_rate = 16000
            if sample_rate != target_sample_rate:
                logger.info(f"重采样: {sample_rate}Hz -> {target_sample_rate}Hz")
                resampler = torchaudio.transforms.Resample(
                    orig_freq=sample_rate,
                    new_freq=target_sample_rate
                )
                waveform = resampler(waveform)
            sample_rate = target_sample_rate
        else:
            waveform = audio["waveform"]
            sample_rate = audio["sample_rate"]

        # 计算音频时长
        num_samples = waveform.shape[1]
        total_duration = num_samples / sample_rate

        logger.info(f"音频时长: {total_duration:.1f}秒 ({total_duration/60:.1f}分钟)")

        # 判断是否需要分段处理（超过15分钟的音频分段处理）
        use_chunking = total_duration > CHUNK_DURATION + CHUNK_OVERLAP

        if use_chunking:
            logger.info(f"启用分段处理模式，每段 {CHUNK_DURATION} 秒")
            return self._diarize_with_chunking(
                waveform, sample_rate, total_duration,
                num_speakers, min_speakers, max_speakers,
                progress_callback
            )
        else:
            logger.info("音频较短，直接处理")
            return self._diarize_direct(
                waveform, sample_rate, total_duration,
                num_speakers, min_speakers, max_speakers,
                progress_callback
            )

    def _diarize_direct(
        self,
        waveform: torch.Tensor,
        sample_rate: int,
        total_duration: float,
        num_speakers: Optional[int],
        min_speakers: Optional[int],
        max_speakers: Optional[int],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """直接处理较短音频"""
        if progress_callback:
            progress_callback(**{
                "status": "running",
                "stage": "diarizing",
                "progress": 10,
                "message": "正在进行说话人分离...",
                "processed_duration": 0,
                "total_duration": total_duration,
                "chunk_count": 0,
                "total_chunks": 1,
            })

        audio = {"waveform": waveform, "sample_rate": sample_rate}
        result = self.pipeline(
            audio,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers
        )

        diarization = self._get_annotation(result)

        # 转换为可序列化格式
        segments = []
        speaker_labels = set()

        for segment, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "start": segment.start,
                "end": segment.end,
                "speaker": speaker
            })
            speaker_labels.add(speaker)

        merged_segments = self._merge_consecutive_speaker_segments(segments)

        if progress_callback:
            progress_callback(**{
                "status": "completed",
                "stage": "completed",
                "progress": 100,
                "message": f"说话人分离完成，发现 {len(speaker_labels)} 个说话人",
                "processed_duration": total_duration,
                "total_duration": total_duration,
                "chunk_count": 1,
                "total_chunks": 1,
            })

        logger.info(
            "说话人分离完成，发现 %s 个说话人，原始片段 %s 个，合并后 %s 个",
            len(speaker_labels), len(segments), len(merged_segments)
        )

        return {
            "segments": merged_segments,
            "num_speakers": len(speaker_labels),
            "speakers": list(speaker_labels)
        }

    def _diarize_with_chunking(
        self,
        waveform: torch.Tensor,
        sample_rate: int,
        total_duration: float,
        num_speakers: Optional[int],
        min_speakers: Optional[int],
        max_speakers: Optional[int],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """分段处理长音频"""
        # 计算分段数量
        chunk_starts = []
        pos = 0.0
        while pos < total_duration:
            chunk_starts.append(pos)
            pos += CHUNK_DURATION

        total_chunks = len(chunk_starts)
        logger.info(f"将音频分为 {total_chunks} 段进行处理")

        if progress_callback:
            progress_callback(**{
                "status": "running",
                "stage": "diarizing",
                "progress": 5,
                "message": f"分段处理中，共 {total_chunks} 段...",
                "processed_duration": 0,
                "total_duration": total_duration,
                "chunk_count": 0,
                "total_chunks": total_chunks,
            })

        all_segments = []
        for i, chunk_start in enumerate(chunk_starts):
            chunk_end = min(chunk_start + CHUNK_DURATION + CHUNK_OVERLAP, total_duration)

            logger.info(f"处理第 {i+1}/{total_chunks} 段: {chunk_start:.1f}s - {chunk_end:.1f}s")

            if progress_callback:
                progress = 5 + (i / total_chunks) * 90
                processed_duration = chunk_start
                progress_callback(**{
                    "status": "running",
                    "stage": "diarizing",
                    "progress": round(progress, 1),
                    "message": f"处理第 {i+1}/{total_chunks} 段 ({chunk_start/60:.1f}min - {chunk_end/60:.1f}min)",
                    "processed_duration": processed_duration,
                    "total_duration": total_duration,
                    "chunk_count": i + 1,
                    "total_chunks": total_chunks,
                })

            try:
                chunk_segments = self._diarize_chunk(
                    waveform, sample_rate,
                    chunk_start, chunk_end,
                    num_speakers, min_speakers, max_speakers
                )
                all_segments.append(chunk_segments)
                logger.info(f"第 {i+1} 段完成，发现 {len(chunk_segments)} 个片段")
            except Exception as e:
                logger.error(f"处理第 {i+1} 段时出错: {e}")
                # 继续处理其他段

        # 合并所有片段的结果
        if progress_callback:
            progress_callback(**{
                "status": "running",
                "stage": "merging",
                "progress": 95,
                "message": "正在合并结果...",
                "processed_duration": total_duration,
                "total_duration": total_duration,
                "chunk_count": total_chunks,
                "total_chunks": total_chunks,
            })

        merged_segments = self._merge_chunk_results(all_segments, total_duration)

        # 收集所有说话人标签
        speaker_labels = set(seg["speaker"] for seg in merged_segments)

        if progress_callback:
            progress_callback(**{
                "status": "completed",
                "stage": "completed",
                "progress": 100,
                "message": f"说话人分离完成，发现 {len(speaker_labels)} 个说话人",
                "processed_duration": total_duration,
                "total_duration": total_duration,
                "chunk_count": total_chunks,
                "total_chunks": total_chunks,
            })

        logger.info(
            "分段说话人分离完成，发现 %s 个说话人，合并后 %s 个片段",
            len(speaker_labels), len(merged_segments)
        )

        return {
            "segments": merged_segments,
            "num_speakers": len(speaker_labels),
            "speakers": list(speaker_labels)
        }
