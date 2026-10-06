"""Throwaway risk spike: can openpyxl load and save the MOP workbook without damage?

    python scripts/roundtrip_check.py [reference/MOP_sample.xlsx]

Steps:
  1. Copy the reference workbook to out/<name>.original.xlsx. The reference
     file itself is never opened for writing; its SHA-256 is checked before
     and after the run.
  2. Load the copy with openpyxl and save it, unchanged, to
     out/<name>.roundtrip.xlsx.
  3. Compare original vs round-trip by reading the raw OOXML parts inside each
     .xlsx (a zip of XML files). This deliberately does NOT use openpyxl for
     the comparison: openpyxl only reports what it understands, so it would
     hide exactly the losses we are looking for.

Prints PASS/FAIL per property. Exits 1 if anything FAILs.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shutil
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
}
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
R_ID = f"{{{NS['r']}}}id"
R_EMBED = f"{{{NS['r']}}}embed"


# ---------------------------------------------------------------- OOXML reading


class Package:
    """Read-only view of an .xlsx as raw parts."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path)
        self.parts = {i.filename: i.file_size for i in self.zip.infolist()}

    def xml(self, part: str) -> ET.Element | None:
        if part not in self.parts:
            return None
        return ET.fromstring(self.zip.read(part))

    def rels(self, part: str) -> dict[str, str]:
        """Relationship id -> absolute target part, for `part`."""
        d, f = posixpath.split(part)
        root = self.xml(posixpath.join(d, "_rels", f + ".rels"))
        out = {}
        if root is None:
            return out
        for r in root.findall("rel:Relationship", NS):
            if r.get("TargetMode") == "External":
                continue
            target = r.get("Target")
            if target.startswith("/"):
                out[r.get("Id")] = target.lstrip("/")
            else:
                out[r.get("Id")] = posixpath.normpath(posixpath.join(d, target))
        return out

    def sheets(self) -> list[tuple[str, str]]:
        """(sheet name, sheet part) in workbook order."""
        wb = self.xml("xl/workbook.xml")
        rels = self.rels("xl/workbook.xml")
        return [
            (s.get("name"), rels.get(s.get(R_ID), "?"))
            for s in wb.find("m:sheets", NS).findall("m:sheet", NS)
        ]


def shared_strings(pkg: Package) -> list[str]:
    root = pkg.xml("xl/sharedStrings.xml")
    if root is None:
        return []
    return ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")) for si in root]


def cell_value(c: ET.Element, sst: list[str]) -> str | None:
    t = c.get("t")
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
    v = c.find("m:v", NS)
    if v is None or v.text is None:
        return None
    return sst[int(v.text)] if t == "s" else v.text


def sheet_facts(pkg: Package, name: str, part: str, sst: list[str]) -> dict:
    root = pkg.xml(part)
    facts: dict = {}
    if root is None:  # chartsheet or missing
        return {"missing_sheet_xml": 1}

    facts["merged_cells"] = len(root.findall(".//m:mergeCells/m:mergeCell", NS))
    facts["cond_format_rules"] = len(root.findall(".//m:conditionalFormatting/m:cfRule", NS))
    # Excel 2010+ conditional formats / validations live in <extLst> under x14.
    facts["cond_format_rules_x14"] = len(root.findall(f".//{{{X14}}}cfRule"))
    facts["data_validations"] = len(root.findall(".//m:dataValidations/m:dataValidation", NS))
    facts["data_validations_x14"] = len(root.findall(f".//{{{X14}}}dataValidation"))
    facts["hyperlinks"] = len(root.findall(".//m:hyperlinks/m:hyperlink", NS))

    formula_cells, cached, values = {}, 0, {}
    for c in root.iter(f"{{{NS['m']}}}c"):
        f = c.find("m:f", NS)
        if f is not None:
            formula_cells[c.get("r")] = f.text or ""
            cached += cell_value(c, sst) not in (None, "")
        else:
            val = cell_value(c, sst)
            if val not in (None, ""):
                values[c.get("r")] = val
    facts["formula_cells"] = formula_cells
    facts["formula_cached_values"] = cached
    facts["cell_values"] = values
    # Pictures in page headers/footers (e.g. a customer logo) live in a VML part.
    facts["header_footer_pictures"] = len(root.findall("m:legacyDrawingHF", NS))

    rels = pkg.rels(part)
    images = charts = shapes = cropped = bordered = 0
    anchors: list[tuple[int, int]] = []
    for el in root.findall("m:drawing", NS):
        drawing = rels.get(el.get(R_ID))
        droot = pkg.xml(drawing) if drawing else None
        if droot is None:
            continue
        drels = pkg.rels(drawing)
        for anchor in droot:
            for pic in anchor.iter(f"{{{NS['xdr']}}}pic"):
                blip = pic.find(".//a:blip", NS)
                if blip is None or drels.get(blip.get(R_EMBED)) not in pkg.parts:
                    continue
                images += 1
                frm = anchor.find("xdr:from", NS)
                if frm is not None:
                    anchors.append(
                        (int(frm.find("xdr:row", NS).text), int(frm.find("xdr:col", NS).text))
                    )
                src = pic.find(".//a:srcRect", NS)
                cropped += src is not None and bool(src.attrib)
                ln = pic.find("xdr:spPr/a:ln", NS)
                bordered += ln is not None and any(
                    ln.find(f"a:{fill}", NS) is not None
                    for fill in ("solidFill", "gradFill", "pattFill")
                )
        charts += len(droot.findall(".//c:chart", NS))
        shapes += len(list(droot.iter(f"{{{NS['xdr']}}}sp")))
    facts["images"] = images
    facts["image_anchor_cells"] = sorted(anchors)
    facts["images_cropped"] = cropped
    facts["images_with_border"] = bordered
    facts["charts"] = charts
    facts["shapes"] = shapes
    facts["legacy_drawing_vml"] = len(root.findall("m:legacyDrawing", NS))  # comments/controls
    facts["tables"] = len(root.findall(".//m:tableParts/m:tablePart", NS))
    rel_types = Counter(posixpath.dirname(t).split("/")[-1] for t in rels.values())
    facts["pivot_tables"] = rel_types.get("pivotTables", 0)
    facts["comments_parts"] = sum(
        1 for t in rels.values() if re.match(r"comments?\d*\.xml$", posixpath.basename(t))
    )
    return facts


def package_facts(pkg: Package) -> dict:
    wb = pkg.xml("xl/workbook.xml")
    names = wb.find("m:definedNames", NS)
    defined = (
        sorted(f"{n.get('name')}|{n.get('localSheetId', '')}" for n in names)
        if names is not None
        else []
    )
    sheets = pkg.sheets()
    sst = shared_strings(pkg)
    by_dir = Counter(posixpath.dirname(p) for p in pkg.parts)
    return {
        "size": pkg.path.stat().st_size,
        "sheets": sheets,
        "defined_names": defined,
        "media_files": sum(1 for p in pkg.parts if p.startswith("xl/media/")),
        "chart_parts": sum(1 for p in pkg.parts if re.match(r"xl/charts/chart\d+\.xml$", p)),
        "pivot_table_parts": sum(1 for p in pkg.parts if p.startswith("xl/pivotTables/p")),
        "pivot_cache_parts": sum(
            1 for p in pkg.parts if p.startswith("xl/pivotCache/pivotCacheDef")
        ),
        "external_links": sum(1 for p in pkg.parts if re.match(r"xl/externalLinks/ext", p)),
        "vba_project": int("xl/vbaProject.bin" in pkg.parts),
        "slicers": sum(1 for p in pkg.parts if p.startswith("xl/slicers/")),
        "embedded_objects": sum(1 for p in pkg.parts if p.startswith("xl/embeddings/")),
        "ctrl_props": sum(1 for p in pkg.parts if p.startswith("xl/ctrlProps/")),
        "part_dirs": by_dir,
        "media_bytes": sum(n for p, n in pkg.parts.items() if p.startswith("xl/media/")),
        "per_sheet": {name: sheet_facts(pkg, name, part, sst) for name, part in sheets},
    }


# ---------------------------------------------------------------- comparison


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def check(self, prop: str, ok: bool, detail: str) -> None:
        self.rows.append(("PASS" if ok else "FAIL", prop, detail))

    def info(self, prop: str, detail: str) -> None:
        self.rows.append(("INFO", prop, detail))

    def same(self, prop: str, before, after) -> None:
        self.check(prop, before == after, f"before={before} after={after}")

    def print(self) -> int:
        width = max(len(p) for _, p, _ in self.rows)
        for verdict, prop, detail in self.rows:
            print(f"  {verdict:<4}  {prop:<{width}}  {detail}")
        fails = sum(1 for v, _, _ in self.rows if v == "FAIL")
        print()
        print(f"OVERALL: {'FAIL' if fails else 'PASS'} ({fails} failing properties)")
        return 1 if fails else 0


def compare(a: dict, b: dict, rep: Report) -> None:
    pct = 100.0 * (b["size"] - a["size"]) / a["size"]
    # Re-zipping and re-serialising XML changes size without damage; media bytes
    # are the meaningful size comparison.
    rep.info("file size", f"before={a['size']:,} after={b['size']:,} ({pct:+.1f}%)")
    rep.same("package: media bytes", a["media_bytes"], b["media_bytes"])
    rep.same("sheet count", len(a["sheets"]), len(b["sheets"]))
    rep.same("sheet names + order", [n for n, _ in a["sheets"]], [n for n, _ in b["sheets"]])
    lost_names = sorted(set(a["defined_names"]) - set(b["defined_names"]))
    new_names = sorted(set(b["defined_names"]) - set(a["defined_names"]))
    rep.check(
        "defined names",
        not lost_names,
        f"before={len(a['defined_names'])} after={len(b['defined_names'])}"
        + (f" LOST={lost_names[:10]}" if lost_names else "")
        + (f" new={new_names[:5]}" if new_names else ""),
    )
    for key in (
        "media_files",
        "chart_parts",
        "pivot_table_parts",
        "pivot_cache_parts",
        "external_links",
        "vba_project",
        "slicers",
        "embedded_objects",
        "ctrl_props",
    ):
        rep.same(f"package: {key}", a[key], b[key])

    sheet_keys = (
        "images",
        "images_cropped",
        "images_with_border",
        "charts",
        "shapes",
        "merged_cells",
        "cond_format_rules",
        "cond_format_rules_x14",
        "data_validations",
        "data_validations_x14",
        "hyperlinks",
        "tables",
        "pivot_tables",
        "comments_parts",
        "legacy_drawing_vml",
        "header_footer_pictures",
    )
    for name, _ in a["sheets"]:
        sa, sb = a["per_sheet"].get(name, {}), b["per_sheet"].get(name)
        if sb is None:
            rep.check(f"[{name}] sheet present", False, "missing after round-trip")
            continue
        for key in sheet_keys:
            va, vb = sa.get(key, 0), sb.get(key, 0)
            if va or vb:  # only report properties the sheet actually uses
                rep.same(f"[{name}] {key}", va, vb)
        aa, ab = sa.get("image_anchor_cells", []), sb.get("image_anchor_cells", [])
        if aa or ab:
            rep.check(
                f"[{name}] image anchor cells",
                aa == ab,
                "every image anchored to the same top-left cell"
                if aa == ab
                else f"{len(set(aa) ^ set(ab))} anchors differ",
            )
        fa, fb = sa.get("formula_cells", {}), sb.get("formula_cells", {})
        if fa or fb:
            lost = sorted(set(fa) - set(fb))
            # Shared-formula children have empty <f>; openpyxl may expand them,
            # so only compare text where the original had explicit text.
            changed = sorted(r for r in fa if r in fb and fa[r] and fa[r] != fb[r])
            rep.check(
                f"[{name}] formulas",
                not lost and not changed,
                f"cells before={len(fa)} after={len(fb)}"
                + (f" LOST={lost[:8]}" if lost else "")
                + (f" CHANGED={changed[:8]}" if changed else ""),
            )
            rep.same(
                f"[{name}] formula cached values",
                sa.get("formula_cached_values", 0),
                sb.get("formula_cached_values", 0),
            )
        va, vb = sa.get("cell_values", {}), sb.get("cell_values", {})
        diff = sorted(r for r in set(va) | set(vb) if va.get(r) != vb.get(r))
        rep.check(
            f"[{name}] cell values",
            not diff,
            f"non-empty cells={len(va)}" + (f" DIFFER={diff[:8]}" if diff else " identical"),
        )

    lost_dirs = {
        d: n - b["part_dirs"].get(d, 0)
        for d, n in a["part_dirs"].items()
        if n > b["part_dirs"].get(d, 0)
    }
    # openpyxl renames parts (e.g. comments1.xml -> comments/comment1.xml), so this
    # is a pointer for investigation, not a verdict.
    rep.info(
        "package parts by folder",
        "no folder has fewer parts" if not lost_dirs else f"fewer parts in: {lost_dirs}",
    )


# ---------------------------------------------------------------- main


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def peak_rss_mb() -> str:
    try:
        import resource

        return f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:,.0f} MB"
    except ImportError:  # Windows
        return "n/a"


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "reference" / "MOP_sample.xlsx")
    if not src.is_file():
        print(f"not found: {src}", file=sys.stderr)
        return 2

    import openpyxl

    try:
        import PIL

        pil = f"Pillow {PIL.__version__}"
    except ImportError:
        pil = "NOT INSTALLED (openpyxl drops all images on load without it)"

    OUT.mkdir(exist_ok=True)
    original = OUT / f"{src.stem}.original.xlsx"
    roundtrip = OUT / f"{src.stem}.roundtrip.xlsx"
    ref_hash = sha256(src)
    shutil.copy2(src, original)

    print(f"openpyxl {openpyxl.__version__}; {pil}")
    print(f"reference: {src}")
    t0 = time.monotonic()
    wb = openpyxl.load_workbook(original)  # default mode: formulas kept as text
    t1 = time.monotonic()
    wb.save(roundtrip)
    t2 = time.monotonic()
    print(f"load {t1 - t0:.1f}s, save {t2 - t1:.1f}s, peak RSS {peak_rss_mb()}")
    print()

    rep = Report()
    rep.check(
        "reference file untouched", sha256(src) == ref_hash, "SHA-256 identical before/after run"
    )
    compare(package_facts(Package(original)), package_facts(Package(roundtrip)), rep)
    return rep.print()


if __name__ == "__main__":
    sys.exit(main())
