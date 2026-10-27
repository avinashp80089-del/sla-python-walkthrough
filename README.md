# SLA Semantic Release Guard

A Python demonstration of detecting semantic data drift and preventing an
unsafe SLA release. The example uses fictional fulfillment data and a
one-hour, order-level SLA.

## What it demonstrates

The original feed contains one row per order and reports durations in seconds.
The changed feed keeps the same columns but reports one row per package in
milliseconds. Treating the changed feed as if it still had the original meaning
can produce misleading results.

The validation workflow checks schema and semantic contracts, verifies the
complete expected package population, converts package durations to order
durations using the latest package completion, and compares results against
independently specified expected data. It blocks publication when the changed
feed does not meet the approved contract and preserves the active release.

For the synthetic baseline, 6 of 12 orders meet the SLA: 50% compliance and
50% breach rate. An order completing at exactly 3,600 seconds is compliant.

## Requirements

- Python 3.12 or newer
- `pandas==3.1.0`
- `pytest==9.1.1` for the test suite

Create a virtual environment and install the pinned dependencies:

```bash
python3 setup_project.py --install
source .venv/bin/activate
```

## Run the workflow

Prepare a fresh session, then evaluate the baseline, changed, and corrected
scenarios:

```bash
python 03_workflow.py prepare
python 03_workflow.py baseline
python 03_workflow.py changed
python 03_workflow.py corrected
```

Evaluation does not publish. To publish a passing result, publish the most
recent run for its scenario explicitly:

```bash
python 03_workflow.py publish baseline
```

The `changed` scenario is expected to be blocked from publication. To use
another session rather than the default `take1`, pass `--session NAME` before
the action, for example:

```bash
python 03_workflow.py --session take2 prepare
python 03_workflow.py --session take2 baseline
```

Running `03_workflow.py` without an action displays help. Available actions
include `prepare`, `baseline`, `changed`, `corrected`, `publish`, `rollback`,
`impact`, `status`, and `export`.

The package CLI also offers a complete demonstration in its own workspace:

```bash
python -m sla_guard demo
```

## Inspect evidence and run tests

The SQL evidence script opens the session database read-only. With no query it
shows the order and package counts. The `expected` query calculates the
baseline compliance rates from the source feed:

```bash
python 05_sql_evidence.py
python 05_sql_evidence.py expected
python 05_sql_evidence.py trace
python 05_sql_evidence.py active
python 05_sql_evidence.py kpi
python 05_sql_evidence.py rows
python 05_sql_evidence.py history
```

The `kpi` and `rows` queries inspect the active published release and therefore
return no publication results until a release has been published.

Run the business-rule and release-integrity tests with:

```bash
python 04_verify_tests.py
```

Run only the exact-SLA-boundary cases with:

```bash
python 04_verify_tests.py --boundary
```

## Project layout

- `sla_guard/` — validation, release management, and command-line interface
- `tests/` — business-rule, boundary, integrity, and packaging tests
- `.runtime/inputs/` — generated synthetic fixtures and contracts
- `01_task_description.py` — task and scenario description
- `02_inputs_and_contract.py` — semantic contract and synthetic fixture setup
- `03_workflow.py` — explicit scenario evaluation and release actions
- `04_verify_tests.py` — focused test runner
- `05_sql_evidence.py` — read-only SQL evidence queries

Generated input files, databases, reports, and session state are stored under
`.runtime/`.
