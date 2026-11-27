"""Shared host (scripts/hostd.py): Range/HEAD serving, cheap URL verification, health assessment, tunnel restart.

2026-10-25: the stock SimpleHTTPRequestHandler ignored Range, so every 64-byte check downloaded the whole MP4 through
the tunnel and five parallel batches saturated it. These tests pin the fix offline (no tunnel, no network)."""
import functools, http.server, json, os, signal, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import hostd  # noqa: E402


def _get(url, headers=None, method='GET'):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}, method=method), timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class _Root(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); root = Path(self.td.name)
        self.patches = [mock.patch.object(hostd, k, v) for k, v in
                        dict(ROOT=root, WWW=root/'www', STATE=root/'state.json', LOCK=root/'start.lock', LOG=root/'hostd.log').items()]
        for p in self.patches: p.start()
        self.root = root

    def tearDown(self):
        for p in self.patches: p.stop()
        self.td.cleanup()


class RangeServer(_Root):
    def setUp(self):
        super().setUp()
        self.data = os.urandom(200_000); d = self.root/'www'/'abc'; d.mkdir(parents=True)
        (d/'clip.mp4').write_bytes(self.data)
        self.srv = hostd._start_http(); self.base = f'http://127.0.0.1:{self.srv.server_address[1]}'
        self.url = self.base + '/abc/clip.mp4'

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def test_first_bytes_range_returns_only_those_bytes(self):
        code, h, body = _get(self.url, {'Range': 'bytes=0-63'})
        self.assertEqual((code, body), (206, self.data[:64]))
        self.assertEqual(h['Content-Range'], f'bytes 0-63/{len(self.data)}')
        self.assertEqual(h['Content-Length'], '64'); self.assertEqual(h['Content-Type'], 'video/mp4')

    def test_open_ended_suffix_and_unsatisfiable_ranges(self):
        code, h, body = _get(self.url, {'Range': 'bytes=199990-'})
        self.assertEqual((code, body), (206, self.data[199990:]))
        code, h, body = _get(self.url, {'Range': 'bytes=-10'})
        self.assertEqual((code, body), (206, self.data[-10:]))
        code, h, body = _get(self.url, {'Range': 'bytes=0-999999'})          # end past the file is clamped
        self.assertEqual((code, len(body)), (206, len(self.data)))
        code, h, _ = _get(self.url, {'Range': 'bytes=300000-'})
        self.assertEqual(code, 416); self.assertEqual(h['Content-Range'], f'bytes */{len(self.data)}')

    def test_head_and_plain_get(self):
        code, h, body = _get(self.url, method='HEAD')
        self.assertEqual((code, body, h['Content-Length'], h['Accept-Ranges']), (200, b'', str(len(self.data)), 'bytes'))
        code, h, body = _get(self.url)
        self.assertEqual((code, body), (200, self.data))
        self.assertEqual(_get(self.base + '/abc/missing.mp4', {'Range': 'bytes=0-63'})[0], 404)

    def test_verify_matches_content_and_size(self):
        src = self.root/'local.mp4'; src.write_bytes(self.data)
        self.assertTrue(hostd._verify(self.url, src))
        src.write_bytes(self.data + b'x')                                     # same head, different size
        self.assertFalse(hostd._verify(self.url, src))
        src.write_bytes(b'y' + self.data[1:])                                 # same size, different head
        self.assertFalse(hostd._verify(self.url, src))

    def test_publish_through_public_base_never_starts_the_tunnel(self):
        src = self.root/'G01.hq.mp4'; src.write_bytes(self.data)
        with mock.patch.dict(os.environ, {'LYMPHLOOP_PUBLIC_BASE': self.base + '/'}), mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(hostd, 'ensure', side_effect=AssertionError('tunnel started')):
            os.environ.pop('LYMPHLOOP_HOST_MODE', None)
            url = hostd.publish(src)
        self.assertEqual(url, f'{self.base}/{hostd._digest(src)}/G01.hq.mp4')
        self.assertEqual(_get(url)[2], self.data)

    def test_probe_requests_are_not_counted_as_transfers(self):
        (self.root/'www'/'_probe').mkdir(); (self.root/'www'/'_probe'/'t.bin').write_bytes(b'z' * 1000)
        seen = []
        orig = hostd._Handler.copyfile
        def watch(h, s, d):
            class W:
                def write(_, b): seen.append((h.path, hostd._ACTIVE[0])); d.write(b)
            return orig(h, s, W())
        with mock.patch.object(hostd._Handler, 'copyfile', watch):
            _get(self.base + '/_probe/t.bin', {'Range': 'bytes=0-99'})
            _get(self.url, {'Range': 'bytes=0-99'})
        self.assertEqual({c for p, c in seen if p.startswith('/_probe/')}, {0})
        self.assertEqual({c for p, c in seen if p == '/abc/clip.mp4'}, {1})
        self.assertEqual(hostd._ACTIVE[0], 0)


class IgnoresRange(_Root):
    def test_fetch_head_reads_only_n_bytes_from_a_server_without_range(self):
        data = os.urandom(3_000_000); d = self.root/'plain'; d.mkdir(); (d/'big.mp4').write_bytes(data)
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(type('Q', (http.server.SimpleHTTPRequestHandler,), {'log_message': lambda *a: None}), directory=str(d)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            got, total = hostd._fetch_head(f'http://127.0.0.1:{srv.server_address[1]}/big.mp4', 64)
            self.assertEqual((got, total), (data[:64], len(data)))
            src = self.root/'big.mp4'; src.write_bytes(data)
            self.assertTrue(hostd._verify(f'http://127.0.0.1:{srv.server_address[1]}/big.mp4', src))
        finally:
            srv.shutdown(); srv.server_close()


class Assess(unittest.TestCase):
    def test_dead_tunnel_restarts_after_two_checks(self):
        d, s, r = hostd._assess(False, 0, 0, 0, 0); self.assertEqual((d, s, r), (1, 0, False))
        d, s, r = hostd._assess(False, 0, 0, d, s); self.assertTrue(r)

    def test_slow_counts_only_when_idle(self):
        d = s = 0
        for _ in range(10): d, s, r = hostd._assess(True, 5.0, 3, d, s)     # APIMART downloading: load, not breakage
        self.assertEqual((s, r), (0, False))
        for i in range(hostd.SLOW_CHECKS): d, s, r = hostd._assess(True, 5.0, 0, d, s)
        self.assertTrue(r)
        d, s, r = hostd._assess(True, 900.0, 0, 0, 3); self.assertEqual((d, s, r), (0, 0, False))   # one good check resets
        d, s, r = hostd._assess(True, None, 0, 1, 2); self.assertEqual((d, s, r), (0, 2, False))    # no speed probe: slow count kept


class RestartTunnel(_Root):
    @unittest.skipUnless(hasattr(signal, 'SIGUSR1'), 'no user signals on Windows: the daemon is stopped and started again')
    def test_running_daemon_gets_sigusr1_and_reports_the_new_url(self):
        hostd._write_state(pid=os.getpid(), url='https://old.trycloudflare.com', healthy_at=time.time(), restartable=True)
        prev = signal.signal(signal.SIGUSR1, lambda *_: hostd._write_state(url='https://new.trycloudflare.com', healthy_at=time.time()))
        try: s = hostd.restart_tunnel(timeout=10)
        finally: signal.signal(signal.SIGUSR1, prev)
        self.assertEqual(s['url'], 'https://new.trycloudflare.com'); self.assertTrue(s['alive'])

    def test_daemon_without_restart_support_is_stopped_and_started_again(self):
        # detached like the real daemon (not our child, so it leaves no zombie that still answers kill 0)
        if os.name == 'nt':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, **hostd.oscompat.detached())
            pid = child.pid
        else:
            pid = int(subprocess.run(['sh', '-c', 'sleep 60 >/dev/null 2>&1 & echo $!'], capture_output=True, text=True).stdout)
        try:
            hostd._write_state(pid=pid, url='https://old.trycloudflare.com', healthy_at=time.time())
            def fake_ensure(): hostd._write_state(pid=os.getpid(), url='https://new.trycloudflare.com', healthy_at=time.time(), restartable=True); return 'x'
            with mock.patch.object(hostd, 'ensure', side_effect=fake_ensure):
                s = hostd.restart_tunnel(timeout=20)
            self.assertFalse(hostd._alive(pid))
            self.assertEqual(s['url'], 'https://new.trycloudflare.com')
        finally:
            if hostd._alive(pid): hostd._terminate(pid) if os.name == 'nt' else os.kill(pid, signal.SIGKILL)


class PublicModes(_Root):
    def test_public_modes_are_opt_in_and_logged(self):
        src = self.root/'c.mp4'; src.write_bytes(b'0' * 100)
        with mock.patch.dict(os.environ, {'LYMPHLOOP_HOST_MODE': 'litterbox'}), mock.patch.object(hostd, 'publish_litterbox', return_value='https://litter.catbox.moe/x.mp4') as lb:
            self.assertEqual(hostd.publish(src), 'https://litter.catbox.moe/x.mp4'); lb.assert_called_once()
        self.assertIn('third-party public host', (self.root/'hostd.log').read_text())
        env = {k: v for k, v in os.environ.items() if k not in ('LYMPHLOOP_HOST_MODE', 'LYMPHLOOP_PUBLIC_BASE')}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(hostd, 'publish_litterbox', side_effect=AssertionError), \
             mock.patch.object(hostd, 'publish_tempfile', side_effect=AssertionError), mock.patch.object(hostd, 'ensure', return_value='https://t.trycloudflare.com'):
            self.assertEqual(hostd.publish(src, verify=False), f'https://t.trycloudflare.com/{hostd._digest(src)}/c.mp4')


if __name__ == '__main__':
    unittest.main()
