"""
Export saved records as CSV, TXT or XLSX.

XLSX is written with the standard library only (an .xlsx file is a zip of
XML files), so the project has no third-party dependencies.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from xml.sax.saxutils import escape

# Columns in exported files, with their header labels.
EXPORT_COLUMNS = [
    ("name", "Name"),
    ("login", "GitHub username"),
    ("location", "Area"),
    ("email", "Gmail"),
    ("profile_url", "Profile URL"),
    ("avatar_url", "Avatar URL"),
]


def _csv_safe(value) -> str:
    """Stop spreadsheet apps from running text like '=HYPERLINK(...)' as a formula."""
    text = str(value or "")
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in EXPORT_COLUMNS])
    for r in rows:
        w.writerow([_csv_safe(r.get(key)) for key, _ in EXPORT_COLUMNS])
    # UTF-8 BOM so Excel shows non-ASCII names (e.g. Japanese) correctly.
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_txt(rows: list[dict]) -> bytes:
    """One Gmail address per line."""
    return "".join(f"{r['email']}\n" for r in rows).encode("utf-8")


# ---------------------------------------------------------------- XLSX ----

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Gmail addresses" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# Style 0 = normal, style 1 = bold (header row).
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell(ref: str, value, style: int = 0) -> str:
    text = escape(_ILLEGAL_XML.sub("", str(value or "")))
    s = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{s}><is><t xml:space="preserve">{text}</t></is></c>'


def to_xlsx(rows: list[dict]) -> bytes:
    table = [[label for _, label in EXPORT_COLUMNS]]
    table += [[r.get(key) or "" for key, _ in EXPORT_COLUMNS] for r in rows]

    sheet_rows = []
    for i, values in enumerate(table, 1):
        style = 1 if i == 1 else 0
        cells = "".join(_cell(f"{_col_letter(j)}{i}", v, style) for j, v in enumerate(values))
        sheet_rows.append(f'<row r="{i}">{cells}</row>')

    widths = [min(60, max(len(str(row[j])) for row in table) + 2) for j in range(len(EXPORT_COLUMNS))]
    cols = "".join(f'<col min="{j + 1}" max="{j + 1}" width="{w}" customWidth="1"/>'
                   for j, w in enumerate(widths))

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        f'<cols>{cols}</cols>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        '</worksheet>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


FORMATS = {
    "csv": (to_csv, "text/csv; charset=utf-8"),
    "txt": (to_txt, "text/plain; charset=utf-8"),
    "xlsx": (to_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}
