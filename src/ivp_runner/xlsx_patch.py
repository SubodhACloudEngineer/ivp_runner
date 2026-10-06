"""Surgical .xlsx cell writer.

Writes plain text into existing rows of one worksheet and copies every other
part of the package across unchanged. It exists because openpyxl's load/save
round-trip destroys content in the customer MOP workbook (embedded Visio
objects, cached formula values, x14 validations, SharePoint metadata).

The edit is done on the sheet XML text, not by re-serialising a parsed tree:
ElementTree would rename namespace prefixes and break ``mc:Ignorable``, which
makes Excel "repair" the file.

Text is written as an inline string (``t="inlineStr"``), so sharedStrings.xml
is never touched. Excel converts it to a shared string on its next save. Each
cell keeps its existing style index.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

CELL_REF_RE = re.compile(r"^([A-Z]{1,3})([1-9]\d*)$")
_ATTR_RE = re.compile(r'([\w:]+)="([^"]*)"')
# XML 1.0 forbids most control characters, even escaped.
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class PatchError(ValueError):
    """The requested edit is unsafe or impossible. Nothing has been written."""


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def split_ref(ref: str) -> tuple[str, int]:
    m = CELL_REF_RE.match(ref)
    if not m:
        raise PatchError(f"invalid cell reference {ref!r}")
    return m.group(1), int(m.group(2))


def sheet_part(zf: zipfile.ZipFile, sheet_name: str) -> str:
    """Zip path of the worksheet named ``sheet_name``."""
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rid = None
    for s in wb.iter(f"{{{NS_MAIN}}}sheet"):
        if s.get("name") == sheet_name:
            rid = s.get(f"{{{NS_R}}}id")
            break
    if rid is None:
        raise PatchError(f"sheet {sheet_name!r} not found")
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for r in rels.iter(f"{{{NS_REL}}}Relationship"):
        if r.get("Id") == rid:
            target = r.get("Target")
            if target.startswith("/"):
                return target.lstrip("/")
            return posixpath.normpath(posixpath.join("xl", target))
    raise PatchError(f"sheet {sheet_name!r}: relationship {rid} not found")


def read_cell_texts(path: str | Path, sheet_name: str, refs: list[str]) -> dict[str, str]:
    """Displayed text of each cell (shared, inline, or formula-cached string). Missing gives ""."""
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read(sheet_part(zf, sheet_name)))
        sst: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            sst = ["".join(t.text or "" for t in si.iter(f"{{{NS_MAIN}}}t")) for si in sroot]
    wanted = set(refs)
    out = dict.fromkeys(refs, "")
    for c in root.iter(f"{{{NS_MAIN}}}c"):
        ref = c.get("r")
        if ref not in wanted:
            continue
        t = c.get("t")
        if t == "inlineStr":
            out[ref] = "".join(x.text or "" for x in c.iter(f"{{{NS_MAIN}}}t"))
            continue
        v = c.find(f"{{{NS_MAIN}}}v")
        if v is not None and v.text is not None:
            out[ref] = sst[int(v.text)] if t == "s" else v.text
    return out


def patch_cells(
    src: str | Path, dst: str | Path, sheet_name: str, cells: Mapping[str, str]
) -> None:
    """Copy ``src`` to ``dst`` with ``cells`` (ref -> text) written into ``sheet_name``.

    Refuses to write if ``dst`` is ``src``, or if a target cell holds a formula,
    sits inside a merged range other than at its top-left, carries rich-value
    metadata, or is on a row that doesn't exist. Every other zip entry is
    copied with identical content, in the original order.
    """
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise PatchError("dst must differ from src: the source workbook is never modified")
    if not cells:
        raise PatchError("no cells to write")

    with zipfile.ZipFile(src) as zin:
        part = sheet_part(zin, sheet_name)
        xml = zin.read(part).decode("utf-8")
        patched = _patch_sheet_xml(xml, cells)
        ET.fromstring(patched)  # must still be well-formed

        tmp = dst.with_name(dst.name + ".partial")
        with zipfile.ZipFile(tmp, "w") as zout:
            zout.comment = zin.comment
            for info in zin.infolist():
                data = patched.encode("utf-8") if info.filename == part else zin.read(info)
                zout.writestr(info, data, compress_type=info.compress_type)
    tmp.replace(dst)


def _patch_sheet_xml(xml: str, cells: Mapping[str, str]) -> str:
    merges = [
        tuple(split_ref(p) for p in m.split(":"))
        for m in re.findall(r'<mergeCell ref="([A-Z]+\d+:[A-Z]+\d+)"', xml)
    ]
    for ref, text in sorted(cells.items(), key=lambda kv: split_ref(kv[0])[1]):
        col, row = split_ref(ref)
        _check_merge(ref, col, row, merges)
        xml = _write_cell(xml, ref, col, row, text)
    return xml


def _check_merge(ref: str, col: str, row: int, merges) -> None:
    ci = col_index(col)
    for (c1, r1), (c2, r2) in merges:
        if r1 <= row <= r2 and col_index(c1) <= ci <= col_index(c2) and (c1, r1) != (col, row):
            raise PatchError(f"{ref} is inside merged range {c1}{r1}:{c2}{r2} but not its top-left")


def _write_cell(xml: str, ref: str, col: str, row: int, text: str) -> str:
    row_m = re.search(rf'<row r="{row}"(?:\s[^>]*?)?(?:/>|>(.*?)</row>)', xml, re.S)
    if row_m is None:
        raise PatchError(f"{ref}: row {row} does not exist in the sheet")
    row_xml = row_m.group(0)

    cell_m = re.search(rf'<c r="{ref}"((?:\s+[\w:]+="[^"]*")*)\s*(?:/>|>(.*?)</c>)', row_xml, re.S)
    style = None
    if cell_m:
        attrs = dict(_ATTR_RE.findall(cell_m.group(1)))
        body = cell_m.group(2) or ""
        if "<f" in body:
            raise PatchError(f"{ref} holds a formula; refusing to overwrite it")
        if "vm" in attrs or "cm" in attrs:
            raise PatchError(f"{ref} carries cell/value metadata; refusing to overwrite it")
        style = attrs.get("s")

    new_cell = _inline_cell(ref, style, text)
    if cell_m:
        new_row = row_xml[: cell_m.start()] + new_cell + row_xml[cell_m.end() :]
    elif row_xml.endswith("/>"):
        new_row = row_xml[:-2] + ">" + new_cell + "</row>"
    else:
        # Insert in column order, before the first cell to the right of `col`.
        target = col_index(col)
        insert_at = len(row_xml) - len("</row>")
        for m in re.finditer(r'<c r="([A-Z]+)\d+"', row_xml):
            if col_index(m.group(1)) > target:
                insert_at = m.start()
                break
        new_row = row_xml[:insert_at] + new_cell + row_xml[insert_at:]
    return xml[: row_m.start()] + new_row + xml[row_m.end() :]


def _inline_cell(ref: str, style: str | None, text: str) -> str:
    text = _ILLEGAL_XML_RE.sub("", text)
    s = f' s="{style}"' if style is not None else ""
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
