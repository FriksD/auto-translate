import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.pipeline.docx_export import export_docx, parse_paragraphs


class DocxExportTest(unittest.TestCase):
    def test_parse_normalizes_prefixes(self):
        self.assertEqual(
            parse_paragraphs("q: 问题\n\n A:回答\n补充段落"),
            [("q", "Q：问题"), ("a", "A：回答"), ("p", "补充段落")],
        )

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


if __name__ == "__main__":
    unittest.main()
