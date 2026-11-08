import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.pipeline import prompts
from backend.pipeline.docx_export import export_docx, parse_paragraphs


class DocxExportTest(unittest.TestCase):
    def test_parse_normalizes_prefixes(self):
        self.assertEqual(
            parse_paragraphs("q: 问题\n\n A:回答\n补充段落"),
            [("q", "Q：问题"), ("a", "A：回答"), ("p", "补充段落")],
        )

    def test_named_answers_for_several_interviewees(self):
        self.assertEqual(
            parse_paragraphs("Q：问 Spinx\nSpinx: 回答\nxertioN：另一个回答\n我想说：不是名字"),
            [("q", "Q：问 Spinx"), ("a", "Spinx：回答"), ("a", "xertioN：另一个回答"), ("p", "我想说：不是名字")],
        )

    def test_group_interview_prompts_name_the_answers(self):
        group = {"identified": {"interviewees": ["Spinx", "xertioN"]}}
        self.assertIn('Never write "A:"', prompts.clean_format(group))
        self.assertIn("A: <answer", prompts.clean_format({"identified": {"interviewees": ["Spinx"]}}))
        english = "Q: Spinx?\n\nSpinx: Yes.\n\nQ: And you?\n\nxertioN: Sure."
        self.assertEqual(prompts.answer_speakers(english), ["Spinx", "xertioN"])
        self.assertIn("不要写成「A：」", prompts.write_format(english))
        self.assertIn("A：<选手回答", prompts.write_format("Q: hi\n\nA: yes"))

    def test_question_bold_answer_plain_12pt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_docx(
                "Q：是什么给了你们赢下这么强队伍的信心？\n\nA：我们准备得很充分。\n第二段回答。",
                Path(tmp) / "out.docx",
            )
            document = Document(str(path))
            paragraphs = document.paragraphs
            section = document.sections[0]

        self.assertEqual([p.text for p in paragraphs], [
            "Q：是什么给了你们赢下这么强队伍的信心？", "A：我们准备得很充分。", "第二段回答。",
        ])
        self.assertEqual([[r.bold for r in p.runs] for p in paragraphs], [[True], [False], [False]])
        for paragraph in paragraphs:
            for run in paragraph.runs:
                self.assertEqual(run.font.size.pt, 12)
        self.assertEqual((round(section.page_width.cm, 1), round(section.page_height.cm, 1)), (21.0, 29.7))

    def test_lead_is_first_plain_paragraph(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_docx("Q：问\n\nA：答", Path(tmp) / "out.docx", lead="IEM北京站今日继续，TYLOO以2-0击败9z。")
            paragraphs = Document(str(path)).paragraphs
        self.assertEqual([p.text for p in paragraphs], ["IEM北京站今日继续，TYLOO以2-0击败9z。", "Q：问", "A：答"])
        self.assertEqual([[r.bold for r in p.runs] for p in paragraphs], [[False], [True], [False]])
        self.assertEqual(paragraphs[0].runs[0].font.size.pt, 12)


if __name__ == "__main__":
    unittest.main()
