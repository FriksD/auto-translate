"""直播录制公用部分的测试：HLTV / bo3.gg 解析、录音时间轴与截取、镜头时间轴、断流提醒。"""
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.pipeline import live, monitor
from backend.pipeline.recorder import SAMPLE_RATE, chunk_start
from backend.sources import bo3
from backend.sources import hltv_parse as P
from backend.sources.browser import is_challenge, is_hltv_page

FIXTURES = Path(__file__).parent / "fixtures" / "hltv"
BO3_FIXTURES = Path(__file__).parent / "fixtures" / "bo3"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def bo3_fixture(name: str):
    return json.loads((BO3_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))

class ParseLiveTest(unittest.TestCase):
    def test_live_page_has_scoreboard_and_map_status(self):
        html = fixture("match_live")
        match = P.parse_match(html)
        self.assertEqual((match["status"], match["best_of"]), ("live", 3))
        self.assertEqual([(m["name"], m["status"], m["winner"]) for m in match["maps"]],
                         [("Ancient", "finished", 1), ("Mirage", "live", None), ("Anubis", "upcoming", None)])
        self.assertEqual(P.parse_scoreboard(html), {
            "round": 4, "map": "Mirage", "ct": {"team": "fnatic", "score": 2}, "t": {"team": "MIBR", "score": 1}})

    def test_only_first_unfinished_map_is_live(self):
        # 第一张图还在打时，第二张图（非决胜的选图）也带 played
        html = fixture("match_live").replace("results-left won pick", "results-left tie pick", 1).replace(
            "results-right lost", "results-right tie", 1)
        match = P.parse_match(html)
        self.assertEqual([(m["name"], m["status"]) for m in match["maps"]],
                         [("Ancient", "live"), ("Mirage", "upcoming"), ("Anubis", "upcoming")])

    def test_finished_page_has_no_scoreboard(self):
        html = fixture("match_over_bo3")
        match = P.parse_match(html)
        self.assertEqual(match["status"], "over")
        self.assertEqual([m["winner"] for m in match["maps"]], [2, 2, None])
        self.assertIsNone(P.parse_scoreboard(html))


class ChallengeTest(unittest.TestCase):
    def test_challenge_page_detected_in_any_language(self):
        page = fixture("challenge")
        self.assertTrue(is_challenge("Just a moment...", page))
        self.assertTrue(is_challenge("请稍候…", page))
        self.assertTrue(is_challenge("", page))  # 只看页面内容
        self.assertFalse(is_hltv_page(page))
        for name in ("matches", "match_live", "match_over_bo3", "team_vitality"):
            self.assertFalse(is_challenge("HLTV.org", fixture(name)), name)
        self.assertTrue(is_hltv_page('<!DOCTYPE html><html lang="en" data-client-country-iso="SG">'))

class Bo3Test(unittest.TestCase):
    def test_finished_match(self):
        match = bo3.normalize_match(bo3_fixture("match_over"))
        self.assertEqual((match["status"], match["best_of"], match["team1"]["name"]), ("over", 3, "fnatic"))
        self.assertEqual([(m["name"], m["status"], m["winner"], m["team1_score"], m["team2_score"]) for m in match["maps"]],
                         [("Ancient", "finished", 1, 13, 9), ("Mirage", "finished", 1, 13, 7)])
        official = monitor.official_stream(match["streams"])
        self.assertEqual((official["url"], official["country"]), ("https://kick.com/starladder", "Other"))

    def test_swapped_order_and_three_maps(self):
        match = bo3.normalize_match(bo3_fixture("match_over_3maps"), swapped=True)
        self.assertEqual((match["team1"]["name"], match["team2"]["name"]), ("Astralis", "Eternal Fire"))
        self.assertEqual([m["winner"] for m in match["maps"]], [1, 2, 2])
        self.assertEqual((match["maps"][0]["team1_score"], match["maps"][0]["team2_score"]), (13, 10))
        self.assertEqual((match["team1"]["score"], match["team2"]["score"]), (1, 2))

    def test_find_match_by_names_and_time(self):
        results = bo3_fixture("list")["results"]
        start = bo3.parse_time("2026-10-30T12:00:00Z")
        found = bo3.find_match(results, "Nemiga", "Vitality", start)
        self.assertEqual((found[0]["slug"], found[1]), ("nemiga-vs-vitality-30-10-2026", False))
        self.assertTrue(bo3.find_match(results, "Team Vitality", "Nemiga Gaming", start + 3600)[1])
        self.assertIsNone(bo3.find_match(results, "Nemiga", "Vitality", start + 2 * 86400))
        self.assertIsNone(bo3.find_match(results, "Nemiga", "Falcons", start))

    def test_team_name_normalization(self):
        self.assertTrue(bo3.same_team("Falcons", ["Team Falcons"]))
        self.assertTrue(bo3.same_team("MOUZ", ["mouz cs2"]))
        self.assertTrue(bo3.same_team("Natus Vincere", ["Natus Vincere"]))
        self.assertFalse(bo3.same_team("BIG", ["BIG Academy"]) and bo3.same_team("G2", ["G2 Ares"]))
        self.assertEqual(bo3.map_display_name("de_dust2"), "Dust2")


class NotLiveTest(unittest.TestCase):
    def test_faster_than_realtime_means_replay(self):
        from backend.pipeline.recorder import is_faster_than_live, recorded_since
        self.assertFalse(is_faster_than_live(60, 30))    # 刚连上，不判断
        self.assertFalse(is_faster_than_live(75, 60))    # 直播：开头多下载了直播点之前的几个分片
        self.assertTrue(is_faster_than_live(160, 60))    # 回放：实测约 2.7 倍速
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            now = float(int(time.time()))
            write_chunk(directory, now - 100, 30, 1)      # 上一次连接的
            write_chunk(directory, now, 30, 1)
            write_chunk(directory, now + 10, 30, 1)
            self.assertAlmostEqual(recorded_since(directory, now), 60, places=3)

def write_chunk(directory: Path, start: float, seconds: float, value: int) -> Path:
    path = directory / time.strftime("c_%Y%m%d_%H%M%S.wav", time.localtime(start))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(value.to_bytes(2, "little", signed=True) * int(seconds * SAMPLE_RATE))
    return path


class TimelineTest(unittest.TestCase):
    def test_timeline_chains_chunks_and_cut_fills_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            origin = float(int(time.time()))
            write_chunk(directory, origin, 60, 1)
            write_chunk(directory, origin + 59, 60, 2)   # 文件名时刻只精确到秒，实际紧接上一段
            write_chunk(directory, origin + 200, 30, 3)  # 断流重连
            chunks = sorted(directory.glob("c_*.wav"))
            timeline = live.chunk_timeline(chunks, chunk_start(chunks[0]))
            self.assertEqual([round(begin) for _, begin, _ in timeline], [0, 60, 200])

            out = directory / "cut.wav"
            self.assertAlmostEqual(live.cut_audio(timeline, 50, 210, out), 160, places=3)
            with wave.open(str(out), "rb") as wav:
                data = wav.readframes(wav.getnframes())

            def sample(t: float) -> int:
                index = int(t * SAMPLE_RATE) * 2
                return int.from_bytes(data[index:index + 2], "little", signed=True)
            # 50 秒起：第一段 → 第二段 → 断流期间的静音 → 重连后的第三段
            self.assertEqual([sample(5), sample(20), sample(100), sample(155)], [1, 2, 0, 3])

    def test_line_end(self):
        lines = [{"start": t, "end": t + 20, "text": "x"} for t in range(0, 3000, 20)]
        self.assertEqual(live.line_end_at(lines, 160), 180)


def shots_from(spec):
    """spec: [(秒, 标签), ...] 每 5 秒一帧"""
    return [{"file": f"f{t}.jpg", "t": float(t), "label": label} for t, label in spec]


class ShotTimelineTest(unittest.TestCase):
    def test_interview_run_tolerates_short_cutaway(self):
        spec = [(t, "desk") for t in range(600, 700, 5)]
        spec += [(t, "interview") for t in range(700, 800, 5)]
        spec += [(800, "game"), (805, "game")]                     # 插了 10 秒回放
        spec += [(t, "interview") for t in range(810, 925, 5)]
        spec += [(t, "desk") for t in range(925, 1000, 5)]
        runs = live.interview_runs(shots_from(spec))
        self.assertEqual(runs, [{"start": 700.0, "end": 925.0, "open": False}])

    def test_run_still_open_until_cut_away_long_enough(self):
        spec = [(t, "interview") for t in range(700, 900, 5)] + [(900, "desk"), (905, "desk")]
        self.assertTrue(live.interview_runs(shots_from(spec))[0]["open"])
        self.assertIsNone(live.matching_run(live.interview_runs(shots_from(spec)), 400))
        self.assertEqual(live.matching_run(live.interview_runs(shots_from(spec)), 690)["start"], 700)

    def test_short_flash_is_ignored(self):
        spec = [(t, "desk") for t in range(0, 100, 5)] + [(100, "interview"), (105, "interview")]
        spec += [(t, "desk") for t in range(110, 200, 5)]
        self.assertEqual(live.interview_runs(shots_from(spec)), [])

    def test_sheet_and_labels(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            frames = []
            for i in range(9):
                path = Path(tmp) / f"f_{i}.jpg"
                Image.new("RGB", (320, 180), (i * 20, 0, 0)).save(path)
                frames.append((path, 600.0 + i * 5))
            sheet = Image.open(__import__("io").BytesIO(live.make_sheet(frames)))
            self.assertEqual(sheet.size, (960, 540))
        self.assertEqual(live.parse_shot_labels({"labels": ["Interview", "desk", "ad"]}, 3), ["interview", "desk", "other"])
        with self.assertRaises(ValueError):
            live.parse_shot_labels({"labels": ["desk"]}, 3)

class RecorderOutageTest(unittest.TestCase):
    def test_one_error_per_outage(self):
        from backend.pipeline import recorder

        events = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rec = recorder.Recorder("https://twitch.tv/x", Path(tmp.name) / "chunks", "ffmpeg", None,
                                on_event=lambda level, message: events.append(level))
        attempts = []

        def fail_fast():
            attempts.append(1)
            if len(attempts) == 6:
                rec.down_since = None  # 中间稳定录过一阵（_record_once 里判定恢复）
            if len(attempts) >= 12:
                rec._stop.set()
            return False

        with unittest.mock.patch.object(recorder, "RETRY_DELAYS", [0]), \
                unittest.mock.patch.object(rec, "_record_once", fail_fast):
            rec._run()
        # 第 3 次失败报错，之后不再报；恢复后再断（失败次数仍在累计）立刻再报一次
        self.assertEqual([i for i, level in enumerate(events) if level == "error"], [2, 5])


if __name__ == "__main__":
    unittest.main()
