"""P9A benchmark test package.

This marker matters for more than tidiness: pytest inserts the FIRST directory
above a test file that is not a package into sys.path.  `benchmarks/` is a
package but `benchmarks/tests/` was not, so pytest stopped at `benchmarks/tests/`
and `import benchmarks...` failed under a plain `pytest` invocation (CI) while
passing under `python -m pytest` (which prepends the CWD).  With this marker the
walk continues past `benchmarks/` to the repository root, exactly as it does for
`tests/`, so `benchmarks` is importable in both invocations.
"""
