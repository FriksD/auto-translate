import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.pipeline.names import NameNormalizer, collect_canonical_names, name_key


class NameKeyTest(unittest.TestCase):
    def test_leet_and_case_variants_share_key(self):
        self.assertEqual(name_key("m0NESY"), name_key("monesy"))
        self.assertEqual(name_key("Mo Nesy"), "monesy")
        self.assertEqual(name_key("s1mple"), "simple")


class NameNormalizerTest(unittest.TestCase):
    def setUp(self):
        self.normalizer = NameNormalizer(
            ["m0NESY", "ZywOo", "Natus Vincere", "Virtus.pro", "The MongolZ", "apEX"],
            aliases={"NAVI": "Natus Vincere", "小蜜蜂": "Vitality"},
        )

    def test_replaces_misspelled_names_with_hltv_spelling(self):
        text, report = self.normalizer.normalize("A：monesy 和 zywoo 都打得很好，Mo Nesy 说 apex 很强。")
        self.assertEqual(text, "A：m0NESY 和 ZywOo 都打得很好，m0NESY 说 apEX 很强。")
        self.assertEqual(sum(r["count"] for r in report.to_dict()["replaced"]), 4)

    def test_multi_word_and_punctuated_team_names(self):
        text, _ = self.normalizer.normalize("对阵 natus vincere、Virtus pro 和 the mongolz")
        self.assertEqual(text, "对阵 Natus Vincere、Virtus.pro 和 The MongolZ")

    def test_aliases(self):
        text, report = self.normalizer.normalize("Navi 和小蜜蜂")
        self.assertEqual(text, "Natus Vincere 和Vitality")
        self.assertEqual(report.to_dict()["unknown"], [])

    def test_correct_names_are_untouched(self):
        source = "Q：m0NESY 和 ZywOo"
        text, report = self.normalizer.normalize(source)
        self.assertEqual(text, source)
        self.assertEqual(report.to_dict()["replaced"], [])

    def test_near_misses_are_reported_as_suspects(self):
        _, report = self.normalizer.normalize("Monessy 打得很好")
        self.assertEqual(report.to_dict()["suspects"], [{"token": "Monessy", "candidates": ["m0NESY"]}])

    def test_ambiguous_keys_are_not_replaced(self):
        normalizer = NameNormalizer(["s1mple", "Simple"])
        text, report = normalizer.normalize("simple")
        self.assertEqual(text, "simple")
        self.assertEqual(report.to_dict()["ambiguous"][0]["token"], "simple")


class CollectCanonicalNamesTest(unittest.TestCase):
    def test_collects_teams_players_coaches_and_extra_names(self):
        context = {
            "teams": [
                {"name": "Falcons", "players": ["m0NESY", "NiKo"], "stats": {"coach": "zonic"}},
                {"name": "Vitality", "players": ["ZywOo", "NiKo"]},
            ],
            "extra_names": ["Hugo"],
        }
        self.assertEqual(
            collect_canonical_names(context),
            ["Falcons", "m0NESY", "NiKo", "zonic", "Vitality", "ZywOo", "Hugo"],
        )


if __name__ == "__main__":
    unittest.main()
