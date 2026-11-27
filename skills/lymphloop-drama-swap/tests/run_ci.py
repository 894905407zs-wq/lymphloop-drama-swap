"""Run the test suite and judge it against tests/known_failures.txt.

    python tests/run_ci.py                    # everything; exit 0 unless a test outside the known list fails
    python tests/run_ci.py NAME [NAME ...]    # only the tests whose id contains one of the names

The known list holds tests that already failed before a change was made (one id per line, '#' comments; an id may be
followed by the systems it applies to, e.g. "test_x.Class.test_y  windows"). A listed test that passes is reported so
the list can be trimmed. Each test is named as it starts and a failure is printed when it happens, so a log that was
cut short still says what went wrong; a run that stops moving dumps every thread's stack (LYMPHLOOP_CI_STALL seconds,
default 600)."""
import faulthandler, os, sys, time, unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SYSTEM = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux')
STALL = int(os.environ.get('LYMPHLOOP_CI_STALL', '600'))


def known():
    out = set(); f = TESTS/'known_failures.txt'
    for line in (f.read_text(encoding='utf-8').splitlines() if f.is_file() else []):
        parts = line.split('#')[0].split()
        if parts and (len(parts) == 1 or SYSTEM in parts[1:]): out.add(parts[0])
    return out


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite): yield from flatten(item)
        else: yield item


class Live(unittest.TextTestResult):
    """Prints the end of a failure's traceback at once and re-arms the stall dump for every test."""
    def startTest(self, test):
        faulthandler.dump_traceback_later(STALL, repeat=True, file=sys.stderr)
        self._t0 = time.monotonic(); super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        took = time.monotonic() - getattr(self, '_t0', time.monotonic())
        if took >= 20: self.stream.writeln(f'      ({took:.0f} s)')

    def _show(self, rows):
        for line in rows[-1][1].strip().splitlines()[-14:]: self.stream.writeln('      | ' + line[:400])
        self.stream.flush()

    def addError(self, test, err): super().addError(test, err); self._show(self.errors)
    def addFailure(self, test, err): super().addFailure(test, err); self._show(self.failures)


def main(names):
    sys.path.insert(0, str(TESTS))
    suite = unittest.defaultTestLoader.discover(str(TESTS))
    if names:
        suite = unittest.TestSuite(t for t in flatten(suite) if any(n in t.id() for n in names))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, buffer=True, resultclass=Live).run(suite)
    faulthandler.cancel_dump_traceback_later()
    failed = {}
    for test, trace in result.failures + result.errors:
        failed.setdefault(test.id().split(' ')[0], trace.strip().splitlines()[-1][:300])
    expected = known()
    new = sorted(set(failed) - expected); fixed = sorted(expected - set(failed)) if not names else []
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
    sys.exit(main(sys.argv[1:]))
