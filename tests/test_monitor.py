"""直播间监视：开播探测、链接整理、采访触发与定位、截取成任务、旧录音清理的测试。"""
import json
import sys
import tempfile
import time
import unittest
import unittest.mock
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import config, db
from backend.pipeline import monitor
from backend.pipeline.recorder import SAMPLE_RATE
from backend.sources import stream_status

PHRASES = ["joined by", "congratulations"]


def iv(start, end=None, ended=False, names=("ZywOo",), team="Vitality", live=True):
    return {"start": start, "end": end, "ended": ended, "interviewees": list(names), "team": team, "live": live,
            "note": ""}


def write_chunk(directory: Path, start: float, seconds: float, value: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / time.strftime("c_%Y%m%d_%H%M%S.wav", time.localtime(start))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(value.to_bytes(2, "little", signed=True) * int(seconds * SAMPLE_RATE))
    return path


class StreamStatusTest(unittest.TestCase):
    def test_platform_and_proxy_defaults(self):
        self.assertEqual(stream_status.platform("https://live.bilibili.com/35"), "bilibili")
        self.assertEqual(stream_status.platform("https://www.twitch.tv/eslcs"), "twitch")
        self.assertFalse(stream_status.default_proxy("https://live.bilibili.com/34"))
        self.assertTrue(stream_status.default_proxy("https://www.youtube.com/@ESLCS/live"))
        self.assertEqual(stream_status.bilibili_room("https://live.bilibili.com/35?spm=x"), "35")
        self.assertEqual(stream_status.bilibili_room("https://live.bilibili.com/h5/21622811"), "21622811")
        self.assertIsNone(stream_status.bilibili_room("https://www.twitch.tv/eslcs"))

    def test_bilibili_only_status_1_is_live(self):
        # 副舞台未开播（0）时 streamlink 仍然给流；轮播（2）也不算
        for code, live in ((0, False), (1, True), (2, False)):
            parsed = stream_status.parse_bilibili({"code": 0, "data": {"live_status": code, "title": "IEM北京"}})
            self.assertEqual(parsed["live"], live)
        with self.assertRaises(RuntimeError):
            stream_status.parse_bilibili({"code": 1, "message": "房间不存在"})

    def test_streamlink_json(self):
        self.assertTrue(stream_status.parse_streamlink_json(json.dumps({"streams": {"best": {}}}))["live"])
        offline = json.dumps({"error": "No playable streams found on this URL: https://www.twitch.tv/x"})
        self.assertFalse(stream_status.parse_streamlink_json(offline)["live"])
        with self.assertRaises(RuntimeError):
            stream_status.parse_streamlink_json(json.dumps({"error": "Unable to open URL: timeout"}))

    def test_probe_errors_are_unknown(self):
        with unittest.mock.patch.object(stream_status, "probe_bilibili", side_effect=OSError("network")):
            self.assertIsNone(stream_status.probe("https://live.bilibili.com/35")["live"])


class StreamsTest(unittest.TestCase):
    def test_normalize(self):
        streams = monitor.normalize_streams([
            {"url": " https://live.bilibili.com/35 ", "label": "主舞台"},
            {"url": "https://live.bilibili.com/35/"},  # 重复
            {"url": "https://www.youtube.com/@ESLCS/live", "proxy": False},
            {"url": ""},
        ])
        self.assertEqual([s["url"] for s in streams], ["https://live.bilibili.com/35", "https://www.youtube.com/@ESLCS/live"])
        self.assertEqual(streams[0], {"url": "https://live.bilibili.com/35", "label": "主舞台", "proxy": False,
                                      "language": ""})
        self.assertFalse(streams[1]["proxy"])
        self.assertNotIn("language", streams[1])
        with self.assertRaises(ValueError):
            monitor.normalize_streams([{"url": f"https://live.bilibili.com/{n}"} for n in (1, 2, 3)])

    def test_is_on(self):
        now = 1000.0
        self.assertTrue(monitor.is_on({"enabled": True, "windows": []}, now))
        self.assertTrue(monitor.is_on({"enabled": False, "windows": [{"start": 900, "end": 1100}]}, now))
        self.assertFalse(monitor.is_on({"enabled": False, "windows": [{"start": 1100, "end": 1200}]}, now))


class TriggerTest(unittest.TestCase):
    def test_phrase_or_camera_after_seen(self):
        lines = [{"start": 10, "end": 15, "text": "Congratulations on the win"},
                 {"start": 100, "end": 105, "text": "great round there"},
                 {"start": 200, "end": 205, "text": "I'm joined by ropz"}]
        self.assertEqual(monitor.find_trigger(lines, [], 0, PHRASES), 10)
        self.assertEqual(monitor.find_trigger(lines, [], 15, PHRASES), 200)
        runs = [{"start": 150, "end": 180, "open": False}]
        self.assertEqual(monitor.find_trigger(lines, runs, 15, PHRASES), 150)
        self.assertIsNone(monitor.find_trigger(lines, runs, 210, PHRASES))
        # 还开着的采访镜头段：起点早于 seen_until 也算
        self.assertEqual(monitor.find_trigger([], [{"start": 150, "end": 230, "open": True}], 210, PHRASES), 150)

    def test_scan_window_keeps_interview_start(self):
        lines = [{"start": t, "end": t + 10, "text": "x"} for t in range(0, 3000, 10)]
        window = monitor.scan_window(lines, 0)
        self.assertGreaterEqual(window[0]["end"], 3000 - monitor.SCAN_WINDOW)
        window = monitor.scan_window(lines, 0, keep_from=1000)
        self.assertEqual(window[0]["start"], 990)
        self.assertEqual(monitor.scan_window(lines, 2900)[0]["start"], 2890)

    def test_parse_interviews(self):
        lines = [{"start": 600, "end": 640, "text": "thanks"}]
        parsed = monitor.parse_interviews({"interviews": [
            {"start": "10:00", "end": "10:00", "ended": True, "interviewees": "ZywOo", "team": "Vitality"},
            {"start": "05:00", "live": False, "interviewees": []},
            {"start": None},
        ]}, lines)
        self.assertEqual([p["start"] for p in parsed], [300, 600])
        self.assertFalse(parsed[0]["live"])
        self.assertEqual(parsed[1]["end"], 640)  # 换成最后一句的结束
        self.assertEqual(parsed[1]["interviewees"], ["ZywOo"])


class StepDetectionTest(unittest.TestCase):
    def test_no_interview_twice_dismisses_trigger(self):
        det = {"trigger_at": 100}
        det, caps, _ = monitor.step_detection(det, [], [], 400, 0)
        self.assertEqual(det["trigger_at"], 100)
        det, caps, _ = monitor.step_detection(det, [], [], 460, 0)
        self.assertIsNone(det["trigger_at"])
        self.assertEqual(det["seen_until"], 460)
        self.assertEqual(caps, [])

    def test_open_camera_keeps_trigger(self):
        runs = [{"start": 100, "end": 400, "open": True}]
        det = {"trigger_at": 100}
        for until in (400, 460, 520):
            det, _, _ = monitor.step_detection(det, [], runs, until, 0)
        self.assertEqual(det["trigger_at"], 100)

    def test_end_needs_confirmation(self):
        det, caps, events = monitor.step_detection({}, [iv(100)], [], 300, 1000)
        self.assertEqual(det["active"]["start"], 100)
        self.assertIn("检测到采访开始：ZywOo", events[0][1])
        det, caps, _ = monitor.step_detection(det, [iv(100, 400, True)], [], 500, 1030)
        self.assertEqual(caps, [])
        self.assertEqual(det["active"]["pending_end"], 400)
        det, caps, _ = monitor.step_detection(det, [iv(100, 410, True)], [], 560, 1060)
        self.assertEqual(caps, [{"start": 100, "end": 410, "source": "auto", "interviewees": ["ZywOo"],
                                 "team": "Vitality"}])
        self.assertEqual(det["floor"], 410)
        self.assertIsNone(det["active"])

    def test_end_too_close_to_transcript_end_is_not_counted(self):
        det, _, _ = monitor.step_detection({}, [iv(100, 400, True)], [], 420, 0)
        self.assertIsNone(det["active"]["pending_end"])

    def test_next_interview_splits_immediately(self):
        # 一个接一个采访：第二个人开始就确定第一段结束，镜头一直是采访画面也不影响
        runs = [{"start": 90, "end": 700, "open": True}]
        det, caps, _ = monitor.step_detection(
            {}, [iv(100, 300, True), iv(305, names=("apEX",))], runs, 350, 0)
        self.assertEqual(len(caps), 1)
        self.assertEqual((caps[0]["start"], caps[0]["end"]), (100, 300))
        self.assertEqual(det["trigger_at"], 305)
        self.assertTrue(det["recheck"])
        # 下一次：截过的那段不再出现在结果里（在 floor 之前），开始跟踪第二个人
        det, caps, events = monitor.step_detection(
            det, [iv(100, 300, True), iv(305, names=("apEX",))], runs, 420, 0)
        self.assertEqual(caps, [])
        self.assertEqual(det["active"]["start"], 305)
        self.assertEqual(det["active"]["interviewees"], ["apEX"])

    def test_camera_still_on_blocks_end(self):
        runs = [{"start": 95, "end": 600, "open": True}]
        det, _, _ = monitor.step_detection({}, [iv(100, 400, True)], runs, 600, 0)
        det, caps, _ = monitor.step_detection(det, [iv(100, 400, True)], runs, 660, 0)
        self.assertEqual(caps, [])

    def test_camera_cut_away_captures(self):
        runs = [{"start": 95, "end": 420, "open": False}]
        det, caps, _ = monitor.step_detection({}, [iv(100)], runs, 500, 0)
        self.assertEqual(len(caps), 1)
        self.assertEqual((caps[0]["start"], caps[0]["end"], caps[0]["source"]), (95, 420, "camera"))

    def test_recorded_feature_is_ignored_and_retracts(self):
        det, _, _ = monitor.step_detection({}, [iv(100)], [], 300, 0)
        det, caps, events = monitor.step_detection(det, [iv(100, live=False)], [], 360, 0)
        self.assertIsNone(det["active"])
        self.assertEqual(caps, [])
        self.assertIn("撤回", events[0][1])

    def test_before_floor_is_ignored(self):
        det, caps, _ = monitor.step_detection({"floor": 500}, [iv(100, 300, True)], [], 700, 0)
        self.assertIsNone(det["active"])
        self.assertEqual(caps, [])


class StoredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        db.init_db()

    def tearDown(self):
        db._conn.close()
        self.tmp.cleanup()

    def test_monitor_crud(self):
        created = db.create_monitor(title="IEM 北京", streams=monitor.normalize_streams(
            [{"url": "https://live.bilibili.com/35"}]), enabled=True)
        self.assertTrue(created["enabled"])
        self.assertEqual(created["streams"][0]["proxy"], False)
        updated = db.update_monitor(created["id"], windows=[{"start": 1, "end": 2}], enabled=False)
        self.assertEqual(updated["windows"], [{"start": 1, "end": 2}])
        self.assertFalse(updated["enabled"])
        db.add_monitor_event(created["id"], "开始监视")
        self.assertEqual(db.recent_monitor_events(created["id"])[0]["message"], "开始监视")
        db.delete_monitor(created["id"])
        self.assertIsNone(db.get_monitor(created["id"]))

    def test_capture_creates_task(self):
        url = "https://live.bilibili.com/35"
        created = db.create_monitor(title="IEM 北京", event_name="IEM Beijing 2026",
                                    streams=monitor.normalize_streams([{"url": url}]), enabled=True)
        manager = unittest.mock.Mock()
        session = monitor.StreamSession(manager, created["id"], created["streams"][0])
        origin = time.mktime(time.strptime("2026-11-08 12:00:00", "%Y-%m-%d %H:%M:%S"))
        session.files.patch(origin=origin)
        for index in range(4):
            write_chunk(session.files.chunk_dir, origin + index * 30, 30, index + 1)
        session.files.still_dir.mkdir(parents=True)
        for offset in (5, 60):
            (session.files.still_dir / time.strftime("f_%Y%m%d_%H%M%S.jpg", time.localtime(origin + offset))).write_bytes(b"x")
        with open(session.files.transcript_path, "w", encoding="utf-8") as fh:
            for t in (10, 40, 70, 115):
                fh.write(json.dumps({"start": t, "end": t + 5, "text": f"line {t}"}) + "\n")

        with unittest.mock.patch.object(monitor.cards, "track"):
            task = session.capture({"start": 40, "end": 80, "source": "auto", "interviewees": ["ZywOo"],
                                    "team": "Vitality"})
        manager.enqueue.assert_called_once_with(task["id"])
        self.assertEqual(task["source_type"], "live")
        self.assertEqual(task["team1"], "Vitality")
        self.assertIn("ZywOo", task["title"])
        self.assertEqual(task["context"]["monitor"]["id"], created["id"])
        workdir = config.task_dir(task["id"])
        with wave.open(str(workdir / "interview.wav"), "rb") as wav:
            self.assertAlmostEqual(wav.getnframes() / SAMPLE_RATE, 60, places=2)  # 前后各 10 秒余量
        state = json.loads((workdir / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(state["recording"]["started_at"], origin)
        self.assertEqual(state["interview"]["start"], 40)
        self.assertEqual(state["detection"]["interviewee"], "ZywOo")
        self.assertEqual(len(list((workdir / "stills").glob("*.jpg"))), 1)  # 只要采访前后的
        lines = [json.loads(l) for l in (workdir / "live_transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([l["start"] for l in lines], [40, 70])

    def test_prune_and_reset(self):
        files = monitor.StreamFiles(7, "https://live.bilibili.com/34")
        now = time.time()
        origin = now - 8 * 3600
        files.patch(origin=origin, transcribed=[])
        old = write_chunk(files.chunk_dir, origin, 30, 1)
        new = write_chunk(files.chunk_dir, now - 60, 30, 2)
        files.patch(transcribed=[old.name, new.name])
        with open(files.transcript_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"start": 0, "end": 5, "text": "old"}) + "\n")
            fh.write(json.dumps({"start": 8 * 3600 - 60, "end": 8 * 3600 - 55, "text": "new"}) + "\n")
        files.prune(now - 6 * 3600)
        self.assertEqual([c.name for c in files.chunks()], [new.name])
        self.assertEqual([l["text"] for l in files.transcript()], ["new"])
        self.assertEqual(files.load()["transcribed"], [new.name])
        files.reset(now)
        self.assertEqual(files.transcript(), [])
        self.assertEqual(files.load()["origin"], now)

    def test_reconcile_starts_and_stops_sessions(self):
        created = db.create_monitor(title="IEM 北京", streams=monitor.normalize_streams(
            [{"url": "https://live.bilibili.com/35"}, {"url": "https://live.bilibili.com/34"}]), enabled=True)
        manager = monitor.MonitorManager()
        with unittest.mock.patch.object(monitor.StreamSession, "start"), \
                unittest.mock.patch.object(monitor.StreamSession, "stop"):
            manager.reconcile()
            self.assertEqual(len(manager.sessions), 2)
            db.update_monitor(created["id"], streams=monitor.normalize_streams([{"url": "https://live.bilibili.com/35"}]))
            manager.reconcile()
            self.assertEqual(len(manager.sessions), 1)
            db.update_monitor(created["id"], enabled=False)
            manager.reconcile()
            self.assertEqual(manager.sessions, {})


class OfflineTest(unittest.TestCase):
    """未开播时不录、不提醒；开播后开始录；B 站下播就停录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config.use_data_dir(Path(self.tmp.name))
        db.init_db()
        created = db.create_monitor(title="IEM 北京", streams=monitor.normalize_streams(
            [{"url": "https://live.bilibili.com/34"}]), enabled=True)
        self.session = monitor.StreamSession(unittest.mock.Mock(), created["id"], created["streams"][0])

    def tearDown(self):
        db._conn.close()
        self.tmp.cleanup()

    def test_waits_while_offline_then_records(self):
        with unittest.mock.patch.object(stream_status, "probe", return_value={"live": False, "title": "副舞台"}), \
                unittest.mock.patch.object(monitor.StreamSession, "_start_recorder") as start:
            self.session._watch_live()
            start.assert_not_called()
            self.assertEqual(self.session.files.load()["status"], "offline")
            self.session._watch_live()  # 还没到下次探测时间
        with unittest.mock.patch.object(stream_status, "probe", return_value={"live": True, "title": "副舞台"}) as probe, \
                unittest.mock.patch.object(monitor.StreamSession, "_start_recorder") as start:
            self.session._watch_live()
            probe.assert_not_called()
            self.session._last_probe = 0
            self.session._watch_live()
            start.assert_called_once_with("副舞台")

    def test_bilibili_going_offline_stops_recording(self):
        recorder = unittest.mock.Mock(running=True)
        self.session.recorder = recorder
        with unittest.mock.patch.object(stream_status, "probe", return_value={"live": False, "title": ""}):
            self.session._watch_live()
        recorder.stop.assert_called_once()
        self.assertIsNone(self.session.recorder)
        self.assertEqual(self.session.files.load()["status"], "offline")


if __name__ == "__main__":
    unittest.main()
