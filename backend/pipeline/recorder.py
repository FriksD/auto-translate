"""
直播录音：streamlink 拉流 → ffmpeg 转成 16kHz 单声道 → 按固定时长切成 wav 分段。

- 分段文件名带开始时刻（chunks/c_YYYYmmdd_HHMMSS.wav），断流重连后时间轴仍然连续可算。
- 拉最低画质（144p/160p）：同时按 frame_interval 秒存一帧画面（frames/f_YYYYmmdd_HHMMSS.jpg），
  用来按镜头判断采访起止。不要画面时（frame_interval=0）Twitch 只拉 audio_only。
- 拉流进程退出（断流、主播下播又开播）时自动重连；连续失败多次会回调通知，但不会放弃，直到被停止。
- 录到的音频明显比实际经过的时间多，说明拉到的是回放而不是直播（下载比实时快），
  立刻停掉这个直播源，回调 on_not_live 换一个。
"""
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

CHUNK_PATTERN = "c_%Y%m%d_%H%M%S.wav"
CHUNK_TIME_FORMAT = "c_%Y%m%d_%H%M%S"
FRAME_PATTERN = "f_%Y%m%d_%H%M%S.jpg"
FRAME_TIME_FORMAT = "f_%Y%m%d_%H%M%S"
FRAME_WIDTH = 320
SAMPLE_RATE = 16000
RETRY_DELAYS = [1, 5, 10, 20, 30]  # 第一次立刻重连：Kick 偶尔断流，每秒都可能是采访内容
NOTIFY_AFTER_FAILURES = 3
STABLE_SECONDS = 60
# 回放检测：连接 LIVE_CHECK_AFTER 秒后，录到的音频比经过的时间多出 LIVE_SURPLUS_SECONDS 以上就不是直播。
# 直播刚连上时会先下载直播点之前的几个分片（Twitch/Kick 约 6 秒，YouTube 约 15 秒），所以留出余量
LIVE_CHECK_AFTER = 45
LIVE_SURPLUS_SECONDS = 40
LIVE_CHECK_INTERVAL = 5


def recorded_since(chunk_dir: Path, since: float) -> float:
    """since 时刻之后开始的分段里已经写了多少秒音频（包括正在写的那段）。"""
    return sum(chunk_duration(c) for c in chunk_dir.glob("c_*.wav") if chunk_start(c) >= since - 1)


def is_faster_than_live(recorded: float, elapsed: float) -> bool:
    return elapsed >= LIVE_CHECK_AFTER and recorded - elapsed > LIVE_SURPLUS_SECONDS


def chunk_start(path: Path) -> float:
    """分段的开始时刻（本地时间写在文件名里）。"""
    return time.mktime(time.strptime(path.stem, CHUNK_TIME_FORMAT))


def frame_time(path: Path) -> float:
    """画面的时刻（写入时的本地时间，和直播实时对应）。"""
    return time.mktime(time.strptime(path.stem, FRAME_TIME_FORMAT))


def _data_offset(path: Path) -> int:
    with open(path, "rb") as fh:
        head = fh.read(512)
    index = head.find(b"data", 12)
    return index + 8 if index >= 0 else 44


def read_pcm(path: Path) -> bytes:
    """分段的 16bit PCM 数据。不信任 wav 头里的长度：被强杀的分段头部没有回填。"""
    data = path.read_bytes()[_data_offset(path):]
    return data[:len(data) // 2 * 2]


def chunk_duration(path: Path) -> float:
    return max(0, path.stat().st_size - _data_offset(path)) / (SAMPLE_RATE * 2)


def build_commands(url: str, output_pattern: Path, ffmpeg: str, proxy: Optional[str],
                   chunk_seconds: int, frame_dir: Optional[Path] = None, frame_interval: int = 0) -> List[List[str]]:
    streamlink = [sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "info",
                  "--retry-open", "3", "--twitch-disable-ads"]
    if proxy:
        streamlink += ["--http-proxy", proxy]
    frames = bool(frame_dir and frame_interval > 0)
    streamlink += [url, "worst" if frames else "audio_only,worst"]
    ffmpeg_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-map", "0:a:0", "-vn", "-ac", "1",
                  "-ar", str(SAMPLE_RATE), "-f", "segment", "-segment_time", str(chunk_seconds),
                  # 每个包都落盘：进程被强杀（服务重启）时只丢最后一点，否则会丢掉缓冲里的 256KB（8 秒）以上
                  "-segment_format_options", "flush_packets=1",
                  "-strftime", "1", str(output_pattern)]
    if frames:
        ffmpeg_cmd += ["-map", "0:v:0", "-an", "-vf", f"fps=1/{frame_interval},scale={FRAME_WIDTH}:-2",
                       "-q:v", "5", "-f", "image2", "-strftime", "1", str(frame_dir / FRAME_PATTERN)]
    return [streamlink, ffmpeg_cmd]


class Recorder:
    def __init__(self, url: str, chunk_dir: Path, ffmpeg: str, proxy: Optional[str],
                 chunk_seconds: int = 60, on_event: Optional[Callable[[str, str], None]] = None,
                 on_not_live: Optional[Callable[[str, float], Optional[str]]] = None,
                 frame_interval: int = 0):
        """
        Args:
            on_event: 回调 (level, message)，用于写任务日志和发通知
            on_not_live: 回调 (url, connected_at)，发现拉到的是回放时调用；返回换用的直播源地址，
                返回 None 则停止录制，等人工指定直播源
        """
        self.url = url
        self.chunk_dir = chunk_dir
        self.ffmpeg = ffmpeg
        self.proxy = proxy
        self.chunk_seconds = chunk_seconds
        self.on_event = on_event or (lambda level, message: None)
        self.on_not_live = on_not_live
        self.frame_interval = frame_interval
        self.frame_dir = chunk_dir.parent / "frames"
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._procs: List[subprocess.Popen] = []
        self._thread: Optional[threading.Thread] = None
        self.connected_since: Optional[float] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"recorder-{self.chunk_dir.parent.name}", daemon=True)
        self._thread.start()

    def switch(self, url: str) -> None:
        """换一个直播源继续录（时间轴不变）。"""
        self.url = url
        self._restart.set()
        self._kill()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=15)

    def completed_chunks(self) -> List[Path]:
        """已经写完的分段；录制中时最新的一个还在写，不算。"""
        chunks = sorted(self.chunk_dir.glob("c_*.wav"))
        if self.running and chunks:
            chunks = chunks[:-1]
        return [c for c in chunks if c.stat().st_size > 44]

    # ── 录制线程 ──────────────────────────────────────────────────────────────

    def _kill(self) -> None:
        # 只停 streamlink：ffmpeg 读到 EOF 后会正常写完最后一个分段，超时才强杀
        procs = list(self._procs)
        if not procs:
            return
        try:
            procs[0].terminate()
        except Exception:
            pass
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            self._restart.clear()
            started = time.time()
            not_live = self._record_once()
            if self._stop.is_set():
                break
            if not_live:
                replacement = self.on_not_live(self.url, started) if self.on_not_live else None
                if not replacement:
                    break
                self.url = replacement
                failures = 0
                continue
            if self._restart.is_set():
                self.on_event("info", f"已切换直播源：{self.url}")
                failures = 0
                continue
            failures = 0 if time.time() - started > STABLE_SECONDS else failures + 1
            delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
            message = f"直播流中断，{delay} 秒后重连" + (f"（连续失败 {failures} 次）" if failures else "")
            self.on_event("error" if failures == NOTIFY_AFTER_FAILURES else "warning", message)
            self._stop.wait(delay)

    def _record_once(self) -> bool:
        """录一次，直到拉流结束或被停止。返回是否因为"不是直播"被停掉。"""
        stream_cmd, ffmpeg_cmd = build_commands(
            self.url, self.chunk_dir / CHUNK_PATTERN, self.ffmpeg, self.proxy, self.chunk_seconds,
            self.frame_dir, self.frame_interval)
        log_path = self.chunk_dir.parent / "recorder.log"
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {self.url}\n")
            log.flush()
            try:
                streamlink = subprocess.Popen(stream_cmd, stdout=subprocess.PIPE, stderr=log,
                                              creationflags=creationflags)
                ffmpeg = subprocess.Popen(ffmpeg_cmd, stdin=streamlink.stdout, stdout=subprocess.DEVNULL,
                                          stderr=log, creationflags=creationflags)
            except Exception as error:
                log.write(f"启动失败：{error}\n")
                logger.exception("录音进程启动失败")
                return False
            # 让 streamlink 在 ffmpeg 退出时收到 SIGPIPE
            streamlink.stdout.close()
            self._procs = [streamlink, ffmpeg]
            connected = self.connected_since = time.time()
            not_live = False
            try:
                while ffmpeg.poll() is None:
                    try:
                        ffmpeg.wait(timeout=LIVE_CHECK_INTERVAL)
                    except subprocess.TimeoutExpired:
                        recorded = recorded_since(self.chunk_dir, connected)
                        if is_faster_than_live(recorded, time.time() - connected):
                            log.write(f"不是直播：{time.time() - connected:.0f} 秒内录到 {recorded:.0f} 秒音频\n")
                            log.flush()
                            not_live = True
                            self._kill()
                            break
                streamlink.wait(timeout=10)
            except Exception:
                self._kill()
            finally:
                self._procs = []
                self.connected_since = None
            return not_live
