"""Inbox builders shared by the files connector tests (T01-09)."""

from __future__ import annotations

import datetime
import os
import zipfile
from pathlib import Path
from typing import Any

import duckdb

from herness.connectors.files import FilesConnector
from herness.connectors.settings import FilesSettings

NOW = datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.UTC)
EXCEL_SKIP = "DuckDB excel extension not installed (spec O-3; installed at deploy by T10-26)"


def settings(entities: dict[str, Any] | None = None, **extra: Any) -> FilesSettings:
    entities = entities or {"teams": {"pattern": "*.csv", "key_field": ["id"]}}
    data: dict[str, Any] = {"enabled": True, "entities": entities}
    return FilesSettings.model_validate(data | extra)


def connector(
    root: Path,
    entities: dict[str, Any] | None = None,
    *,
    now: datetime.datetime = NOW,
    **extra: Any,
) -> FilesConnector:
    return FilesConnector(settings(entities, **extra), inbox_root=root, clock=lambda: now)


def mtime_ns(age_s: float) -> int:
    return int((NOW - datetime.timedelta(seconds=age_s)).timestamp()) * 10**9


def drop(root: Path, rel: str, data: bytes | str, *, age_s: float = 3600) -> Path:
    """Write ``root/rel`` and set its mtime ``age_s`` seconds before ``NOW``."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8", newline="")
    else:
        path.write_bytes(data)
    ns = mtime_ns(age_s)
    os.utime(path, ns=(ns, ns))
    return path


def excel_available() -> bool:
    con = duckdb.connect(":memory:", config={"autoinstall_known_extensions": False})
    try:
        con.execute("LOAD excel")
    except duckdb.Error:
        return False
    finally:
        con.close()
    return True


_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.sheet.main+xml"/>{overrides}</Types>'
)
_SHEET_CT = (
    '<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
)
_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_RNS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_WS_REL = (
    '<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/worksheet" Target="worksheets/sheet{n}.xml"/>'
)


def _row(r: int, cells: list[str]) -> str:
    out = "".join(
        f'<c r="{chr(65 + i)}{r}" t="inlineStr"><is><t>{v}</t></is></c>'
        for i, v in enumerate(cells)
    )
    return f'<row r="{r}">{out}</row>'


def _package(zf: zipfile.ZipFile, names: list[str]) -> None:
    n = range(1, len(names) + 1)
    zf.writestr(
        "[Content_Types].xml", _CT.format(overrides="".join(_SHEET_CT.format(n=i) for i in n))
    )
    zf.writestr("_rels/.rels", _RELS)
    sheets = "".join(
        f'<sheet name="{s}" sheetId="{i}" r:id="rId{i}"/>' for i, s in zip(n, names, strict=True)
    )
    zf.writestr("xl/workbook.xml", f"<workbook {_NS} {_RNS}><sheets>{sheets}</sheets></workbook>")
    rels = "".join(_WS_REL.format(n=i) for i in n)
    zf.writestr(
        "xl/_rels/workbook.xml.rels",
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>",
    )


def write_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> Path:
    """Write a minimal inline-string workbook (no openpyxl in the lock)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _package(zf, list(sheets))
        for i, rows in enumerate(sheets.values(), start=1):
            body = "".join(_row(r, cells) for r, cells in enumerate(rows, start=1))
            zf.writestr(
                f"xl/worksheets/sheet{i}.xml",
                f"<worksheet {_NS}><sheetData>{body}</sheetData></worksheet>",
            )
    return path


def write_xlsx_bomb(path: Path, inflated_bytes: int) -> Path:
    """Small workbook whose one cell inflates to ``inflated_bytes`` of text."""
    chunk = b"A" * 1_048_576
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        _package(zf, ["Teams"])
        with zf.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as out:
            out.write(
                f'<worksheet {_NS}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>'.encode()
            )
            for _ in range(inflated_bytes // len(chunk)):
                out.write(chunk)
            out.write(b"</t></is></c></row></sheetData></worksheet>")
    return path


def sparse(path: Path, size: int) -> Path:
    """Create a ``size``-byte file without writing its bytes (seek past the end)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.seek(size - 1)
        fh.write(b"\0")
    ns = mtime_ns(3600)
    os.utime(path, ns=(ns, ns))
    return path
