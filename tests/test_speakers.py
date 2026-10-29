import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.pipeline.stages import split_by_speaker


def word(text, start, end):
    return {"word": text, "start": start, "end": end}


class SplitBySpeakerTest(unittest.TestCase):
    def test_splits_segment_spanning_question_and_answer(self):
        segments = [{
            "start": 0, "end": 6, "text": "How are you? I am fine.",
            "words": [word(" How", 0, 0.5), word(" are", 0.5, 1), word(" you?", 1, 1.5),
                      word(" I", 3, 3.5), word(" am", 3.5, 4), word(" fine.", 4, 5)],
        }]
        turns = [{"start": 0, "end": 2, "speaker": "S0"}, {"start": 2.8, "end": 5.5, "speaker": "S1"}]
        result = split_by_speaker(segments, turns)
        self.assertEqual([(s["speaker"], s["text"]) for s in result], [("S0", "How are you?"), ("S1", "I am fine.")])

    def test_merges_consecutive_segments_of_same_speaker(self):
        segments = [
            {"start": 0, "end": 1, "text": "Hello", "words": [word(" Hello", 0, 1)]},
            {"start": 1, "end": 2, "text": "there", "words": [word(" there", 1, 2)]},
        ]
        result = split_by_speaker(segments, [{"start": 0, "end": 2, "speaker": "S0"}])
        self.assertEqual([(s["speaker"], s["text"]) for s in result], [("S0", "Hello there")])

    def test_words_outside_any_turn_use_nearest_speaker(self):
        segments = [{"start": 0, "end": 1, "text": "Hi", "words": [word(" Hi", 9, 9.5)]}]
        result = split_by_speaker(segments, [{"start": 0, "end": 2, "speaker": "S0"}, {"start": 10, "end": 12, "speaker": "S1"}])
        self.assertEqual(result[0]["speaker"], "S1")


if __name__ == "__main__":
    unittest.main()
