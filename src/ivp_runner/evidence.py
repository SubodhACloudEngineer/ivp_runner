"""Evidence cards: one PNG per result, replacing hand-made Mist UI screenshots.

Why Pillow rather than matplotlib:
* A card is text, a coloured verdict band and a few rules. That is direct
  pixel drawing, which Pillow does in a few calls. matplotlib is a plotting
  library: it would add a figure/axes model, a large dependency, a font
  cache, and backend version strings written into the PNG metadata.
* Determinism: Pillow writes no timestamp or software tag unless asked.
  It ships its own FreeType font (Aileron, via ``ImageFont.load_default``),
  so output doesn't depend on which fonts the machine has. The same Pillow
  version gives byte-identical files.
* Size: drawing in RGB then quantising to a 32-colour palette (fast octree,
  no dithering), gives small PNGs (well under the ~40 KB budget) and keeps text
  anti-aliased.

The card puts what matters first, in the largest type: verdict, test ID,
device. It stays legible when Excel scales it down to fit a row.

Only ASCII is drawn. The bundled font has no glyphs for symbols like the
check mark or arrows, so any other character is replaced rather than shown
as an empty box.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ivp_runner.catalogue import Catalogue, Check
from ivp_runner.results import TestResult, Verdict

WIDTH = 880
PAD = 18
LABEL_W = 118
LINE_GAP = 4
SECTION_GAP = 9
PALETTE_COLORS = 32

INK = (31, 35, 40)
MUTED = (87, 96, 106)
RULE = (208, 215, 222)
BG = (255, 255, 255)
PANEL = (246, 248, 250)
VERDICT_COLORS = {
    Verdict.PASS: (26, 127, 55),  # green
    Verdict.FAIL: (207, 34, 46),  # red
    Verdict.ERROR: (154, 103, 0),  # amber: could not be executed
    Verdict.SKIP: (110, 119, 129),  # grey: not evaluated
}
VERDICT_NOTE = {
    Verdict.PASS: "criterion met",
    Verdict.FAIL: "criterion NOT met",
    Verdict.ERROR: "could not be evaluated",
    Verdict.SKIP: "not evaluated (precondition)",
}


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


F_VERDICT = _font(34)
F_TITLE = _font(19)
F_BODY = _font(15)
F_SMALL = _font(13)


def _ascii(text: str) -> str:
    """Characters the bundled font can draw; anything else becomes '?'."""
    text = text.replace("—", "-").replace("–", "-").replace("…", "...")
    return "".join(ch if 32 <= ord(ch) < 127 or ch == "\n" else "?" for ch in text)


@lru_cache(maxsize=65536)
def _length(font: ImageFont.FreeTypeFont, text: str) -> float:
    """Cached text width: cards repeat the same labels and words hundreds of times."""
    return font.getlength(text)


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Greedy word wrap. Widths are summed per word (cached), not per candidate line."""
    space = _length(font, " ")
    lines: list[str] = []
    for para in _ascii(text).split("\n"):
        line, line_w = "", 0.0
        for w in para.split(" "):
            w_w = _length(font, w)
            if not line:
                candidate_w = w_w
            else:
                candidate_w = line_w + space + w_w
            if candidate_w <= width:
                line, line_w = (f"{line} {w}" if line else w), candidate_w
                continue
            if line:
                lines.append(line)
            while w_w > width:  # very long token: hard break
                cut = max(1, int(len(w) * width / w_w))
                lines.append(w[:cut])
                w = w[cut:]
                w_w = _length(font, w)
            line, line_w = w, w_w
        lines.append(line)
    return lines


def _fmt(value: Any) -> str:
    if value is None:
        return "(not reported)"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, dict) and {"start", "end", "delta"} <= value.keys():
        d = value["delta"]
        sign = "+" if isinstance(d, int | float) and d > 0 else ""
        return f"start {value['start']}, end {value['end']}, change {sign}{d}"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value) or "(empty)"
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _source_line(check: Check | None, result: TestResult) -> str:
    if check is None:
        return result.method
    endpoint = check.collect.source
    if check.collect.method == "mist_api":
        from ivp_runner.collectors.mist_api import SOURCES

        spec = SOURCES.get(check.collect.source)
        if spec is not None:
            query = "&".join(f"{k}={v}" for k, v in spec.params.items())
            endpoint = f"GET {spec.path}" + (f"?{query}" if query else "")
    return f"{endpoint}  ({check.collect.method}: {check.collect.source})"


def _raw_lines(result: TestResult) -> list[str]:
    return [
        f"{ref.sample}: {Path(ref.path).name} #{ref.pointer}  sha256 {ref.sha256[:12]}"
        for ref in result.raw_ref
    ]


def card_rows(result: TestResult, check: Check | None) -> list[tuple[str, list[tuple[str, Any]]]]:
    """(label, [(text, colour)]) rows for the card body. Pure; used by render_card and tests."""
    dev = result.device
    rows: list[tuple[str, list[tuple[str, Any]]]] = []
    if check is not None:
        rows.append(("MOP", [(f"{check.mop.section} / {check.mop.description.strip()}", INK)]))
    rows.append(("Site", [(f"{result.site.name}  ({result.site.site_class})", INK)]))
    if dev is None:
        rows.append(("Device", [("whole site (no device data)", MUTED)]))
    else:
        bits = [dev.name or dev.id]
        if dev.mac:
            bits.append(f"MAC {dev.mac}")
        if dev.model:
            bits.append(dev.model)
        rows.append(("Device", [("   ".join(bits), INK)]))

    if result.assertions:
        rows.append(("Criterion", [(f"- {a.rule}", INK) for a in result.assertions]))
        observed = []
        for a in result.assertions:
            mark = VERDICT_COLORS[a.verdict]
            line = f"[{a.verdict.value}] {a.field} = {_fmt(a.actual)}"
            if a.expected is not None:
                line += f"   (expected: {_fmt(a.expected)})"
            observed.append((line, mark))
        rows.append(("Observed", observed))
    elif check is not None:
        rows.append(("Criterion", [(f"- {r}", INK) for r in _criterion_text(check)]))
        rows.append(("Observed", [("nothing evaluated", MUTED)]))

    # "Why" carries what Observed can't show: why nothing was evaluated, or why
    # an evaluated value could not be judged. A plain FAIL is already explained.
    if result.message and (not result.assertions or result.verdict is Verdict.ERROR):
        rows.append(("Why", [(result.message, VERDICT_COLORS[result.verdict])]))
    if check is not None and check.coverage == "partial" and check.limitation:
        # The whole limitation: on evidence, a half-stated caveat misleads.
        rows.append(("Partial", [(" ".join(check.limitation.split()), MUTED)]))
    rows.append(
        (
            "Time",
            [(f"{result.timestamp_utc} UTC    {result.timestamp_local} {result.timezone}", INK)],
        )
    )
    rows.append(
        ("Source", [(_source_line(check, result), INK)] + [(r, MUTED) for r in _raw_lines(result)])
    )
    return rows


def _criterion_text(check: Check) -> list[str]:
    from ivp_runner.criteria import describe

    return [line.strip() for line in describe(check.pass_when)]


def render_card(result: TestResult, check: Check | None) -> bytes:
    """PNG bytes for one result. Same input, same Pillow version: same bytes."""
    color = VERDICT_COLORS[result.verdict]
    value_w = WIDTH - 2 * PAD - LABEL_W

    # Layout pass: wrap everything and compute the height.
    title = check.title if check else ""
    title_lines = _wrap(f"{result.test_id}  {title}", F_TITLE, WIDTH - 2 * PAD - 190)
    band_h = max(78, 18 + len(title_lines) * (F_TITLE.size + LINE_GAP) + 12)
    body: list[tuple[str, list[tuple[str, Any]]]] = []
    height = band_h + PAD
    for label, parts in card_rows(result, check):
        wrapped = [(line, c) for text, c in parts for line in _wrap(text, F_BODY, value_w)]
        body.append((label, wrapped))
        height += len(wrapped) * (F_BODY.size + LINE_GAP) + SECTION_GAP
    footer = f"run {result.run_id}   ivp-runner evidence card"
    height += SECTION_GAP + F_SMALL.size + PAD

    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)

    # Verdict band: colour plus the word, so it reads in greyscale too.
    d.rectangle([0, 0, WIDTH - 1, band_h - 1], fill=color)
    d.text((PAD, 10), result.verdict.value, font=F_VERDICT, fill=BG)
    d.text((PAD, 10 + F_VERDICT.size + 8), VERDICT_NOTE[result.verdict], font=F_SMALL, fill=BG)
    y = 14
    for line in title_lines:
        d.text((PAD + 190, y), line, font=F_TITLE, fill=BG)
        y += F_TITLE.size + LINE_GAP

    y = band_h + PAD // 2
    for i, (label, lines) in enumerate(body):
        block_h = len(lines) * (F_BODY.size + LINE_GAP)
        if i % 2 == 0:
            d.rectangle([0, y - 3, WIDTH - 1, y + block_h + 2], fill=PANEL)
        d.text((PAD, y), label, font=F_BODY, fill=MUTED)
        for line, c in lines:
            d.text((PAD + LABEL_W, y), line, font=F_BODY, fill=c)
            y += F_BODY.size + LINE_GAP
        y += SECTION_GAP
    d.line([PAD, y, WIDTH - PAD, y], fill=RULE, width=1)
    d.text((PAD, y + SECTION_GAP // 2 + 2), _ascii(footer), font=F_SMALL, fill=MUTED)
    d.rectangle([0, 0, WIDTH - 1, height - 1], outline=color, width=3)

    return _to_png(img)


def _to_png(img: Image.Image) -> bytes:
    # A site renders hundreds of cards, so speed matters. FASTOCTREE is
    # deterministic and ~5x faster than MEDIANCUT. zlib level 6 is ~6x faster
    # than 9 for ~1 KB more per card (largest observed: 32 KB, under budget).
    small = img.quantize(
        colors=PALETTE_COLORS, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
    )
    buf = io.BytesIO()
    small.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


# ---------------------------------------------------------------- row summary card

ROW_WIDTH = 620  # fits the MOP's Evidence column (~637 px) without scaling
ROW_MAX_NAMES = 12
F_ROW_VERDICT = _font(26)
F_ROW_TITLE = _font(15)
F_ROW_BODY = _font(13)


def render_row_card(
    test_ids: list[str], results: Iterable[TestResult], catalogue: Catalogue
) -> bytes:
    """One compact card summarising every device for the checks on one MOP row.

    Same deterministic Pillow pipeline as ``render_card``. The per-device cards
    stay under ``evidence/<test-id>/`` and the card says so.
    """
    from collections import Counter

    from ivp_runner.results import rollup

    by_test: dict[str, list[TestResult]] = {t: [] for t in test_ids}
    for r in results:
        if r.test_id in by_test:
            by_test[r.test_id].append(r)
    present = [t for t in test_ids if by_test[t]]
    verdict = rollup(r.verdict for t in present for r in by_test[t])
    color = VERDICT_COLORS[verdict]
    checks = {c.test_id: c for c in catalogue.checks}
    text_w = ROW_WIDTH - 2 * PAD
    line_h = F_ROW_BODY.size + 3

    # Layout pass.
    blocks: list[tuple[str, list[tuple[str, Any, Any]], Counter]] = []
    latest = max((r.timestamp_utc for t in present for r in by_test[t]), default=None)
    for t in present:
        rs = by_test[t]
        check = checks.get(t)
        counts = Counter(r.verdict for r in rs)
        lines: list[tuple[str, Any, Any]] = []
        for v in (Verdict.FAIL, Verdict.ERROR):
            groups: dict[str, list[TestResult]] = {}
            for r in rs:
                if r.verdict is v:
                    groups.setdefault(r.reason.value if r.reason else "", []).append(r)
            for reason, members in groups.items():
                names = [(m.device.name or m.device.id) if m.device else "site" for m in members]
                if len(names) > ROW_MAX_NAMES and members[0].message:
                    text = f"{v.value} {reason}: {len(names)} APs - {members[0].message}"
                else:
                    shown = ", ".join(names[:ROW_MAX_NAMES])
                    more = (
                        f" +{len(names) - ROW_MAX_NAMES} more" if len(names) > ROW_MAX_NAMES else ""
                    )
                    text = f"{v.value} {reason}: {shown}{more}"
                for ln in _wrap(text, F_ROW_BODY, text_w)[:3]:
                    lines.append((ln, VERDICT_COLORS[v], F_ROW_BODY))
        if check is not None and check.coverage == "partial" and check.limitation:
            first = " ".join(check.limitation.split()).split(". ")[0].rstrip(".")
            for ln in _wrap(f"Partial check: {first}.", F_ROW_BODY, text_w)[:2]:
                lines.append((ln, MUTED, F_ROW_BODY))
        title = f"{t}  {check.title if check else ''}"
        blocks.append((_wrap(title, F_ROW_TITLE, text_w)[0], lines, counts))

    band_h = 44
    height = band_h + 8
    for _, lines, _ in blocks:
        height += (F_ROW_TITLE.size + 4) + 12 + (line_h + 2) + len(lines) * line_h + 8
    tz_line = ""
    if latest:
        r0 = next(r for t in present for r in by_test[t] if r.timestamp_utc == latest)
        tz_line = f"{r0.timestamp_utc} UTC   {r0.timestamp_local} {r0.timezone}"
    pointer = "Per-AP cards: " + ", ".join(f"evidence/{t}/" for t in present)
    footer = [ln for txt in (tz_line, pointer) if txt for ln in _wrap(txt, F_SMALL, text_w)]
    height += 4 + len(footer) * (F_SMALL.size + 3) + 8

    img = Image.new("RGB", (ROW_WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, ROW_WIDTH - 1, band_h - 1], fill=color)
    d.text((PAD, 8), verdict.value, font=F_ROW_VERDICT, fill=BG)
    note = f"{VERDICT_NOTE[verdict]}  -  {sum(len(by_test[t]) for t in present)} results"
    d.text((PAD + 110, 16), _ascii(note), font=F_ROW_BODY, fill=BG)

    y = band_h + 8
    for title, lines, counts in blocks:
        d.text((PAD, y), title, font=F_ROW_TITLE, fill=INK)
        y += F_ROW_TITLE.size + 4
        total = sum(counts.values()) or 1
        x = PAD
        for v in (Verdict.PASS, Verdict.FAIL, Verdict.ERROR, Verdict.SKIP):
            if counts[v]:
                w = max(2, round(text_w * counts[v] / total))
                d.rectangle([x, y, min(x + w, PAD + text_w) - 1, y + 7], fill=VERDICT_COLORS[v])
                x += w
        y += 12
        summary = "   ".join(f"{counts[v]} {v.value}" for v in Verdict if counts[v])
        d.text((PAD, y), summary, font=F_ROW_BODY, fill=INK)
        y += line_h + 2
        for ln, c, f in lines:
            d.text((PAD, y), ln, font=f, fill=c)
            y += line_h
        y += 8
    d.line([PAD, y, ROW_WIDTH - PAD, y], fill=RULE, width=1)
    y += 4
    for ln in footer:
        d.text((PAD, y), ln, font=F_SMALL, fill=MUTED)
        y += F_SMALL.size + 3
    d.rectangle([0, 0, ROW_WIDTH - 1, height - 1], outline=color, width=3)
    return _to_png(img)


def write_cards(results: Iterable[TestResult], catalogue: Catalogue) -> list[Path]:
    """Render each result's card to its evidence_path. Returns the paths written."""
    by_id = {c.test_id: c for c in catalogue.checks}
    written = []
    for r in results:
        if not r.evidence_path:
            continue
        path = Path(r.evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_card(r, by_id.get(r.test_id)))
        written.append(path)
    return written
