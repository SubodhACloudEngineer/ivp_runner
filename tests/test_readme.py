"""The README's "Adding a check" walkthrough must keep working as written."""

import re
from pathlib import Path

from ivp_runner.assert_engine import evaluate
from ivp_runner.catalogue import explain, load_catalogue
from ivp_runner.results import Reason, Verdict

from .factories import CATALOGUE, ap, context, disconnected, payloads, profile

README = Path(__file__).resolve().parent.parent / "README.md"


def readme_catalogue(tmp_path):
    text = README.read_text(encoding="utf-8")
    (snippet,) = [s for s in re.findall(r"```yaml\n(.*?)```", text, re.S) if "AP-06" in s]
    path = tmp_path / "ap.yaml"
    path.write_text(CATALOGUE.read_text(encoding="utf-8") + "\n" + snippet, encoding="utf-8")
    return load_catalogue(path)


def ap06(tmp_path, item):
    evs = evaluate(readme_catalogue(tmp_path), profile(), *payloads([item]), context())
    (r,) = [e.result for e in evs if e.result.test_id == "AP-06"]
    return r


def test_readme_example_loads_and_explains_as_documented(tmp_path):
    out = explain(readme_catalogue(tmp_path))
    assert "AP-06  AP sees its upstream switch over LLDP" in out
    assert "lldp_stat.system_name is reported" in out


def test_readme_example_pass_fail_skip(tmp_path):
    assert ap06(tmp_path, ap(1)).verdict is Verdict.PASS

    no_neighbour = ap(2)
    del no_neighbour["lldp_stat"]["port_id"]
    r = ap06(tmp_path, no_neighbour)
    assert (r.verdict, r.reason) == (Verdict.FAIL, Reason.CRITERIA_NOT_MET)

    r = ap06(tmp_path, disconnected(9))
    assert (r.verdict, r.reason) == (Verdict.SKIP, Reason.PRECONDITION_FAILED)
