import unittest
import sys
from pathlib import Path
from unittest.mock import patch

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.pipeline.diarization import DiarizationService


class CapturingDiarizationService(DiarizationService):
    def __init__(self):
        super().__init__()
        self.captured_sample_rate = None
        self.captured_num_speakers = None

    def _load_pipeline(self):
        self.pipeline = object()

    def _diarize_direct(
        self,
        waveform,
        sample_rate,
        total_duration,
        num_speakers,
        min_speakers,
        max_speakers,
        progress_callback=None,
    ):
        self.captured_sample_rate = sample_rate
        self.captured_num_speakers = num_speakers
        return {"segments": [], "num_speakers": 0, "speakers": []}


class FakeResample:
    def __init__(self, orig_freq, new_freq):
        self.orig_freq = orig_freq
        self.new_freq = new_freq

    def __call__(self, waveform):
        return waveform


class DiarizationServiceTest(unittest.TestCase):
    def test_diarize_resamples_to_16khz_and_preserves_speaker_count(self):
        service = CapturingDiarizationService()
        waveform = torch.zeros(1, 32000)

        with patch("backend.pipeline.diarization.torchaudio.load", return_value=(waveform, 32000)):
            with patch("backend.pipeline.diarization.torchaudio.transforms.Resample", FakeResample):
                service.diarize("C:/audio/interview.wav", num_speakers=2)

        self.assertEqual(service.captured_sample_rate, 16000)
        self.assertEqual(service.captured_num_speakers, 2)


if __name__ == "__main__":
    unittest.main()
