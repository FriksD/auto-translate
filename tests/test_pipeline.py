"""
后处理流水线端到端测试：上传音频 → 转写（替身）→ 两段 LLM（替身）→ 名词规范化 → Word。
Whisper 和 LLM 用替身，ffmpeg 使用真实程序。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="atn-test-")
os.environ["ATN_DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from backend import app as app_module  # noqa: E402
from backend import config  # noqa: E402
from backend.pipeline import stages  # noqa: E402


class FakeWhisper:
    device = "cpu"

    def __init__(self):
        self.initial_prompt = None

    def transcribe(self, path, language=None, progress_callback=None, initial_prompt=None, beam_size=1):
        self.initial_prompt = initial_prompt
        return {
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "What gave you the confidence today?"},
                {"start": 2.0, "end": 4.0, "text": "Monesy played really well."},
            ],
            "language": "en",
            "language_probability": 0.99,
            "duration": 4.0,
        }


def fake_complete(provider, system, user, **kwargs):
    if "Raw transcript" in user:
        return "Q: What gave you the confidence today?\n\nA: Monesy played really well."
    return "Q：今天是什么给了你们信心？\n\nA：monesy 今天打得非常好。"


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class PipelineTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def _wait(self, client, task_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = client.get(f"/api/tasks/{task_id}").json()
            if task["state"] not in ("queued", "processing"):
                return task
            time.sleep(0.2)
        self.fail("流水线超时")

    def test_uploaded_audio_produces_normalized_docx(self):
        audio = Path(_TMP) / "sample.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio)],
            check=True,
        )
        whisper = FakeWhisper()
        with patch.object(stages, "_get_whisper", return_value=whisper), \
                patch.object(stages.llm, "complete", side_effect=fake_complete), \
                TestClient(app_module.app) as client:
            settings = client.get("/api/settings").json()
            settings["llm"]["providers"] = [{"id": "p1", "name": "fake", "protocol": "openai", "model": "m"}]
            settings["llm"]["clean"]["provider_id"] = "p1"
            settings["llm"]["write"]["provider_id"] = "p1"
            client.put("/api/settings", json=settings)

            task = client.post("/api/tasks", json={"title": "Falcons vs Vitality", "source_type": "file"}).json()
            self.assertEqual(task["state"], "draft")
            client.patch(f"/api/tasks/{task['id']}", json={
                "context": {"teams": [{"name": "Falcons", "players": ["m0NESY", "NiKo"]}]},
            })
            with open(audio, "rb") as fh:
                client.post(f"/api/tasks/{task['id']}/upload", files={"file": ("sample.wav", fh, "audio/wav")})

            done = self._wait(client, task["id"])
            self.assertEqual(done["state"], "review", done["error"])
            self.assertIn("m0NESY", whisper.initial_prompt)
            self.assertEqual(done["texts"]["draft_zh.txt"], "Q：今天是什么给了你们信心？\n\nA：m0NESY 今天打得非常好。")
            self.assertTrue(done["docx"].endswith("_Falcons_vs_Vitality.docx"))
            self.assertTrue((config.task_dir(task["id"]) / done["docx"]).exists())

            # 人工修改中文稿后，从「生成 Word」重跑
            client.put(f"/api/tasks/{task['id']}/texts/draft_zh.txt", json={"text": "Q：改过的问题\n\nA：改过的回答"})
            client.post(f"/api/tasks/{task['id']}/run", json={"from_stage": "export"})
            rerun = self._wait(client, task["id"])
            self.assertEqual(rerun["state"], "review")


if __name__ == "__main__":
    unittest.main()
