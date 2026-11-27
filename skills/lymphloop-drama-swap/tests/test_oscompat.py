"""scripts/oscompat.py: the OS differences behind one interface (file locks, live pids, tool names, fonts).

Runs on macOS, Windows and Linux; the Windows half (LockFileEx, OpenProcess) is only exercised there."""
import os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import oscompat  # noqa: E402

HOLDER = ("import sys, time; sys.path.insert(0, sys.argv[1]); import oscompat\n"
          "f = open(sys.argv[2], 'a+b'); oscompat.lock_file(f, exclusive=(sys.argv[3] == 'x')); print('held', flush=True); time.sleep(120)\n")


def hold(path, mode='x'):
    """Another process that holds the lock until it is ended."""
    p = subprocess.Popen([sys.executable, '-c', HOLDER, str(SCRIPTS), str(path), mode], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == 'held'
    return p


class Locks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name)/'x.lock'

    def test_exclusive_lock_refuses_a_second_holder_until_released(self):
        with open(self.path, 'a+b') as a, open(self.path, 'a+b') as b:
            oscompat.lock_file(a, blocking=False)
            with self.assertRaises(OSError): oscompat.lock_file(b, blocking=False)
            oscompat.unlock_file(a)
            oscompat.lock_file(b, blocking=False); oscompat.unlock_file(b)

    def test_shared_holders_coexist_and_refuse_a_writer(self):
        with open(self.path, 'a+b') as a, open(self.path, 'a+b') as b, open(self.path, 'a+b') as c:
            oscompat.lock_file(a, exclusive=False, blocking=False)
            oscompat.lock_file(b, exclusive=False, blocking=False)
            with self.assertRaises(OSError): oscompat.lock_file(c, blocking=False)
            oscompat.unlock_file(a); oscompat.unlock_file(b)
            oscompat.lock_file(c, blocking=False); oscompat.unlock_file(c)

    def test_the_lock_file_stays_readable_and_writable_while_held(self):
        # the holder's note (pid, command) is written into the lock file and read by the process that was refused
        with open(self.path, 'a+b') as a:
            oscompat.lock_file(a, blocking=False)
            a.truncate(0); a.write(b'{"pid": 1}'); a.flush()
            self.assertEqual(self.path.read_bytes(), b'{"pid": 1}')
            oscompat.unlock_file(a)

    def test_locked_waits_then_gives_up(self):
        with oscompat.locked(self.path):
            t = time.monotonic()
            with self.assertRaises(TimeoutError):
                with oscompat.locked(self.path, wait=0.6, poll=0.1): pass
            self.assertGreaterEqual(time.monotonic() - t, 0.5)
        with oscompat.locked(self.path, wait=0): pass

    def test_another_process_holds_the_lock_and_its_death_frees_it(self):
        p = hold(self.path)
        try:
            with open(self.path, 'a+b') as f:
                with self.assertRaises(OSError): oscompat.lock_file(f, blocking=False)
                p.kill(); p.wait(timeout=20)
                deadline = time.monotonic() + 10
                while True:
                    try: oscompat.lock_file(f, blocking=False); break
                    except OSError:
                        if time.monotonic() > deadline: raise
                        time.sleep(0.1)
                oscompat.unlock_file(f)
        finally:
            if p.poll() is None: p.kill()
            p.stdout.close()

    def test_a_blocking_lock_is_granted_when_the_holder_leaves(self):
        p = hold(self.path)
        try:
            with open(self.path, 'a+b') as f:
                import threading
                threading.Timer(0.8, p.kill).start()
                t = time.monotonic(); oscompat.lock_file(f)            # blocks until the holder is gone
                self.assertGreaterEqual(time.monotonic() - t, 0.4)
                oscompat.unlock_file(f)
        finally:
            if p.poll() is None: p.kill()
            p.wait(timeout=20); p.stdout.close()


class Files(unittest.TestCase):
    def test_replace_wins_while_readers_and_writers_hold_the_target(self):
        import json, threading
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/'state.json'; target.write_text('{"n": -1}'); errors = []; stop = threading.Event()
            def write(n):
                try:
                    for i in range(40):
                        tmp = Path(d)/f'.{n}.{i}.tmp'; tmp.write_text(json.dumps({'n': n, 'i': i})); oscompat.replace(tmp, target)
                except Exception as e: errors.append(e)
            def read():
                while not stop.is_set():
                    try: json.loads(target.read_text())
                    except PermissionError: pass          # Windows: the file is being swapped right now
                    except Exception as e: errors.append(e); return
            readers = [threading.Thread(target=read) for _ in range(3)]; writers = [threading.Thread(target=write, args=(n,)) for n in range(6)]
            [t.start() for t in readers + writers]; [t.join() for t in writers]; stop.set(); [t.join() for t in readers]
            self.assertEqual(errors, [])
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ['state.json'])

    def test_change_time_moves_when_a_file_is_rewritten_with_its_old_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'movie.mp4'; f.write_bytes(b'old'); s = f.stat(); before = oscompat.change_ns(f, s)
            self.assertEqual(oscompat.change_ns(f), before)
            time.sleep(0.05); f.write_bytes(b'new'); os.utime(f, ns=(s.st_atime_ns, s.st_mtime_ns))
            self.assertEqual(f.stat().st_mtime_ns, s.st_mtime_ns)
            self.assertNotEqual(oscompat.change_ns(f), before)


class MediaTypes(unittest.TestCase):
    def test_media_types_do_not_depend_on_the_registry(self):
        for name, mime in (('a.mp4', 'video/mp4'), ('B.WEBP', 'image/webp'), ('c.jpeg', 'image/jpeg'), ('d.png', 'image/png'), ('e.wav', 'audio/wav')):
            self.assertEqual(oscompat.mime_type(name), mime)
        self.assertEqual(oscompat.mime_type(Path('x')/'clip.mp4'), 'video/mp4'); self.assertIsNone(oscompat.mime_type('noext'))
        self.assertEqual(oscompat.mime_extension('image/jpeg'), '.jpg'); self.assertEqual(oscompat.mime_extension('image/png'), '.png')


class Processes(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(oscompat.pid_alive(os.getpid()))
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            self.assertTrue(oscompat.pid_alive(p.pid))        # asking must not end it (os.kill(pid, 0) does on Windows)
            time.sleep(0.3); self.assertIsNone(p.poll())
        finally:
            p.kill(); p.wait(timeout=20)
        self.assertFalse(oscompat.pid_alive(p.pid))
        for bad in (None, '', 'x', 0, -1): self.assertFalse(oscompat.pid_alive(bad))

    def test_detached_child_starts(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'ran.txt'
            p = subprocess.Popen([sys.executable, '-c', f'open(r"{out}", "w").write("ok")'], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **oscompat.detached())
            p.wait(timeout=30)
            self.assertEqual(out.read_text(), 'ok')


class Names(unittest.TestCase):
    def test_tool_and_interpreter_names(self):
        if oscompat.WINDOWS:
            self.assertEqual(oscompat.exe('ffmpeg'), 'ffmpeg.exe'); self.assertEqual(oscompat.exe('ffmpeg.exe'), 'ffmpeg.exe')
            self.assertEqual(oscompat.venv_python(Path('s')), Path('s')/'.venv'/'Scripts'/'python.exe')
        else:
            self.assertEqual(oscompat.exe('ffmpeg'), 'ffmpeg')
            self.assertEqual(oscompat.venv_python(Path('s')), Path('s')/'.venv'/'bin'/'python')

    def test_fonts(self):
        for d in oscompat.font_dirs(): self.assertTrue(d.is_dir())
        self.assertIsNone(oscompat.find_font('no-such-font-file.ttf'))
        face = oscompat.find_font(*oscompat.SANS_REGULAR)
        if oscompat.WINDOWS or oscompat.MACOS: self.assertTrue(face and Path(face).is_file(), 'no Arial-like sans found')
        if face:
            from PIL import ImageFont
            ImageFont.truetype(face, 20)

    def test_a_script_started_without_utf8_mode_on_windows_restarts_in_it(self):
        import json
        oscompat.ensure_utf8()      # under a test runner: never restarts
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONUTF8'}
        r = subprocess.run([sys.executable, str(SCRIPTS/'oscompat.py')], env=env, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        seen = json.loads(r.stdout.decode('utf-8'))
        self.assertEqual(seen['platform'], sys.platform)
        if oscompat.WINDOWS:
            # every script reads and writes UTF-8 with the default encoding (Chinese notes, prompts, JSON)
            self.assertEqual(seen['utf8_mode'], 1)
            self.assertEqual(seen['default_encoding'].lower().replace('-', ''), 'utf8')


if __name__ == '__main__':
    unittest.main()
