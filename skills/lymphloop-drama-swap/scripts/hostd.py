#!/usr/bin/env python3
"""One shared public host for reference clips (user request, 2026-10-24: 5 videos and all their segments in parallel).

APIMART accepts only image uploads (video/mp4 is refused, tested 2026-10-24), so reference videos must sit at a public
URL. ONE daemon per skill copy serves ~/.cache/lymphloop-host-claude/www (Codex copy: ~/.cache/lymphloop-host/www)
through ONE Cloudflare quick tunnel for every run and every job:
  - files are content-addressed (www/<sha16>/<name>), so the same clip is published once and reused;
  - the file server answers Range and HEAD requests (2026-10-25: the stock SimpleHTTPRequestHandler ignored Range, so
    every "read the first 64 bytes" check pulled the whole MP4 through the tunnel; with 5 batches in parallel that
    saturated it, ~100-260 KB/s and http2 "stream closed", and 23%+ of tasks failed to fetch their references);
  - publishing verifies a URL with one 64-byte range read, never a full download;
  - every 30 s the daemon checks its own public URL is reachable, and every 2 min (every 30 s once it saw slowness)
    measures its speed with a 256 KB range read; the uplink here is only ~200-500 KB/s, so the probe stays small. Two
    dead checks, or four slow speed checks in a row while nothing else is downloading, restart the tunnel (the URL
    changes; tasks submitted with a dead URL fail to fetch and are resubmitted by apimart_wan.run_batch);
  - `restart-tunnel` asks the running daemon for a new tunnel without stopping it;
  - files are kept 48 h, then pruned;
  - clients start the daemon on demand under a file lock, so parallel runs never start two.
  - a machine-wide FETCH GATE (2026-10-25) paces task submissions for every session and both skill copies: before a
    task is submitted, each reference it will fetch takes a slot; the slot is freed when this host's access log
    shows the file fully sent, or after FETCH_TIMEOUT. Measured on this uplink: one download ~0.3 MB/s, total
    ~1.0-1.2 MB/s from 8-12 parallel downloads; APIMART gives up on a reference after 60 s, so without a gate five
    sessions submitting at once (20+ fetches) pushed single downloads past 60 s.

Opt-in fallbacks (off unless set; files then sit on a third-party public host, so use only when the user agrees):
  LYMPHLOOP_PUBLIC_BASE=https://...   serve www/ through your own public base URL (must answer Range or HEAD)
  LYMPHLOOP_HOST_MODE=litterbox       upload each clip to litterbox.catbox.moe (72 h)
  LYMPHLOOP_HOST_MODE=tempfile        upload each clip to tempfile.org (24 h)

  hostd.py serve           # run the daemon in the foreground (clients start it detached on their own)
  hostd.py status          # url, pid, last healthy check, measured speed
  hostd.py publish FILE    # print the public URL of FILE
  hostd.py restart-tunnel  # new tunnel, same daemon (use when status shows slow/unhealthy)
  hostd.py gate            # fetch-gate slots in use, machine-wide
  hostd.py stop
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, email.utils, functools, hashlib, http.server, json, mimetypes, os, re, secrets, shutil, signal, socket, subprocess, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def _under_claude(skill=None):
    """True when this copy of the skill lives under ~/.claude (checked on the resolved and the as-invoked path)."""
    claude = Path.home()/'.claude'
    paths = [Path(skill)] if skill else [SKILL, Path(__file__).absolute().parents[1]]
    return any(c == p or c in p.parents for c in (claude, claude.resolve()) for p in paths)


def default_root(skill=None):
    """Each copy runs its own daemon: ~/.cache/lymphloop-host-claude for the copy under ~/.claude, ~/.cache/lymphloop-host
    for the Codex copy; LYMPHLOOP_HOST_ROOT overrides both."""
    if os.environ.get('LYMPHLOOP_HOST_ROOT'): return Path(os.environ['LYMPHLOOP_HOST_ROOT'])
    return Path.home()/'.cache'/('lymphloop-host-claude' if _under_claude(skill) else 'lymphloop-host')


def cloudflared_path():
    """CLOUDFLARED_BIN -> this skill's bin/cloudflared -> the Codex wan-media install -> PATH."""
    for c in (os.environ.get('CLOUDFLARED_BIN'), SKILL/'bin'/oscompat.exe('cloudflared'), Path.home()/'.codex/tools/wan-media/2026.10.0'/oscompat.exe('cloudflared'), shutil.which('cloudflared')):
        if c and Path(c).is_file(): return Path(c)
    return SKILL/'bin'/oscompat.exe('cloudflared')


ROOT = default_root()
WWW = ROOT/'www'; STATE = ROOT/'state.json'; LOCK = ROOT/'start.lock'; LOG = ROOT/'hostd.log'
CLOUDFLARED = cloudflared_path()
CHECK_EVERY = 30; KEEP_HOURS = 48; HEALTHY_WITHIN = 120
PROBE_BYTES = 256 * 1024; SPEED_EVERY = 4                          # speed probe every 4th check (2 min), every check while slow
MIN_KBPS = float(os.environ.get('LYMPHLOOP_HOST_MIN_KBPS', 24))   # idle tunnel measured 75-560 KB/s (2026-10-25); below 24 it is broken
DEAD_CHECKS = 2; SLOW_CHECKS = 4
GATE_DIR = Path(os.environ.get('LYMPHLOOP_GATE_DIR') or Path.home()/'.cache'/'lymphloop-fetch-gate')   # shared by both copies
FETCH_SLOTS = int(os.environ.get('LYMPHLOOP_FETCH_SLOTS', 8))      # references being fetched at once, machine-wide
FETCH_TIMEOUT = 120                                                # a slot nobody fetched within this is freed


class Error(RuntimeError): pass


def _log(msg):
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f: f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')


def _read_state():
    try: return json.loads(STATE.read_text())
    except Exception: return {}


def _write_state(**kw):
    s = {**_read_state(), **kw}; tmp = STATE.with_name(f'.state.{os.getpid()}.{secrets.token_hex(4)}.tmp'); tmp.write_text(json.dumps(s, indent=1)); tmp.replace(STATE)


def _alive(pid):
    return oscompat.pid_alive(pid)       # os.kill(pid, 0) would END the process on Windows


def _terminate(pid):
    """Stop a daemon. Windows has no SIGTERM handler to run: end the daemon together with its tunnel process and
    clear the state it would have cleared itself."""
    if oscompat.WINDOWS:
        subprocess.run(['taskkill', '/PID', str(int(pid)), '/T', '/F'], capture_output=True)
        _write_state(pid=None, url=None, stopped_at=time.time())
    else: os.kill(int(pid), signal.SIGTERM)


def _digest(path, n=16):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()[:n]


def _fetch_head(url, n=64, timeout=20):
    """First n bytes of url and the total size the server reports (None if it did not say). Reads at most n bytes even
    from a server that ignores Range."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={'Range': f'bytes=0-{n - 1}'}), timeout=timeout) as r:
        cr = r.headers.get('Content-Range') or ''
        if r.status == 206: total = int(cr.rsplit('/', 1)[1]) if re.search(r'/\d+$', cr) else None
        else: total = int(r.headers['Content-Length']) if r.headers.get('Content-Length') else None
        return r.read(n), total


def _head_exists(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=timeout) as r:
        return r.status == 200 and int(r.headers.get('Content-Length', '0')) > 0


def _verify(url, src, timeout=20):
    """True when url serves src: same first 64 bytes and same size, via one range read (HEAD as a fallback)."""
    with open(src, 'rb') as f: want = f.read(64)
    size = os.path.getsize(src)
    try:
        got, total = _fetch_head(url, 64, timeout)
        return got == want and total in (None, size)
    except urllib.error.HTTPError as e:
        if e.code in (405, 416, 501): return _head_exists(url, timeout)
        raise


# ---------------------------------------------------------------- daemon
_ACTIVE = [0]; _ACTIVE_LOCK = threading.Lock()


def _access(line):
    """One line per served file (not health probes): who fetched what, how much, how long, and whether they hung up."""
    try:
        with open(ROOT/'access.log', 'a') as f: f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + line + '\n')
    except OSError: pass


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Static files with single-range support (206/416) and Accept-Ranges; counts transfers that are not health probes."""
    timeout = 120                                     # drop connections that stall, so the transfer count stays honest

    def log_message(self, *a): pass

    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes'); super().end_headers()

    def send_head(self):
        self._range = None
        m = re.fullmatch(r'\s*bytes=(\d*)-(\d*)\s*', self.headers.get('Range') or '')
        path = self.translate_path(self.path)
        if not m or not (m[1] or m[2]) or os.path.isdir(path): return super().send_head()
        try: f = open(path, 'rb')
        except OSError: self.send_error(404, 'File not found'); return None
        size = os.fstat(f.fileno()).st_size
        if m[1]: start, end = int(m[1]), min(int(m[2]) if m[2] else size - 1, size - 1)
        else: start, end = max(0, size - int(m[2])), size - 1
        if start >= size or start > end:
            f.close(); self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.send_header('Content-Length', '0'); self.end_headers(); return None
        self.send_response(206)
        self.send_header('Content-Type', mimetypes.guess_type(path)[0] or 'application/octet-stream')
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}'); self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Last-Modified', email.utils.formatdate(os.fstat(f.fileno()).st_mtime, usegmt=True))
        self.end_headers(); f.seek(start); self._range = end - start + 1
        return f

    def copyfile(self, src, dst):
        probe = self.path.startswith('/_probe/')
        if not probe:
            with _ACTIVE_LOCK: _ACTIVE[0] += 1
        t0, start, sent, err = time.monotonic(), src.tell(), 0, ''
        try:
            n = getattr(self, '_range', None)
            if n is None: n = float('inf')
            while n > 0:
                buf = src.read(int(min(1 << 16, n)))
                if not buf: break
                dst.write(buf); n -= len(buf); sent += len(buf)
        except OSError as e:
            err = type(e).__name__                      # the fetcher hung up mid-transfer
        finally:
            if not probe:
                with _ACTIVE_LOCK: _ACTIVE[0] -= 1
                _access(f'{self.command} {self.path} range={self.headers.get("Range") or "-"} from={start} sent={sent} '
                        f'secs={time.monotonic() - t0:.1f} ua={(self.headers.get("User-Agent") or "-")[:60]} {err}')


def _start_http(directory=None):
    d = Path(directory or WWW); d.mkdir(parents=True, exist_ok=True)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(_Handler, directory=str(d)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _start_tunnel(port):
    """cloudflared quick tunnel; on this network its own edge discovery fails, so edge IPs are resolved here."""
    if not CLOUDFLARED.is_file(): raise Error('cloudflared missing at ' + str(CLOUDFLARED))
    env = {k: v for k, v in os.environ.items() if k.lower() not in ('https_proxy', 'http_proxy', 'all_proxy')}
    env['GODEBUG'] = 'netdns=cgo'
    edges = []
    for host in ('region1.v2.argotunnel.com', 'region2.v2.argotunnel.com'):
        try: edges += ['--edge', socket.getaddrinfo(host, 7844, socket.AF_INET, socket.SOCK_STREAM)[0][4][0] + ':7844']
        except OSError: pass
    tlog = ROOT/'tunnel.log'; out = open(tlog, 'wb')
    proc = subprocess.Popen([str(CLOUDFLARED), 'tunnel', '--url', f'http://127.0.0.1:{port}', '--protocol', 'http2', '--edge-ip-version', '4',
                             *edges, '--no-autoupdate'], stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        text = tlog.read_text(errors='replace')
        m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', text)
        if m and 'Registered tunnel connection' in text: return proc, m[0]
        if proc.poll() is not None: break
        time.sleep(1)
    proc.kill(); raise Error('quick tunnel did not register; see ' + str(tlog))


def _probe(url, token):
    try: return _fetch_head(f'{url}/_probe/{token}.txt', 16, 15)[0] == token.encode()[:16]
    except Exception: return False


def _speed(url, token, timeout=20):
    """KB/s of a PROBE_BYTES range read of the probe blob through the tunnel; 0 when it fails or comes back short."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(f'{url}/_probe/{token}.bin', headers={'Range': f'bytes=0-{PROBE_BYTES - 1}'})
        with urllib.request.urlopen(req, timeout=timeout) as r: n = len(r.read(PROBE_BYTES))
    except Exception: return 0.0
    return round(n / 1024 / max(time.monotonic() - t0, 1e-3), 1) if n == PROBE_BYTES else 0.0


def _wait_reachable(url, token, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _probe(url, token): return True
        time.sleep(2)
    return False


def _prune():
    cutoff = time.time() - KEEP_HOURS * 3600
    for d in WWW.iterdir() if WWW.is_dir() else []:
        if d.name != '_probe' and d.is_dir() and d.stat().st_mtime < cutoff: shutil.rmtree(d, ignore_errors=True)


def _assess(reachable, kbps, active, dead, slow):
    """Next (dead, slow) counters and whether to restart the tunnel. kbps None = no speed probe this round. Slowness
    counts only while no other transfer is running: APIMART pulling many clips at once is load, not a broken tunnel."""
    if not reachable: dead, slow = dead + 1, 0
    elif kbps is None: dead = 0
    elif kbps < MIN_KBPS and active == 0: dead, slow = 0, slow + 1
    else: dead, slow = 0, 0
    return dead, slow, dead >= DEAD_CHECKS or slow >= SLOW_CHECKS


def serve():
    ROOT.mkdir(parents=True, exist_ok=True); WWW.mkdir(parents=True, exist_ok=True)
    srv = _start_http(); port = srv.server_address[1]
    token = secrets.token_hex(12); probe_dir = WWW/'_probe'; probe_dir.mkdir(exist_ok=True)
    for old in probe_dir.iterdir(): old.unlink(missing_ok=True)
    (probe_dir/f'{token}.txt').write_text(token); (probe_dir/f'{token}.bin').write_bytes(os.urandom(PROBE_BYTES))
    proc = None; url = None; dead = slow = n = 0; wake = threading.Event(); want_new = [False]

    def stop(*_):
        if proc and proc.poll() is None: proc.kill()
        _write_state(pid=None, url=None, stopped_at=time.time()); sys.exit(0)

    def recycle(*_): want_new[0] = True; wake.set()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    if hasattr(signal, 'SIGUSR1'): signal.signal(signal.SIGUSR1, recycle)     # no user signals on Windows: restart_tunnel stops and starts the daemon there
    _write_state(pid=os.getpid(), restartable=hasattr(signal, 'SIGUSR1'))
    while True:
        if proc is None or proc.poll() is not None or want_new[0]:
            reason = 'requested' if want_new[0] else 'started' if proc is None else 'tunnel exited'
            want_new[0] = False
            if proc and proc.poll() is None: proc.kill()
            proc = None; _write_state(url=None, healthy_at=0)
            for attempt in range(5):
                try:
                    proc, url = _start_tunnel(port)
                    if _wait_reachable(url, token): break
                    _log(f'tunnel {url} registered but not reachable; retrying'); proc.kill(); proc = None
                except Error as e:
                    _log(str(e)); proc = None
                time.sleep(5 * (attempt + 1))
            if proc is None: _write_state(pid=os.getpid(), url=None, error='no working tunnel'); wake.wait(CHECK_EVERY); wake.clear(); continue
            dead = slow = 0; kbps = _speed(url, token)
            _log(f'serving {url} ({reason}; {kbps} KB/s)')
            _write_state(pid=os.getpid(), port=port, url=url, started_at=time.time(), healthy_at=time.time(), speed_kbps=kbps, slow_checks=0, error=None, restartable=True)
        wake.wait(CHECK_EVERY); wake.clear()
        if want_new[0]: _log('tunnel restart requested'); continue
        n += 1; reachable = _probe(url, token)
        kbps = _speed(url, token) if reachable and (slow or n % SPEED_EVERY == 0) else None
        with _ACTIVE_LOCK: active = _ACTIVE[0]
        dead, slow, restart = _assess(reachable, kbps, active, dead, slow)
        speed = {} if kbps is None else {'speed_kbps': kbps, 'speed_at': time.time()}
        if reachable and not slow: _write_state(healthy_at=time.time(), active_transfers=active, slow_checks=0, **speed)
        else:
            _write_state(active_transfers=active, slow_checks=slow, **speed)
            _log(f'health check: reachable={reachable} {kbps} KB/s active={active} dead={dead} slow={slow}')
        if restart: _log('restarting tunnel (' + ('unreachable' if dead else f'slow < {MIN_KBPS} KB/s') + ')'); want_new[0] = True
        _prune()


# ---------------------------------------------------------------- clients
def ensure(timeout=300):
    """Base URL of a healthy daemon; starts one (detached) if none is running."""
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(LOCK, 'a') as lk:
        oscompat.lock_file(lk)
        s = _read_state()
        if not (s.get('pid') and _alive(s['pid'])):
            _write_state(pid=None, url=None, healthy_at=0, launched_at=time.time())
            log = open(LOG, 'a')
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve'], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             **oscompat.detached())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = _read_state()
            if s.get('url') and s.get('pid') and _alive(s['pid']) and time.time() - s.get('healthy_at', 0) < HEALTHY_WITHIN: return s['url']
            time.sleep(2)
    raise Error('shared host did not become healthy; see ' + str(LOG))


def publish(path, verify=True):
    """Public URL for a local file (content-addressed copy under the shared host)."""
    mode = os.environ.get('LYMPHLOOP_HOST_MODE')
    if mode in ('litterbox', 'tempfile'):
        _log(f'LYMPHLOOP_HOST_MODE={mode}: {Path(path).name} goes to a third-party public host')
        return publish_litterbox(path) if mode == 'litterbox' else publish_tempfile(path)
    src = Path(path).resolve(); digest = _digest(src); name = re.sub(r'[^A-Za-z0-9._-]', '_', src.name)
    dest = WWW/digest/name
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True); tmp = dest.with_name('.' + name + '.' + secrets.token_hex(4))
        shutil.copyfile(src, tmp); tmp.replace(dest)
    os.utime(dest.parent)   # keep it alive another 48 h
    custom_base = os.environ.get('LYMPHLOOP_PUBLIC_BASE')
    if custom_base:
        url = f'{custom_base.rstrip("/")}/{digest}/{name}'
        if not verify or _verify(url, src): return url
        raise Error('published file not reachable through LYMPHLOOP_PUBLIC_BASE: ' + name)
    for attempt in range(3):
        base = ensure(); url = f'{base}/{digest}/{name}'
        if not verify: return url
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if _verify(url, src): return url
            except Exception: pass
            time.sleep(2)
        _log(f'published {name} not reachable at {url} (attempt {attempt + 1})')
    raise Error('published file not reachable through the shared host: ' + name)


def publish_litterbox(path):
    """Opt-in third-party fallback (LYMPHLOOP_HOST_MODE=litterbox): litterbox.catbox.moe, 72 h links.
    Cached by file SHA so paid task retries never upload a different reference by accident."""
    src = Path(path).resolve(); digest = _digest(src, 64)
    root = ROOT/'litterbox'; root.mkdir(parents=True, exist_ok=True)
    cache = root/(digest + '.json'); lock = root/(digest + '.lock')
    with lock.open('a+') as f:
        oscompat.lock_file(f)
        if cache.exists():
            item = json.loads(cache.read_text())
            if time.time() - item.get('uploaded_at', 0) < 60 * 3600: return item['url']
        form = f'fileToUpload=@{src};filename={src.name};type=video/mp4'
        for attempt in range(3):
            p = subprocess.run(['curl', '--max-time', '300', '--silent', '--show-error', '--fail-with-body', '-F', 'reqtype=fileupload',
                                '-F', 'time=72h', '-F', form, 'https://litterbox.catbox.moe/resources/internals/api.php'], capture_output=True, text=True)
            url = p.stdout.strip()
            if p.returncode == 0 and url.startswith('https://litter.catbox.moe/'):
                try:
                    if _verify(url, src, 45):
                        cache.write_text(json.dumps({'url': url, 'uploaded_at': time.time(), 'source': str(src), 'size': src.stat().st_size}))
                        return url
                except Exception: pass
            time.sleep(2 * (attempt + 1))
    raise Error('Litterbox upload or public range verification failed for ' + src.name)


def publish_tempfile(path):
    """Opt-in third-party fallback (LYMPHLOOP_HOST_MODE=tempfile): tempfile.org direct download, 24 h links.
    Cached by file SHA; the URL is checked to serve video/mp4 before it is returned."""
    src = Path(path).resolve(); digest = _digest(src, 64)
    root = ROOT/'tempfile'; root.mkdir(parents=True, exist_ok=True)
    cache = root/(digest + '.json'); lock = root/(digest + '.lock')
    with lock.open('a+') as f:
        oscompat.lock_file(f)
        if cache.exists():
            item = json.loads(cache.read_text())
            if time.time() - item.get('uploaded_at', 0) < 20 * 3600: return item['url']
        for attempt in range(3):
            p = subprocess.run(['curl', '--max-time', '300', '--silent', '--show-error', '--fail-with-body', '-F', f'files=@{src}',
                                '-F', 'expiryHours=24', 'https://tempfile.org/api/upload/local'], capture_output=True, text=True)
            try: result = json.loads(p.stdout) if p.returncode == 0 else None
            except ValueError: result = None
            item = result and result.get('success') and (result.get('file') or (result.get('files') or [None])[0])
            if item and item.get('size') == src.stat().st_size and item.get('id'):
                url = f"https://tempfile.org/{item['id']}/download"
                check = subprocess.run(['curl', '--max-time', '45', '--silent', '--show-error', '--location', '--head', url], capture_output=True, text=True)
                if check.returncode == 0 and 'content-type: video/mp4' in check.stdout.lower():
                    cache.write_text(json.dumps({'url': url, 'uploaded_at': time.time(), 'source': str(src), 'size': src.stat().st_size, 'remote_id': item['id']}))
                    return url
            time.sleep(2 * (attempt + 1))
    raise Error('TempFile upload or verification failed for ' + src.name)


# ---------------------------------------------------------------- fetch gate
def _gate_lock():
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(GATE_DIR/'gate.lock', 'a+'); oscompat.lock_file(f); return f


def _gate_read():
    try: return json.loads((GATE_DIR/'gate.json').read_text())
    except Exception: return []


def _gate_write(slots):
    tmp = GATE_DIR/f'.gate.{os.getpid()}.{secrets.token_hex(4)}.tmp'; tmp.write_text(json.dumps(slots, indent=1)); tmp.replace(GATE_DIR/'gate.json')


_ACCESS_LINE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) GET (\S+) range=\S+ from=(\d+) sent=(\d+)')


def _fetched(slot, now):
    """True when the access log of the host that serves this slot's file shows the whole file sent after the slot was
    taken (APIMART's own probe read and its full download are separate requests; only a complete one counts)."""
    log = Path(slot['log'])
    try:
        with open(log, 'rb') as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - (1 << 20))); tail = f.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return False
    for line in reversed(tail):
        m = _ACCESS_LINE.match(line)
        if not m or m[2] != slot['path']: continue
        at = time.mktime(time.strptime(m[1], '%Y-%m-%d %H:%M:%S'))
        if at + 1 < slot['at']: break
        if int(m[3]) + int(m[4]) >= slot['size']: return True
    return False


def _gate_live(slots, now):
    return [s for s in slots if now - s['at'] < FETCH_TIMEOUT and not _fetched(s, now)]


def own_url(url):
    """True for URLs served by one of this machine's quick-tunnel hosts (third-party or custom bases are not gated)."""
    return bool(re.match(r'https://[a-z0-9-]+\.trycloudflare\.com/', url or ''))


def reserve(refs, wait=3600, note=''):
    """Take one fetch slot per (url, local_path) about to be fetched by a task that is being submitted. Blocks while the
    machine already has FETCH_SLOTS references waiting to be fetched; a batch larger than the whole gate is let through
    alone once the gate is empty. Returns slot ids (release() them if the submission itself fails)."""
    want = [(u, Path(p)) for u, p in refs if own_url(u)]
    if not want: return []
    deadline = time.monotonic() + wait; said = False
    while True:
        with _gate_lock():
            now = time.time(); live = _gate_live(_gate_read(), now)
            if len(live) + len(want) <= FETCH_SLOTS or not live:
                new = [{'id': secrets.token_hex(6), 'path': '/' + '/'.join(u.rstrip('/').split('/')[-2:]), 'size': p.stat().st_size,
                        'log': str(ROOT/'access.log'), 'at': now, 'pid': os.getpid(), 'note': note[:80]} for u, p in want]
                _gate_write(live + new); return [s['id'] for s in new]
            _gate_write(live)
        if not said: print(f'fetch gate full ({len(live)}/{FETCH_SLOTS} references being fetched); waiting', file=sys.stderr, flush=True); said = True
        if time.monotonic() > deadline: raise Error('fetch gate stayed full for %d s' % wait)
        time.sleep(2)


def release(ids):
    if not ids: return
    with _gate_lock():
        _gate_write([s for s in _gate_read() if s['id'] not in set(ids)])


def gate_status():
    with _gate_lock():
        now = time.time(); live = _gate_live(_gate_read(), now); _gate_write(live)
    return {'slots': FETCH_SLOTS, 'in_use': len(live), 'timeout_s': FETCH_TIMEOUT,
            'waiting_for': [{'path': s['path'], 'age_s': round(now - s['at']), 'note': s.get('note', '')} for s in live]}


def status():
    s = _read_state(); s['alive'] = bool(s.get('pid') and _alive(s['pid']))
    if s.get('healthy_at'): s['healthy_seconds_ago'] = round(time.time() - s['healthy_at'])
    return s


def stop():
    s = _read_state()
    if s.get('pid') and _alive(s['pid']): _terminate(s['pid'])
    return status()


def restart_tunnel(timeout=300):
    """New tunnel (new URL) for the running daemon. A daemon from before 2026-10-25 has no SIGUSR1 handler, so it is
    stopped and started again instead. Tasks already fetching from the old URL fail and are resubmitted."""
    s = _read_state(); old = s.get('url')
    if s.get('pid') and _alive(s['pid']):
        if s.get('restartable') and hasattr(signal, 'SIGUSR1'): os.kill(int(s['pid']), signal.SIGUSR1)
        else:
            _terminate(s['pid']); deadline = time.monotonic() + 20
            while _alive(s['pid']) and time.monotonic() < deadline: time.sleep(0.5)
            if _alive(s['pid']): raise Error(f'old daemon {s["pid"]} did not stop')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = _read_state()
        if not (s.get('pid') and _alive(s['pid'])): ensure(); continue
        if s.get('url') and s['url'] != old and time.time() - s.get('healthy_at', 0) < HEALTHY_WITHIN: return status()
        time.sleep(2)
    raise Error('no new healthy tunnel; see ' + str(LOG))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sub = p.add_subparsers(dest='cmd', required=True)
    for c in ('serve', 'status', 'stop', 'restart-tunnel', 'gate'): sub.add_parser(c)
    q = sub.add_parser('publish'); q.add_argument('file')
    a = p.parse_args()
    if a.cmd == 'serve': serve()
    elif a.cmd == 'publish': print(publish(a.file))
    elif a.cmd == 'restart-tunnel': print(json.dumps(restart_tunnel(), indent=1))
    elif a.cmd == 'gate': print(json.dumps(gate_status(), indent=1))
    else: print(json.dumps(status() if a.cmd == 'status' else stop(), indent=1))


if __name__ == '__main__':
    main()
