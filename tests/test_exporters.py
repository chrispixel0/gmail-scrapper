import io
import unittest
import zipfile
from xml.etree import ElementTree as ET

from backend import exporters

ROWS = [
    {"login": "alice", "name": "山田 Alice", "location": "Tokyo", "email": "alice@gmail.com",
     "profile_url": "https://github.com/alice", "avatar_url": "https://avatars.githubusercontent.com/u/1"},
    {"login": "eve", "name": "=HYPERLINK(\"x\")", "location": None, "email": "eve@gmail.com",
     "profile_url": "https://github.com/eve", "avatar_url": None},
]
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class ExporterTests(unittest.TestCase):
    def test_csv(self):
        text = exporters.to_csv(ROWS).decode("utf-8-sig")
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "Name,GitHub username,Area,Gmail,Profile URL,Avatar URL")
        self.assertIn("山田 Alice,alice,Tokyo,alice@gmail.com", lines[1])
        self.assertTrue(lines[2].startswith("\"'=HYPERLINK"))   # formula neutralised

    def test_txt_is_one_email_per_line(self):
        self.assertEqual(exporters.to_txt(ROWS).decode(), "alice@gmail.com\neve@gmail.com\n")

    def test_xlsx_is_valid_workbook(self):
        data = exporters.to_xlsx(ROWS)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIsNone(z.testzip())
            for part in z.namelist():                    # every part must be well-formed XML
                ET.fromstring(z.read(part))
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows = sheet.findall(".//m:sheetData/m:row", NS)
        self.assertEqual(len(rows), 3)
        values = [[t.text or "" for t in r.findall(".//m:t", NS)] for r in rows]
        self.assertEqual(values[0][:4], ["Name", "GitHub username", "Area", "Gmail"])
        self.assertEqual(values[1][0], "山田 Alice")
        self.assertEqual(values[2][3], "eve@gmail.com")

    def test_col_letters(self):
        self.assertEqual([exporters._col_letter(i) for i in (0, 25, 26, 27)], ["A", "Z", "AA", "AB"])


if __name__ == "__main__":
    unittest.main()
