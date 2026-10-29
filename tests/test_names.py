import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.pipeline.names import NameNormalizer, collect_canonical_names, name_key, replace_tokens


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

    def test_pronunciation_variants_are_inferred(self):
        normalizer = NameNormalizer(["m0NESY", "TeSeS", "kyousuke", "karrigan", "magixx", "ropz"])
        text, report = normalizer.normalize("Monessy、Manasi、Tesis、Kyosuke、Karigan、Magix、Ropes")
        self.assertEqual(text, "m0NESY、m0NESY、TeSeS、kyousuke、karrigan、magixx、ropz")
        methods = {r["from"]: r["method"] for r in report.to_dict()["replaced"]}
        self.assertEqual(methods["Monessy"], "phonetic")
        self.assertEqual(methods["Manasi"], "skeleton")

    def test_known_words_are_not_inferred(self):
        normalizer = NameNormalizer(["m0NESY"], known_words=["mines"])
        text, _ = normalizer.normalize("mines")
        self.assertEqual(text, "mines")

    def test_unresolvable_words_are_reported(self):
        normalizer = NameNormalizer(["sh1ro", "donk"])
        _, report = normalizer.normalize("Shearer 和 Donkin")
        report = report.to_dict()
        self.assertEqual(report["unknown"], ["Shearer"])
        self.assertEqual(report["suspects"], [{"token": "Donkin", "candidates": ["donk"]}])

    def test_ambiguous_keys_are_not_replaced(self):
        normalizer = NameNormalizer(["s1mple", "Simple"])
        text, report = normalizer.normalize("simple")
        self.assertEqual(text, "simple")
        self.assertEqual(report.to_dict()["ambiguous"][0]["token"], "simple")


class ReplaceTokensTest(unittest.TestCase):
    def test_replaces_whole_words_only(self):
        text, counts = replace_tokens("Shearer 说 Shearers 不对，Shearer。", {"Shearer": "sh1ro"})
        self.assertEqual(text, "sh1ro 说 Shearers 不对，sh1ro。")
        self.assertEqual(counts, {"Shearer": 2})


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

    def test_includes_recent_opponents(self):
        context = {"teams": [{"name": "Falcons", "players": [], "past_matches": [{"opponent": "Natus Vincere"}]}]}
        self.assertEqual(collect_canonical_names(context), ["Falcons", "Natus Vincere"])
        normalizer = NameNormalizer(collect_canonical_names(context), aliases={"NAVI": "Natus Vincere"})
        self.assertEqual(normalizer.normalize("击败 NAVI 之后")[0], "击败 Natus Vincere 之后")
        self.assertEqual(normalizer.normalize("击败 Na'Vi 之后")[0], "击败 Natus Vincere 之后")


if __name__ == "__main__":
    unittest.main()
