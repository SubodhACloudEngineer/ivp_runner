# ivp_runner

Post-deployment IVP (installation verification plan) runner for Juniper Mist
sites. It runs the AP checks in `catalogue/ap.yaml` against the Mist API
(read-only), records a verdict per check and device with evidence, and fills
the status column of a copy of the MOP workbook.

## Run

```bash
pip install -e .
cp sites/TEMPLATE.yaml sites/<site-id>.yaml    # fill from the LLD, not from Mist
read -rs MIST_API_TOKEN && export MIST_API_TOKEN

ivp run --site <site-id> --org <org-id> --api-host api.eu.mist.com \
        --catalogue catalogue/ap.yaml --mop reference/MOP_sample.xlsx --out out/
unset MIST_API_TOKEN
```

`--json` prints `results.json` to stdout instead of the table. Progress always
goes to stderr.

Everything for a run is written to `out/<run-id>/`: `results.json`, `raw/` (API
responses plus manifest), `evidence/` (PNG cards and JSON), the populated
`MOP_*.xlsx` copy, and `run.log`. The reference workbook is only ever read.

| Exit code | Meaning |
|---|---|
| 0 | every check passed (SKIP alone doesn't count) |
| 1 | at least one FAIL, no ERROR |
| 2 | at least one ERROR: a check could not be executed (wins over FAIL) |
| 3 | the tool could not complete (no token, bad catalogue or profile, MOP template mismatch) |

Review what each check asserts: `python -m ivp_runner.catalogue explain catalogue/ap.yaml`
