# ivp-runner

Automated post-deployment checks for Juniper Mist sites. It replaces the
manual "Verify AP" part of the installation verification plan (IVP) in the
customer MOP workbook.

For one site it:

1. reads AP state from the Mist API, **read-only** (GET requests only);
2. judges every AP against written pass criteria, giving a verdict per check
   and AP: PASS, FAIL, SKIP or ERROR;
3. keeps the evidence: every raw API response, plus one card (PNG) per check
   and AP;
4. fills a **copy** of the MOP workbook: the status column, and a summary
   card per row in the evidence column. The original workbook is never
   modified.

| Test ID | Checks | MOP row (IVP Test Plan) |
|---|---|---|
| AP-00 | AP is connected and reporting (precondition for the rest) | 57 |
| AP-01 | Expected number of SSIDs on each enabled band (partial: counts, not names) | 58 |
| AP-02 | AP is not power-constrained (full PoE power) | 59 |
| AP-03 | Management IP in the expected subnet; DNS servers as designed (partial) | 60 |
| AP-04 | Uplink up, at or above the expected speed, full duplex | 61 |
| AP-05 | No new RX errors on the uplink during the run (partial: no TX counter) | 61 |

"Partial" checks state their limitation on every card. See all criteria in
plain English with:

```bash
python -m ivp_runner.catalogue explain catalogue/ap.yaml
```

## What the verdicts mean

| Verdict | Meaning | MOP status |
|---|---|---|
| PASS | The AP was judged and meets the criterion. | Complete |
| FAIL | The AP was judged and does **not** meet it. This is a finding about the network. | Issue Reported |
| ERROR | The AP could **not** be judged: data or a design value was missing, or malformed. This is a gap in the evidence, never a finding. | Not Started |
| SKIP | Not run because its precondition (AP-00) failed for that AP. | Not Started |

A MOP row's status is the worst verdict across its APs (FAIL > ERROR > PASS
> SKIP). ERROR and SKIP leave the row as "Not Started": a person must look at
it.

## Running it

You need Python 3.11+, network access to the Mist API, and a Mist API token
with at least read-only access to the site.

```bash
pip install -e .

# 1. Design values for the site, from the LLD (not from Mist):
cp sites/TEMPLATE.yaml sites/<site-id>.yaml
#    fill in uplink_port, ssids, mgmt_subnet, dns_servers, min_uplink_speed_mbps

# 2. The token: typed at a hidden prompt, held only in this shell.
read -rs MIST_API_TOKEN && export MIST_API_TOKEN

# 3. Run.
ivp run --site <site-id> --org <org-id> --api-host api.eu.mist.com \
        --catalogue catalogue/ap.yaml --mop <path/to/MOP.xlsx> --out out/

unset MIST_API_TOKEN
```

- **`--api-host`** is the Mist cloud the org lives on: `api.mist.com`
  (global), `api.eu.mist.com` (EU), and so on. It has no default, so you
  can't query the wrong cloud by accident.
- **`--dry-run`** collects and judges as usual but writes no workbook. In
  that mode `--mop` is optional; if you give it, the workbook is still
  checked.
- **`--json`** prints `results.json` to stdout instead of the table.
  Progress always goes to stderr.

Each run writes one folder, `out/<run-id>/`:

| Path | Contents |
|---|---|
| `results.json` | run header, plus one record per check and AP |
| `raw/` | every API response, byte for byte, with `manifest.json` |
| `evidence/<test-id>/` | one PNG card and one JSON file per AP |
| `MOP_<site>_<run-id>.xlsx` | the populated copy of the workbook |
| `run.log` | timestamped steps |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | every check passed (SKIP alone doesn't count) |
| 1 | at least one FAIL, and no ERROR |
| 2 | at least one ERROR (this wins over FAIL: the evidence is incomplete) |
| 3 | the run could not be completed (see below) |

### When it can't run

Problems that would make every check fail the same way are caught before or
at the first API request. The tool prints the problem and what to do, then
exits 3. It never shows a stack trace, and never reports one ERROR per AP.
For example:

```
RUN FAILED - the checks were not completed.
  Problem:    Mist rejected the API token (HTTP 401 from api.eu.mist.com)
  What to do: The token is wrong, expired or revoked, or it was created on a different
              Mist cloud: tokens only work on their own cloud ...
```

| Situation | What you'll see |
|---|---|
| No token, or one pasted with spaces or a `Token ` prefix | `MIST_API_TOKEN is not set` / `contains a space at character N` |
| Wrong, expired or wrong-cloud token | `Mist rejected the API token (HTTP 401 ...)` |
| Token has no access to the site | `the API token cannot read site ... (HTTP 403 ...)` |
| Wrong site ID | `site ... was not found on <host> (HTTP 404)`; a site *name* instead of an ID is caught before any request |
| Host name typo | `cannot resolve <host> (DNS lookup failed)` |
| Firewall or proxy in the way | `connection to <host>:443 was refused` / `no usable response from <host>`, with a `curl` line to test it |
| TLS inspection on the network | `TLS certificate verification failed`, telling you to set `REQUESTS_CA_BUNDLE` |
| Portal URL given instead of the API host | `answered with something that is not JSON`, telling you to use `api.…`, not `manage.…` |
| Missing, non-xlsx or wrong-version MOP | caught before any API request |
| Profile for another site, or invalid values | caught before any API request |

If only part of the data can't be collected (for example the run-end sample
gets a Mist 500), the run completes. The checks that needed that data are
ERROR, and the cause is printed once above the totals.

## Adding a check (worked example: AP-06)

A check is data, not code. As an example, add "AP sees its upstream switch
over LLDP".

**1. Find the fields in `docs/field_inventory.md`.** Every field a check
reads must already be listed there, because it was seen in a real captured
API response. `lldp_stat.system_name` and `lldp_stat.port_id` are listed. If
the field you need isn't, capture it first (`scripts/capture_samples.py`)
and add it to the inventory. Never guess a field name: the catalogue loader
rejects any path that isn't in the inventory.

**2. Append the check to `catalogue/ap.yaml`.**

```yaml
  - test_id: AP-06
    title: AP sees its upstream switch over LLDP
    mop:
      section: Verify AP
      description: Check the AP's LLDP neighbour
    site_classes: [all]
    coverage: full
    requires: [AP-00]          # SKIP when the AP isn't connected
    collect: {method: mist_api, source: site_device_stats}
    target: {kind: ap, select: {field: type, equals: ap}}
    pass_when:
      all:
        - {field: lldp_stat.system_name, present: true}
        - {field: lldp_stat.port_id, present: true}
    evidence: [lldp_stat.system_name, lldp_stat.port_id, lldp_stat.ap_port_name]
```

Test IDs are permanent. Never renumber or reuse one, even if the title
changes.

Operators: `equals`, `at_least`, `in_subnet`, `same_set_as`, `present`,
`unchanged_during_run` (compares the run-start and run-end readings), and
`for_each` to repeat assertions per key, e.g. per radio band. A design value
comes from the site profile, e.g. `at_least: {expect: min_uplink_speed_mbps}`.
A new design value needs a field in `Expectations` in
`src/ivp_runner/site_profile.py` and a line in `sites/TEMPLATE.yaml`.

**3. Review the sentence it will be judged by.**

```bash
python -m ivp_runner.catalogue explain catalogue/ap.yaml
```
```
AP-06  AP sees its upstream switch over LLDP
  Only if PASS: AP-00 (otherwise SKIP)
  PASS when ALL of:
    - lldp_stat.system_name is reported
    - lldp_stat.port_id is reported
```

**4. Optionally, put it in the MOP.** Add a row to
`catalogue/mop_mapping.yaml` with the row number and the start of that row's
description, or add the test ID to an existing row. Unmapped checks still get
results and evidence; they just don't appear in the workbook.

**5. Add tests and run them.** In `tests/`, using the builders in
`tests/factories.py`, add an AP that passes and one that fails. If the check
reads a design value or compares two readings, also add one that can't be
judged (ERROR). `tests/test_readme.py` does this for AP-06, and
`tests/test_error_vs_fail.py` lists the ERROR cases for each operator.

```bash
pytest -q && ruff check . && ruff format --check .
```

## Safety rules

The full list is in `CLAUDE.md`.

- **Read-only.** There is no code path that sends anything but GET.
- **The token** comes only from `MIST_API_TOKEN`. It is never written to
  disk, logged or printed, and it is scrubbed from saved responses.
- **Customer data stays local.** `reference/`, `samples/`, `out/` and real
  `sites/*.yaml` files are git-ignored. Test fixtures are pseudonymised.
- **Tests make no network calls.**

## Development

```bash
pip install -e '.[dev]'
pytest -q --cov=ivp_runner --cov-branch
```
