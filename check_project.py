"""Run this project's full unittest suite and the managed joint verification."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
    if suite.countTestCases():
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    else:
        print('No tests discovered in tests/', file=sys.stderr)
        result = unittest.TestResult()
    for status, failed_cases in (
        ('FAIL', (test for test, _ in result.failures)),
        ('ERROR', (test for test, _ in result.errors)),
        ('UNEXPECTED_SUCCESS', result.unexpectedSuccesses),
    ):
        for test in failed_cases:
            print(f'{status} {test.id()}')
    successful = result.testsRun > 0 and result.wasSuccessful()
    print(f'TEST SUMMARY total={result.testsRun} failures={len(result.failures)} '
          f'errors={len(result.errors)} skipped={len(result.skipped)} '
          f'expected_failures={len(result.expectedFailures)} '
          f'unexpected_successes={len(result.unexpectedSuccesses)} '
          f'result={"PASS" if successful else "FAIL"}')
    if not successful:
        return 1
    for mode in ('v1', 'managed'):
        print(f'MANAGED EXAMPLES {mode}')
        completed = subprocess.run(
            [sys.executable, '-B', str(ROOT / 'managed_examples.py'), mode], cwd=ROOT)
        if completed.returncode != 0:
            print(f'managed_examples.py {mode} failed with {completed.returncode}')
            return completed.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
