import hashlib
import io
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from ivp_runner.assert_engine import evaluate
from ivp_runner.evidence import F_BODY, VERDICT_COLORS, _wrap, card_rows, render_card, write_cards
from ivp_runner.results import Verdict

from .factories import ap, catalogue, context, payload, payloads, profile
from .fixtures_mist import find, fixture_profile, stats_items

ROOT = Path(__file__).resolve().parent.parent
BUDGET = 40 * 1024


@pytest.fixture(scope="module")
def results():
    items = stats_items()
    target = find(items, status="connected")
    target["port_stat"]["eth0"]["rx_errors"] = 0
    end = stats_items()
    find(end, name=target["name"])["port_stat"]["eth0"]["rx_errors"] = 4
    evs = evaluate(
        catalogue(),
        fixture_profile(),
        {"site_device_stats": payload(items)},
        {"site_device_stats": payload(end, "end")},
        context(),
    )
    return [e.result for e in evs]


def pick(results, test_id, verdict):
    return next(r for r in results if r.test_id == test_id and r.verdict is verdict)


def checks():
    return {c.test_id: c for c in catalogue().checks}


def one_of_each(results):
    seen = {}
    for r in results:
        seen.setdefault((r.test_id, r.verdict), r)
    return list(seen.values())


def rows_of(r):
    return {
        label: " | ".join(t for t, _ in parts) for label, parts in card_rows(r, checks()[r.test_id])
    }


def band_colour(png: bytes):
    img = Image.open(io.BytesIO(png)).convert("RGB")
    return img.getpixel((img.width - 10, 8))


def close(a, b, tol=8):
    return max(abs(x - y) for x, y in zip(a, b, strict=True)) <= tol


def test_rendering_is_byte_identical(results):
    r = pick(results, "AP-05", Verdict.FAIL)
    assert render_card(r, checks()["AP-05"]) == render_card(r, checks()["AP-05"])


def test_rendering_is_byte_identical_across_processes(results):
    r = pick(results, "AP-04", Verdict.FAIL)
    here = hashlib.sha256(render_card(r, checks()["AP-04"])).hexdigest()
    code = (
        "import hashlib, sys\n"
        "from ivp_runner.results import TestResult\n"
        "from ivp_runner.catalogue import load_catalogue\n"
        "from ivp_runner.evidence import render_card\n"
        "r = TestResult.model_validate_json(sys.stdin.read())\n"
        "c = load_catalogue('catalogue/ap.yaml').get(r.test_id)\n"
        "print(hashlib.sha256(render_card(r, c)).hexdigest())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        input=r.model_dump_json(by_alias=True),
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    assert out.stdout.strip() == here


def test_every_kind_of_card_fits_the_size_budget(results):
    sizes = {
        (r.test_id, r.verdict.value): len(render_card(r, checks()[r.test_id]))
        for r in one_of_each(results)
    }
    assert len(sizes) >= 11  # PASS/FAIL/SKIP across AP-00..05
    assert max(sizes.values()) < BUDGET, sizes


def test_png_has_no_metadata_chunks(results):
    png = render_card(pick(results, "AP-02", Verdict.PASS), checks()["AP-02"])
    pos, chunks = 8, []
    while pos < len(png):
        length = int.from_bytes(png[pos : pos + 4], "big")
        chunks.append(png[pos + 4 : pos + 8].decode())
        pos += 12 + length
    assert not {"tEXt", "iTXt", "zTXt", "tIME"} & set(chunks), chunks
    assert Image.open(io.BytesIO(png)).mode == "P"


@pytest.mark.parametrize("verdict", [Verdict.PASS, Verdict.FAIL, Verdict.SKIP])
def test_verdict_band_colour_is_unmistakable(results, verdict):
    r = pick(results, "AP-02", verdict)
    assert close(band_colour(render_card(r, checks()["AP-02"])), VERDICT_COLORS[verdict])


def test_error_band_is_amber():
    evs = evaluate(catalogue(), profile(mgmt_subnet=None), *payloads([ap(1)]), context())
    r = next(e.result for e in evs if e.result.test_id == "AP-03")
    assert r.verdict is Verdict.ERROR
    assert close(band_colour(render_card(r, checks()["AP-03"])), VERDICT_COLORS[Verdict.ERROR])


def test_card_carries_every_required_field(results):
    r = pick(results, "AP-05", Verdict.FAIL)
    rows = rows_of(r)
    assert "Make sure the Ethernet properties are correct" in rows["MOP"]
    assert r.site.name in rows["Site"]
    assert r.device.name in rows["Device"] and f"MAC {r.device.mac}" in rows["Device"]
    assert "is the same at run start and run end" in rows["Criterion"]
    assert "[FAIL] port_stat.eth0.rx_errors = start 0, end 4, change +4" in rows["Observed"]
    assert "expected: start 0, end 0, change 0" in rows["Observed"]
    assert f"{r.timestamp_utc} UTC" in rows["Time"] and "Europe/Madrid" in rows["Time"]
    assert "GET /api/v1/sites/{site_id}/stats/devices?type=ap" in rows["Source"]
    assert "site_device_stats.start.json #" in rows["Source"]
    assert "no TX error counter" in rows["Partial"]


def test_skip_card_says_why(results):
    rows = rows_of(pick(results, "AP-02", Verdict.SKIP))
    assert rows["Observed"] == "nothing evaluated"
    assert "precondition AP-00 did not pass" in rows["Why"]
    assert rows["Criterion"] == "- power_constrained equals false"


def test_placeholders_are_resolved_in_rules(results):
    rules = [a.rule for a in pick(results, "AP-01", Verdict.FAIL).assertions]
    assert all("<" not in rule for rule in rules)
    assert any("ssid_count.band_5" in rule for rule in rules)


def test_non_ascii_text_is_replaced_not_boxed(results):
    base = pick(results, "AP-02", Verdict.PASS)
    r = base.model_copy(update={"site": base.site.model_copy(update={"name": "Café → Site"})})
    site_text = dict(card_rows(r, checks()["AP-02"]))["Site"][0][0]
    assert _wrap(site_text, F_BODY, 600)[0].startswith("Caf? ? Site")
    render_card(r, checks()["AP-02"])


def test_write_cards_writes_to_evidence_path(results, tmp_path):
    rs = [
        r.model_copy(update={"evidence_path": str(tmp_path / r.test_id / "x.png")})
        for r in one_of_each(results)[:3]
    ]
    written = write_cards(rs, catalogue())
    assert [p.name for p in written] == ["x.png"] * 3
    assert all(p.read_bytes().startswith(b"\x89PNG") for p in written)
