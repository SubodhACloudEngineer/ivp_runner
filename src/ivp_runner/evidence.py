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

    # A site renders hundreds of cards, so speed matters. FASTOCTREE is
    # deterministic and ~5x faster than MEDIANCUT. zlib level 6 is ~6x faster
    # than 9 for ~1 KB more per card (largest observed: 32 KB, under budget).
    small = img.quantize(
        colors=PALETTE_COLORS, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
    )
    buf = io.BytesIO()
    small.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


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
