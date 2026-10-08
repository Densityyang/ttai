# V4 CI Coverage Gap

## 1. V4 target

Source: `MASTER_PR_PLAN_V4.md` section 9.1 (the PR/release gate) --
`核心代码覆盖率>=90%、总体目标>=75%且不低于同口径baseline。未达到明确记录缺口，不能把普通quality绿灯当覆盖率已达标。`
That planning document is external to this repository and is not stored here, so the citation
cannot be resolved from the tree alone; the same thresholds are corroborated in-repo by
`docs-pr07a-slice1.md:38` and `docs-pr07a-slice2.md:52`.

- core coverage **>= 90%**
- overall coverage **>= 75%**, and not below the same-methodology baseline

## 2. Currently measured value (measured for this document, not copied)

Measured on branch `agent/v4-ci-hardening` at commit
`16cde3da9bfe3e5bddc02adc313235667f4641a8`, Python 3.13.14 (win32).

Raw `TOTAL` line emitted by the run in section 3:

```
TOTAL                                                  12327   4388    64%
```

That is 12,327 statements, 4,388 missed, 7,939 covered -> **64%** (64.40% before
rounding). The current baseline is below both the 75% overall target and the 90%
core target. This is an overall `src/` statement-coverage number; no separate
"core module" subset was isolated, so the gap to the 90% core target cannot be
stated as a single measured percentage here.

## 3. Exact command and exact scope

Command (PowerShell, with `$env:PYTHONIOENCODING='utf-8'`):

```
.venv/Scripts/python.exe -m pytest --cov=src --cov-report=term -q
```

Scope and result:

- `pyproject.toml` sets `testpaths = ["tests"]`, so the run collects the whole
  `tests/` tree: 817 tests collected (755 under `tests/unit/`, 62 under
  `tests/integration/`).
- Run result: `753 passed, 64 skipped, 1 warning in 87.99s`.
- Integration tests are **collected but skipped**. Both
  `tests/integration/test_postgres_governance.py` and
  `tests/integration/test_query_gateway_postgres.py` gate on
  `TTAI_RUN_POSTGRES_INTEGRATION=1`; that variable was unset, so all 62
  integration tests skipped. The other 2 skips are the POSIX-only tests in
  `tests/unit/test_secret_provider.py`, skipped on Windows by the module's own
  `@pytest.mark.skipif(os.name == "nt", ...)` markers.
- `--cov=src` measures `src/` only. Interpreter: Python 3.13.14, platform
  win32 (coverage header `python 3.13.14-final-0`).

Cross-check with a unit-only run:

```
.venv/Scripts/python.exe -m pytest tests/unit --cov=src --cov-report=term -q
TOTAL                                                  12327   4388    64%
753 passed, 2 skipped, 1 warning in 88.98s
```

The full-suite and unit-only TOTALs are identical (12,327 statements, 4,388
missed, 64%). Dropping the integration directory changes neither the statement
count nor the miss count because every integration test skipped, so the reported
number is not an artifact of collecting integration tests.

## 4. Why no `--cov-fail-under` is enforced yet

The measured overall coverage is 64%, against an overall target of 75% and a core
target of 90%. Adding a `--cov-fail-under` of 75% or 90% today would make the
quality job fail on every run for a pre-existing condition, protecting nothing.
The gate only becomes useful once the baseline is at or above the intended
threshold; enforcing it now would either block all work or have to be immediately
lowered to match the current number. The threshold should land together with (or
after) the work that closes the gap.

## 5. Honesty statement

No threshold was invented and no threshold was weakened. No `# pragma: no cover`,
`pytest.mark.skip`, or `pytest.mark.skipif` marker was added to change the
reported figure. The 64% above is the raw `TOTAL` line printed by pytest-cov on
the commit named in section 2; it is the real baseline, and it is below target.
