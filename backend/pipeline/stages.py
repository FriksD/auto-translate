"""
后处理流水线各阶段的实现。每个阶段只读写任务目录下的文件和任务记录，
因此任何阶段都可以单独重跑。

任务目录 data/tasks/<id>/ 下的文件：
  source.*            原始音频/视频（下载或上传）
  audio.wav           16kHz 单声道音频（按截取区间裁剪后）
  transcript.json     Whisper 转写结果（含说话人标签）
  clean_en.txt        第一步：英文清洗后的问答稿
  draft_raw_zh.txt    第二步：LLM 输出的中文稿
  draft_zh.txt        名词规范化后的中文稿（人工可在界面上修改）
  names_report.json   名词替换记录与待核对列表
  *.docx              最终 Word 初稿
"""
import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config, db, llm
from ..integrations.feishu import FeishuClient
from ..runner import PipelineRunner
from ..sources.browser import BrowserFetcher
from ..sources.hltv import HLTVClient
from . import context as context_builder
from . import prompts
from .docx_export import export_docx
from .names import NameNormalizer, collect_canonical_names, replace_tokens, token_contexts

logger = logging.getLogger(__name__)

TEXT_ARTIFACTS = ("clean_en.txt", "draft_raw_zh.txt", "draft_zh.txt")

browser = BrowserFetcher(config.DATA_DIR / "browser-profile")
hltv = HLTVClient(browser)
_whisper = None
_diarizer = None
_model_lock = threading.Lock()

# 内存中的阶段进度，供界面轮询：task_id → {percent, message, updated_at}
progress: Dict[int, Dict[str, Any]] = {}


def set_progress(task_id: int, percent: Optional[float], message: str) -> None:
    progress[task_id] = {"percent": percent, "message": message, "updated_at": time.time()}


def _task(task_id: int) -> Dict[str, Any]:
    task = db.get_task(task_id)
    if task is None:
        raise RuntimeError("任务不存在")
    return task


def _glossary() -> Tuple[Dict[str, str], Dict[str, str]]:
    aliases, terms = {}, {}
    for entry in db.list_glossary():
        (aliases if entry["kind"] == "alias" else terms)[entry["term"]] = entry["target"]
    return aliases, terms


def _names(task: Dict[str, Any]) -> Tuple[List[str], Dict[str, str]]:
    aliases, _ = _glossary()
    return collect_canonical_names(task["context"]), aliases


def read_text(task_id: int, name: str) -> str:
    path = config.task_dir(task_id) / name
    if not path.exists():
        raise RuntimeError(f"缺少上一阶段的产物 {name}，请从更早的阶段重跑")
    return path.read_text(encoding="utf-8")


def write_text(task_id: int, name: str, text: str) -> None:
    (config.task_dir(task_id) / name).write_text(text, encoding="utf-8")


# ── context ───────────────────────────────────────────────────────────────────

async def stage_context(task_id: int) -> None:
    task = _task(task_id)
    context = dict(task["context"])
    if context.get("teams"):
        db.add_event(task_id, "已有背景信息，跳过抓取（如需刷新请在背景信息页重新抓取）", stage="context")
        return

    if task["match_hltv_id"]:
        set_progress(task_id, None, "正在从 HLTV 抓取比赛信息...")
        built, warnings = await context_builder.build_from_match(hltv, int(task["match_hltv_id"]))
    else:
        names = [t for t in (task["team1"], task["team2"]) if t]
        if not names:
            db.add_event(task_id, "未关联比赛也未填写队伍，跳过抓取；可在背景信息里手动补充选手名",
                         level="warning", stage="context")
            return
        set_progress(task_id, None, f"正在从 HLTV 抓取 {' / '.join(names)} 的信息...")
        built, warnings = await context_builder.build_from_team_names(hltv, names)

    for warning in warnings:
        db.add_event(task_id, warning, level="warning", stage="context")
    context.update(built)
    updates: Dict[str, Any] = {"context": context}
    match = built.get("match")
    if match:
        teams = built["teams"]
        if len(teams) == 2:
            updates.update(team1=teams[0]["name"], team2=teams[1]["name"])
        if match.get("unix_ms"):
            updates["match_time"] = match["unix_ms"] / 1000
    db.update_task(task_id, **updates)
    for team in built["teams"]:
        if team.get("players"):
            db.add_event(task_id, f"{team['name']}：{', '.join(team['players'])}", stage="context")


# ── ingest ────────────────────────────────────────────────────────────────────

def _download(task_id: int, url: str, settings: Dict[str, Any]) -> Path:
    import yt_dlp

    workdir = config.task_dir(task_id)
    for old in workdir.glob("source.*"):
        old.unlink()

    def hook(status: Dict[str, Any]) -> None:
        PipelineRunner.check_cancelled(task_id)
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            done = status.get("downloaded_bytes") or 0
            set_progress(task_id, done / total * 100 if total else None, "正在下载音频...")

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "ffmpeg_location": config.resolve_ffmpeg(settings),
    }
    proxy = config.proxy_or_none(settings)
    if proxy:
        options["proxy"] = proxy
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])
    files = list(workdir.glob("source.*"))
    if not files:
        raise RuntimeError("下载完成但没有找到音频文件")
    return files[0]


def stage_ingest(task_id: int) -> None:
    task = _task(task_id)
    settings = db.get_settings()
    if task["source_type"] == "live":
        raise RuntimeError("直播录制将在第四阶段实现，目前请在「任务信息」里上传采访音频")
    if task["source_type"] == "url":
        if not task["source_url"]:
            raise RuntimeError("未填写视频链接")
        source = _download(task_id, task["source_url"], settings)
        db.update_task(task_id, source_file=str(source))
    else:
        if not task["source_file"] or not Path(task["source_file"]).exists():
            raise RuntimeError("未上传音频文件")
        source = Path(task["source_file"])

    command = [config.resolve_ffmpeg(settings), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]
    if task["clip_start"] is not None:
        command += ["-ss", str(task["clip_start"])]
    if task["clip_end"] is not None:
        command += ["-to", str(task["clip_end"])]
    output = config.task_dir(task_id) / "audio.wav"
    command += ["-vn", "-ac", "1", "-ar", "16000", str(output)]
    set_progress(task_id, None, "正在转换音频格式...")
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 转换失败：{result.stderr.strip()[-300:]}")


# ── transcribe ────────────────────────────────────────────────────────────────

def _get_whisper(settings: Dict[str, Any]):
    global _whisper
    from .whisper import WhisperService

    cfg = settings["whisper"]
    with _model_lock:
        if _whisper is None or not _whisper.matches(cfg["model"], cfg["device"], cfg["compute_type"]):
            _whisper = WhisperService(cfg["model"], cfg["device"], cfg["compute_type"])
        return _whisper


def _get_diarizer(settings: Dict[str, Any]):
    global _diarizer
    from .diarization import DiarizationService

    cfg = settings["diarization"]
    with _model_lock:
        if _diarizer is None or (_diarizer.model_path, _diarizer.hf_token) != (cfg["model_path"], cfg["hf_token"]):
            _diarizer = DiarizationService(cfg["model_path"], cfg["hf_token"])
        return _diarizer


def _speaker_at(turns: List[Dict[str, Any]], start: float, end: float) -> Optional[str]:
    """时间重叠最多的说话人；完全不重叠时取最近的一段。"""
    best, best_overlap = None, 0.0
    for turn in turns:
        overlap = min(end, turn["end"]) - max(start, turn["start"])
        if overlap > best_overlap:
            best, best_overlap = turn["speaker"], overlap
    if best is None and turns:
        middle = (start + end) / 2
        best = min(turns, key=lambda t: min(abs(middle - t["start"]), abs(middle - t["end"])))["speaker"]
    return best


def split_by_speaker(segments: List[Dict[str, Any]], turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    按说话人重新切分转写片段。Whisper 的片段常常跨越提问和回答，
    这里用词级时间戳给每个词标注说话人，再把连续同一说话人的词合并成一段。
    """
    result: List[Dict[str, Any]] = []
    for seg in segments:
        words = seg.get("words") or []
        if not words:
            result.append({**seg, "speaker": _speaker_at(turns, seg["start"], seg["end"])})
            continue
        for word in words:
            speaker = _speaker_at(turns, word["start"], word["end"])
            if result and result[-1].get("speaker") == speaker and "words" in result[-1]:
                current = result[-1]
                current["end"] = word["end"]
                current["words"].append(word)
            else:
                result.append({"start": word["start"], "end": word["end"], "speaker": speaker, "words": [word]})
    for seg in result:
        if "words" in seg:
            seg["text"] = "".join(w["word"] for w in seg["words"]).strip()
    return [seg for seg in result if seg.get("text")]


def stage_transcribe(task_id: int) -> None:
    task = _task(task_id)
    settings = db.get_settings()
    audio = config.task_dir(task_id) / "audio.wav"
    if not audio.exists():
        raise RuntimeError("缺少 audio.wav，请从「获取音频」重跑")

    canonical, aliases = _names(task)
    names_for_prompt = canonical + [t for t in aliases.values() if t not in canonical]
    whisper = _get_whisper(settings)

    def on_progress(**update: Any) -> None:
        PipelineRunner.check_cancelled(task_id)
        set_progress(task_id, update.get("progress"), update.get("message", ""))

    cfg = settings["whisper"]
    diarize = settings["diarization"]["enabled"]
    result = whisper.transcribe(
        str(audio),
        language=cfg["language"] or None,
        progress_callback=on_progress,
        initial_prompt=prompts.whisper_prompt(names_for_prompt) or None,
        beam_size=int(cfg.get("beam_size") or 1),
        word_timestamps=diarize,
    )
    db.add_event(
        task_id,
        f"转写完成：{len(result['segments'])} 段，语言 {result['language']}，设备 {whisper.device}",
        stage="transcribe",
    )

    if diarize:
        set_progress(task_id, None, "正在进行说话人分离...")
        try:
            diarized = _get_diarizer(settings).diarize(str(audio), min_speakers=2, max_speakers=4)
            result["segments"] = split_by_speaker(result["segments"], diarized["segments"])
            db.add_event(
                task_id,
                f"说话人分离完成：{diarized['num_speakers']} 人，按说话人切分为 {len(result['segments'])} 段",
                stage="transcribe",
            )
        except Exception as error:
            # 说话人分离只是辅助，失败不阻塞流程
            db.add_event(task_id, f"说话人分离失败，已跳过：{error}", level="warning", stage="transcribe")

    write_text(task_id, "transcript.json", json.dumps(result, ensure_ascii=False, indent=1))


# ── clean / write ─────────────────────────────────────────────────────────────

def _llm_call(settings: Dict[str, Any], step: str, system: str, user: str) -> str:
    step_cfg = settings["llm"][step]
    provider = llm.find_provider(settings, step_cfg.get("provider_id", ""))
    return llm.complete(
        provider, system, user,
        model=step_cfg.get("model", ""),
        temperature=float(settings["llm"].get("temperature", 0.3)),
        proxy=config.proxy_or_none(settings),
    )


def stage_clean(task_id: int) -> None:
    task = _task(task_id)
    settings = db.get_settings()
    transcript = json.loads(read_text(task_id, "transcript.json"))
    canonical, aliases = _names(task)
    user = prompts.build_clean_prompt(transcript, task["context"], canonical, aliases)
    write_text(task_id, "prompt_clean.txt", user)
    set_progress(task_id, None, "LLM 正在清洗英文稿...")
    text = _llm_call(settings, "clean", prompts.CLEAN_SYSTEM, user)
    if text.strip() == "NO_INTERVIEW":
        raise RuntimeError("LLM 判断转写内容中没有采访，请检查音频或截取区间")
    write_text(task_id, "clean_en.txt", text)


def _parse_json_object(text: str) -> Dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("LLM 没有返回 JSON")
    return json.loads(match.group(0))


def resolve_names_with_llm(task_id: int, settings: Dict[str, Any], text: str, report: Dict[str, Any],
                           canonical: List[str]) -> str:
    """规则推断不了的词，结合上下文交给 LLM 判断；只接受规范名表里的名字。"""
    tokens = [s["token"] for s in report["suspects"]] + report["unknown"]
    contexts = token_contexts(text, tokens)
    if not contexts or not canonical:
        return text
    task = _task(task_id)
    user = prompts.build_name_resolution_prompt(contexts, task["context"], canonical)
    set_progress(task_id, None, "LLM 正在结合上下文核对名词...")
    try:
        answer = _parse_json_object(_llm_call(settings, "clean", prompts.NAME_RESOLVE_SYSTEM, user))
    except Exception as error:
        db.add_event(task_id, f"LLM 名词核对失败，已跳过：{error}", level="warning", stage="write")
        return text

    canonical_set = set(canonical)
    mapping = {t: v for t, v in answer.items() if t in contexts and isinstance(v, str) and v in canonical_set}
    text, counts = replace_tokens(text, mapping)
    for token, count in counts.items():
        report["replaced"].append({"from": token, "to": mapping[token], "method": "llm", "count": count})
    report["suspects"] = [s for s in report["suspects"] if s["token"] not in counts]
    report["unknown"] = [u for u in report["unknown"] if u not in counts]
    return text


def normalize_draft(task_id: int, use_llm: bool = True) -> Dict[str, Any]:
    """对中文稿做名词规范化，写出 draft_zh.txt 和 names_report.json。"""
    task = _task(task_id)
    canonical, aliases = _names(task)
    _, terms = _glossary()
    normalizer = NameNormalizer(canonical, aliases, known_words=terms.keys())
    text, report = normalizer.normalize(read_text(task_id, "draft_raw_zh.txt"))
    report_dict = report.to_dict()
    if use_llm and (report_dict["suspects"] or report_dict["unknown"]):
        text = resolve_names_with_llm(task_id, db.get_settings(), text, report_dict, sorted(normalizer.canonical))
    write_text(task_id, "draft_zh.txt", text)
    write_text(task_id, "names_report.json", json.dumps(report_dict, ensure_ascii=False, indent=1))
    return report_dict


def stage_write(task_id: int) -> None:
    task = _task(task_id)
    settings = db.get_settings()
    canonical, _ = _names(task)
    _, terms = _glossary()
    user = prompts.build_write_prompt(
        read_text(task_id, "clean_en.txt"), task["context"], canonical, terms,
        task["notes"] or "", settings["llm"].get("extra_instructions", ""),
    )
    write_text(task_id, "prompt_write.txt", user)
    set_progress(task_id, None, "LLM 正在翻译润色...")
    write_text(task_id, "draft_raw_zh.txt", _llm_call(settings, "write", prompts.WRITE_SYSTEM, user))

    report = normalize_draft(task_id)
    replaced = sum(item["count"] for item in report["replaced"])
    inferred = sum(item["count"] for item in report["replaced"] if item["method"] in ("phonetic", "skeleton", "llm"))
    message = f"名词规范化：替换 {replaced} 处（其中按发音或上下文推断 {inferred} 处）"
    if report["suspects"]:
        message += "；待核对：" + "、".join(f"{s['token']}→{'/'.join(s['candidates'])}" for s in report["suspects"])
    db.add_event(task_id, message, level="warning" if report["suspects"] else "info", stage="write")


# ── export / deliver ──────────────────────────────────────────────────────────

def docx_filename(task: Dict[str, Any]) -> str:
    date = time.strftime("%Y%m%d", time.localtime(task["created_at"]))
    safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", task["title"]).strip("_") or f"task{task['id']}"
    return f"{date}_{safe_title}.docx"


def find_docx(task_id: int) -> Optional[Path]:
    files = sorted(config.task_dir(task_id).glob("*.docx"))
    return files[0] if files else None


def stage_export(task_id: int) -> None:
    task = _task(task_id)
    workdir = config.task_dir(task_id)
    for old in workdir.glob("*.docx"):
        old.unlink()
    export_docx(read_text(task_id, "draft_zh.txt"), workdir / docx_filename(task))


def stage_deliver(task_id: int) -> None:
    task = _task(task_id)
    settings = db.get_settings()
    if not settings["feishu"].get("enabled"):
        db.add_event(task_id, "未启用飞书，跳过发送", stage="deliver")
        return
    docx = find_docx(task_id)
    if docx is None:
        raise RuntimeError("没有找到 Word 文件，请从「生成 Word」重跑")

    lines = [f"【初稿就绪】{task['title']}"]
    report_path = config.task_dir(task_id) / "names_report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("suspects"):
            lines.append("待核对名词：" + "、".join(
                f"{s['token']}（可能是 {'/'.join(s['candidates'])}）" for s in report["suspects"]
            ))
        if report.get("unknown"):
            lines.append("未收录的英文词：" + "、".join(report["unknown"][:20]))
    client = FeishuClient(settings["feishu"])
    client.send_text("\n".join(lines))
    client.send_file(docx)


def register(runner: PipelineRunner) -> None:
    runner.register("context", stage_context)
    runner.register("ingest", stage_ingest)
    runner.register("transcribe", stage_transcribe)
    runner.register("clean", stage_clean)
    runner.register("write", stage_write)
    runner.register("export", stage_export)
    runner.register("deliver", stage_deliver)
