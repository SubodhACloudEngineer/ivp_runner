"""Surgical .xlsx writer: cell text and anchored pictures.

Writes plain text into existing rows of one worksheet, optionally anchors PNG
pictures to cells of that worksheet, and copies every other part of the
package across unchanged. It exists because openpyxl's load/save
round-trip destroys content in the customer MOP workbook (embedded Visio
objects, cached formula values, x14 validations, SharePoint metadata).

The edit is done on the sheet XML text, not by re-serialising a parsed tree:
ElementTree would rename namespace prefixes and break ``mc:Ignorable``, which
makes Excel "repair" the file.

Text is written as an inline string (``t="inlineStr"``), so sharedStrings.xml
is never touched. Excel converts it to a shared string on its next save. Each
cell keeps its existing style index.

Pictures are appended to the sheet's existing drawing part as
``<xdr:oneCellAnchor>`` elements. Each one declares its own namespaces, so the
drawing's root element is left as it was. Existing pictures, their IDs and
their relationships are never renumbered. Pictures this tool added are named
``ivp-runner:...`` and are replaced on a re-run rather than duplicated.
"""

from __future__ import annotations

import posixpath
import re
import struct
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_DRAWING = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
CT_DRAWING = "application/vnd.openxmlformats-officedocument.drawing+xml"
OUR_PREFIX = "ivp-runner:"
EMU_PER_PX = 9525
MARGIN_PX = 4

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


@dataclass(frozen=True)
class Picture:
    """A PNG to anchor at the top-left of ``cell``, scaled down to fit inside it."""

    cell: str
    png: bytes
    name: str  # stored as "ivp-runner:<name>"; identifies our pictures on re-runs
    description: str = ""


def patch_cells(
    src: str | Path, dst: str | Path, sheet_name: str, cells: Mapping[str, str]
) -> None:
    """Copy ``src`` to ``dst`` with ``cells`` (ref -> text) written into ``sheet_name``."""
    patch_workbook(src, dst, sheet_name, cells, ())


def patch_workbook(
    src: str | Path,
    dst: str | Path,
    sheet_name: str,
    cells: Mapping[str, str],
    pictures: Sequence[Picture] = (),
) -> None:
    """Copy ``src`` to ``dst`` with ``cells`` written and ``pictures`` anchored in ``sheet_name``.

    Refuses to write if ``dst`` is ``src``, or if a target cell holds a formula,
    sits inside a merged range other than at its top-left, carries rich-value
    metadata, or is on a row that doesn't exist. It also refuses if a picture
    target cell already holds a picture this tool did not put there. Every
    other zip entry is copied with identical content, in the original order.
    """
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise PatchError("dst must differ from src: the source workbook is never modified")
    if not cells and not pictures:
        raise PatchError("nothing to write")

    with zipfile.ZipFile(src) as zin:
        names = set(zin.namelist())
        part = sheet_part(zin, sheet_name)
        sheet_xml = zin.read(part).decode("utf-8")
        if cells:
            sheet_xml = _patch_sheet_xml(sheet_xml, cells)
        changed: dict[str, bytes] = {}
        removed: set[str] = set()
        added: dict[str, bytes] = {}
        if pictures:
            sheet_xml = _embed_pictures(
                zin, names, part, sheet_xml, pictures, changed, removed, added
            )
        ET.fromstring(sheet_xml)  # must still be well-formed
        changed[part] = sheet_xml.encode("utf-8")
        for name, data in {**changed, **added}.items():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(data)

        tmp = dst.with_name(dst.name + ".partial")
        with zipfile.ZipFile(tmp, "w") as zout:
            zout.comment = zin.comment
            for info in zin.infolist():
                if info.filename in removed:
                    continue
                data = changed.get(info.filename)
                if data is None:
                    data = zin.read(info)
                zout.writestr(info, data, compress_type=info.compress_type)
            for name, data in added.items():
                zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                compress = zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
                zout.writestr(zi, data, compress_type=compress)
    tmp.replace(dst)


# ---------------------------------------------------------------- pictures


def _rels_path(part: str) -> str:
    d, f = posixpath.split(part)
    return posixpath.join(d, "_rels", f + ".rels")


def _rels(zin: zipfile.ZipFile, names: set[str], rels_part: str) -> list[dict[str, str]]:
    if rels_part not in names:
        return []
    root = ET.fromstring(zin.read(rels_part))
    return [dict(r.attrib) for r in root.iter(f"{{{NS_REL}}}Relationship")]


def _resolve(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        raise PatchError("picture is not a PNG")
    return struct.unpack(">II", png[16:24])


def _col_width_px(sheet_xml: str, col: int) -> int:
    """Column width in pixels (Excel's formula for the default 7 px Calibri digit)."""
    width = None
    cols = re.search(r"<cols>(.*?)</cols>", sheet_xml, re.S)
    if cols:
        for m in re.finditer(r"<col\b([^>]*)/?>", cols.group(1)):
            a = dict(_ATTR_RE.findall(m.group(1)))
            if int(a.get("min", 0)) <= col <= int(a.get("max", 0)) and "width" in a:
                width = float(a["width"])
                break
    if width is None:
        fmt = re.search(r"<sheetFormatPr\b([^>]*)/?>", sheet_xml)
        a = dict(_ATTR_RE.findall(fmt.group(1))) if fmt else {}
        if "defaultColWidth" in a:
            width = float(a["defaultColWidth"])
        else:
            return int(float(a.get("baseColWidth", 8)) * 7 + 5)
    return int((256 * width + int(128 / 7)) / 256 * 7)


def _row_height_px(sheet_xml: str, row: int) -> int:
    m = re.search(rf'<row r="{row}"(\s[^>]*?)?/?>', sheet_xml)
    a = dict(_ATTR_RE.findall(m.group(1) or "")) if m else {}
    if "ht" in a:
        pt = float(a["ht"])
    else:
        fmt = re.search(r"<sheetFormatPr\b([^>]*)/?>", sheet_xml)
        pt = (
            float(dict(_ATTR_RE.findall(fmt.group(1))).get("defaultRowHeight", 15)) if fmt else 15.0
        )
    return int(pt * 96 / 72)


_ANCHOR_RE = re.compile(
    r"<(?P<p>\w+:)?(?P<k>oneCellAnchor|twoCellAnchor|absoluteAnchor)\b.*?</(?P=p)?(?P=k)>", re.S
)


def _anchor_info(anchor_xml: str) -> tuple[str, int | None, int | None, list[str]]:
    """(picture name, from-col 0-based, from-row 0-based, embedded rel ids) of one anchor."""
    name = re.search(r'<(?:\w+:)?cNvPr\b[^>]*\bname="([^"]*)"', anchor_xml)
    col = re.search(r"<(?:\w+:)?from>\s*<(?:\w+:)?col>(\d+)<", anchor_xml)
    row = re.search(r"<(?:\w+:)?row>(\d+)</(?:\w+:)?row>", anchor_xml)
    embeds = re.findall(r'\br:(?:embed|link)="([^"]+)"', anchor_xml)
    return (
        name.group(1) if name else "",
        int(col.group(1)) if col else None,
        int(row.group(1)) if row else None,
        embeds,
    )


def _embed_pictures(
    zin, names, sheet_part_name, sheet_xml, pictures, changed, removed, added
) -> str:
    ct = zin.read("[Content_Types].xml").decode("utf-8")
    sheet_rels_part = _rels_path(sheet_part_name)
    sheet_rels = _rels(zin, names, sheet_rels_part)

    # Find (or create) the sheet's drawing part.
    drawing_part = None
    m = re.search(r'<drawing\b[^>]*\br:id="([^"]+)"', sheet_xml)
    if m:
        rel = next((r for r in sheet_rels if r.get("Id") == m.group(1)), None)
        if rel is None:
            raise PatchError(f"{sheet_part_name}: drawing relationship {m.group(1)} missing")
        drawing_part = _resolve(sheet_part_name, rel["Target"])
        drawing_xml = zin.read(drawing_part).decode("utf-8")
    else:
        n = 1
        while f"xl/drawings/drawing{n}.xml" in names:
            n += 1
        drawing_part = f"xl/drawings/drawing{n}.xml"
        drawing_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<xdr:wsDr xmlns:xdr="{NS_XDR}" xmlns:a="{NS_A}"></xdr:wsDr>'
        )
        rid = _new_id({r.get("Id", "") for r in sheet_rels}, "rIdIvpDrawing")
        rels_xml = _rels_xml_add(
            zin.read(sheet_rels_part).decode("utf-8") if sheet_rels_part in names else None,
            rid,
            REL_DRAWING,
            posixpath.relpath(drawing_part, posixpath.dirname(sheet_part_name)),
        )
        (changed if sheet_rels_part in names else added)[sheet_rels_part] = rels_xml.encode("utf-8")
        sheet_xml = _insert_drawing_ref(sheet_xml, rid)
        ct = _ct_add_override(ct, "/" + drawing_part, CT_DRAWING)
        added[drawing_part] = b""  # placeholder; filled below

    drawing_rels_part = _rels_path(drawing_part)
    drawing_rels = _rels(zin, names, drawing_rels_part)
    rels_xml = zin.read(drawing_rels_part).decode("utf-8") if drawing_rels_part in names else None

    # Drop pictures from a previous ivp-runner run; refuse if someone else's sit in our cells.
    targets = {(col_index(split_ref(p.cell)[0]) - 1, split_ref(p.cell)[1] - 1): p for p in pictures}
    for am in list(_ANCHOR_RE.finditer(drawing_xml)):
        name, col, row, embeds = _anchor_info(am.group(0))
        if name.startswith(OUR_PREFIX):
            drawing_xml = drawing_xml.replace(am.group(0), "", 1)
            for rid in embeds:
                rel = next((r for r in drawing_rels if r.get("Id") == rid), None)
                if rel is not None:
                    removed.add(_resolve(drawing_part, rel["Target"]))
                    rels_xml = re.sub(
                        rf'<Relationship\b[^>]*\bId="{re.escape(rid)}"[^>]*/>', "", rels_xml
                    )
        elif (col, row) in targets:
            cell = targets[(col, row)].cell
            raise PatchError(
                f"{cell} already holds a picture ({name or 'unnamed'}) placed by someone "
                "else; not overwriting"
            )

    existing_ids = [int(i) for i in re.findall(r'<(?:\w+:)?cNvPr\b[^>]*\bid="(\d+)"', drawing_xml)]
    next_id = max(existing_ids, default=0) + 1
    rel_ids = {r.get("Id", "") for r in drawing_rels} | set(
        re.findall(r'Id="([^"]+)"', rels_xml or "")
    )
    media_names = names - removed

    anchors = []
    for i, pic in enumerate(pictures, 1):
        col_letters, row = split_ref(pic.cell)
        col = col_index(col_letters)
        w, h = _png_size(pic.png)
        box_w = _col_width_px(sheet_xml, col) - 2 * MARGIN_PX
        box_h = _row_height_px(sheet_xml, row) - 2 * MARGIN_PX
        scale = min(1.0, box_w / w, box_h / h)
        cx, cy = round(w * scale) * EMU_PER_PX, round(h * scale) * EMU_PER_PX

        safe = re.sub(r"[^A-Za-z0-9]+", "_", pic.name).strip("_") or f"img{i}"
        media = f"xl/media/ivp_runner_{safe}.png"
        k = 2
        while media in media_names or media in added:
            media = f"xl/media/ivp_runner_{safe}_{k}.png"
            k += 1
        added[media] = pic.png
        rid = _new_id(rel_ids, f"rIdIvp{i}")
        rel_ids.add(rid)
        rels_xml = _rels_xml_add(
            rels_xml, rid, REL_IMAGE, posixpath.relpath(media, posixpath.dirname(drawing_part))
        )

        off = MARGIN_PX * EMU_PER_PX
        anchors.append(
            f'<xdr:oneCellAnchor xmlns:xdr="{NS_XDR}" xmlns:a="{NS_A}" xmlns:r="{NS_R}">'
            f"<xdr:from><xdr:col>{col - 1}</xdr:col><xdr:colOff>{off}</xdr:colOff>"
            f"<xdr:row>{row - 1}</xdr:row><xdr:rowOff>{off}</xdr:rowOff></xdr:from>"
            f'<xdr:ext cx="{cx}" cy="{cy}"/>'
            "<xdr:pic><xdr:nvPicPr>"
            f'<xdr:cNvPr id="{next_id}" name="{escape(OUR_PREFIX + pic.name)}" '
            f'descr="{escape(pic.description, {chr(34): "&quot;"})}"/>'
            '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr></xdr:nvPicPr>'
            f'<xdr:blipFill><a:blip r:embed="{rid}"/>'
            "<a:stretch><a:fillRect/></a:stretch></xdr:blipFill>"
            f'<xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic>'
            "<xdr:clientData/></xdr:oneCellAnchor>"
        )
        next_id += 1

    close = re.search(r"</(?:\w+:)?wsDr>\s*$", drawing_xml)
    if close is None:
        raise PatchError(f"{drawing_part}: unexpected drawing XML (no closing wsDr)")
    drawing_xml = drawing_xml[: close.start()] + "".join(anchors) + drawing_xml[close.start() :]
    (added if drawing_part in added else changed)[drawing_part] = drawing_xml.encode("utf-8")
    (changed if drawing_rels_part in names else added)[drawing_rels_part] = rels_xml.encode("utf-8")

    if not re.search(r'<Default\b[^>]*Extension="png"', ct, re.I):
        ct = ct.replace(
            "<Default ", '<Default Extension="png" ContentType="image/png"/><Default ', 1
        )
    if ct != zin.read("[Content_Types].xml").decode("utf-8"):
        changed["[Content_Types].xml"] = ct.encode("utf-8")
    return sheet_xml


def _new_id(existing: set[str], base: str) -> str:
    rid, n = base, 2
    while rid in existing:
        rid, n = f"{base}_{n}", n + 1
    return rid


def _rels_xml_add(rels_xml: str | None, rid: str, rel_type: str, target: str) -> str:
    if rels_xml is None:
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Relationships xmlns="{NS_REL}"></Relationships>'
        )
    rel = f'<Relationship Id="{rid}" Type="{rel_type}" Target="{escape(target)}"/>'
    i = rels_xml.rindex("</Relationships>")
    return rels_xml[:i] + rel + rels_xml[i:]


def _ct_add_override(ct: str, part_name: str, content_type: str) -> str:
    if f'PartName="{part_name}"' in ct:
        return ct
    i = ct.rindex("</Types>")
    return ct[:i] + f'<Override PartName="{part_name}" ContentType="{content_type}"/>' + ct[i:]


# Elements that must follow <drawing> in CT_Worksheet.
_AFTER_DRAWING = (
    "legacyDrawing", "legacyDrawingHF", "drawingHF", "picture", "oleObjects",
    "controls", "webPublishItems", "tableParts", "extLst",
)  # fmt: skip


def _insert_drawing_ref(sheet_xml: str, rid: str) -> str:
    el = f'<drawing xmlns:r="{NS_R}" r:id="{rid}"/>'
    positions = [m.start() for t in _AFTER_DRAWING for m in [re.search(rf"<{t}\b", sheet_xml)] if m]
    i = min(positions) if positions else sheet_xml.rindex("</worksheet>")
    return sheet_xml[:i] + el + sheet_xml[i:]


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
