"""Run the whole test suite and judge it against tests/known_failures.txt.

    python tests/run_ci.py            # exit 0 unless a test outside the known list fails

The known list holds tests that already failed before a change was made (one id per line, '#' comments; an id may be
followed by the systems it applies to, e.g. "test_x.Class.test_y  windows"). A listed test that passes is reported so
the list can be trimmed. The summary at the end is short on purpose: it is what gets read in a CI log."""
import sys, unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SYSTEM = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux')


def known():
    out = set(); f = TESTS/'known_failures.txt'
    for line in (f.read_text(encoding='utf-8').splitlines() if f.is_file() else []):
        parts = line.split('#')[0].split()
        if parts and (len(parts) == 1 or SYSTEM in parts[1:]): out.add(parts[0])
    return out


def main():
    sys.path.insert(0, str(TESTS))
    suite = unittest.defaultTestLoader.discover(str(TESTS))
    result = unittest.TextTestRunner(verbosity=1, buffer=True).run(suite)
    failed = {}
    for test, trace in result.failures + result.errors:
        failed.setdefault(test.id().split(' ')[0], trace.strip().splitlines()[-1][:300])
    expected = known()
    new = sorted(set(failed) - expected); fixed = sorted(expected - set(failed))
    print(f'\n==== {SYSTEM}: ran {result.testsRun}, skipped {len(result.skipped)}, failed {len(failed)} '
          f'({len(failed) - len(new)} known, {len(new)} new)')
    for name in new: print(f'NEW FAILURE  {name}\n             {failed[name]}')
    for name in sorted(set(failed) & expected): print(f'known        {name}')
    for name in fixed: print(f'now passing  {name}  (remove it from known_failures.txt)')
    reasons = {}
    for test, why in result.skipped: reasons[why] = reasons.get(why, 0) + 1
    for why, n in sorted(reasons.items()): print(f'skipped {n:3d}  {why}')
    return 1 if new else 0


if __name__ == '__main__':
    sys.exit(main())
