#!/usr/bin/env python3
"""Submit one APIMART Seedance 2.0 (`seedance-2.0`, 720p, 9:16) reference-video generation per target segment.

User, 2026-11-20: "我们先采用APIMART中的seedance-2.0，不要默认采用100x … 画幅固定9:16 / 根据目标段时长匹配 / 分辨率固定720P";
ratio, duration and resolution are request parameters only, never prompt text. APIMART seedance-2.0 contract
(docs.apimart.ai/cn/api-reference/videos/seedance-2-0/generation, read 2026-11-20): POST /v1/videos/generations with
model, prompt, duration (integer 4-15), size ("9:16"), resolution ("720p"), generate_audio, video_urls (at most 3, total
1.8-15.2 s), image_urls (at most 9). There is no mode field: sending video_urls makes it reference-video generation.
Ledgers written before that day (seedance-2-5-flash 480p from 11-19, seedance-2.5 before) are still read, polled and
downloaded, never submitted again.

User, 2026-11-11: "skill 还是用 seedance2.5 视频参考生来做". The segment's caption-free reference video carries scene,
people, camera, shot order, action, pacing and voices; the product images carry the product; the output is generated,
not edited (`omni_reference_task_type: reference`, 9:16, a whole number of seconds). Edit mode (`edit`, adaptive,
duration -1) was the route until that day and is no longer submitted.

At most ONE PAID generation per segment (user, 2026-11-11: "只要没有实际付费生成，都可以重提，每段实际付费只能一次").
An attempt that cost nothing goes back in the queue with the same inputs and is submitted again, up to
MAX_FREE_RESUBMITS times per segment: a task APIMART reports failed (failures are refunded in full; 11-11 G10's
"invalid_parameter" passed on the identical second try), a submit refused with a 4xx that says nothing was charged,
or a reference that could not be published. A request whose outcome is unknown (timeout, dropped connection) may
have been accepted and charged, so it is never resubmitted blindly; neither are 401/402/403 (key, balance), which
need the user. Every unpaid attempt stays in the ledger under `unpaid_attempts`.

The filename is retained for existing callers. References use the shared host and
cached image uploads. RUN/generation/jobs holds the durable submission ledger.
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, json, mimetypes, os, re, secrets, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import google_api as api  # noqa: E402
import media_pipeline as media  # noqa: E402
import hostd  # noqa: E402
from google_api import AdapterError  # noqa: E402

API = 'https://api.apimart.ai/v1'
MODEL = 'seedance-2.0'          # user 2026-11-20: APIMART seedance-2.0 (the standard 2.0, not mini / fast)
GEN_DIR = 'generation'
ASPECT_RATIO = '9:16'
RESOLUTION = '720p'
MODE = 'reference'
# Goof redos (user 2026-11-23: "第一次生成采用原片参考的模式，如果第一次出现穿帮了，第二次补生就要采用人物卡模式"): no
# reference video; character cards + product images (+ at most AUDIO_MAX voice clips of the speaker, `audio_urls`,
# published through the shared host) and a prompt that describes the whole shot. Film 1603578481269031 G01 on
# 2026-11-23: seedance-2.0 720p, 2 cards + carton views + hand scale + one 8.6 s voice clip, $1.02 for 8 s.
MODE_CARDS = 'cards'
AUDIO_MAX = 3
AUDIO_MIN_S, AUDIO_MAX_S = 2.0, 15.0
REFERENCE_MIN = 4.0
REFERENCE_MAX = 15.1   # This skill keeps target segments under 15 seconds; the reference is held up to a whole second.
OUTPUT_MIN, OUTPUT_MAX = 4, 15   # seconds seedance-2.0 accepts for `duration`
# request parameters per model; the prompt never carries ratio, duration or resolution (user 2026-11-20)
MODEL_SETTINGS = {'seedance-2.0': {'model': 'seedance-2.0', 'resolution': '720p', 'size': '9:16'},
                  'seedance-2-5-flash': {'model': 'seedance-2-5-flash', 'omni_reference_task_type': 'reference', 'resolution': '480p', 'size': '9:16'},
                  'seedance-2.5': {'model': 'seedance-2.5', 'omni_reference_task_type': 'reference', 'resolution': '480p', 'size': '9:16'}}
LEGACY_MODELS = set(MODEL_SETTINGS) - {MODEL}   # older RUN ledgers: their tasks are only polled or downloaded, never resubmitted
SETTINGS = MODEL_SETTINGS[MODEL]


def settings_for(job):
    return MODEL_SETTINGS.get(job.get('model') or MODEL, SETTINGS)
WORKERS = 16                            # parallel submit / poll / download threads per batch
PUBLISH_WORKERS = 4                     # parallel reference publishes (each is one 64-byte range check through the tunnel)
DONE = {'completed', 'succeeded', 'success'}
FAILED = {'failed', 'error', 'cancelled', 'canceled', 'expired'}
_image_upload_lock = threading.RLock()
_video_upload_lock = threading.RLock()
# Where APIMART gets each reference video (user 2026-11-20 "隧道问题怎么解决"): 'apimart' = upload the file to APIMART
# (POST /v1/uploads/videos, 3-day retention, free within the account allowance) and send that CDN URL. Film 2210 on
# 2026-11-20: every submit through the Cloudflare quick tunnel was refused ("Failed to probe reference video
# duration") -- the tunnel gave 22-80 KB/s and seedance-2.0 stops reading after ~35-50 s; the same 1.5 MB file uploaded
# to APIMART in 2.2 s. 'tunnel' = the old shared hostd route, only when the upload endpoint is down.
VIDEO_HOST = os.environ.get('LYMPHLOOP_VIDEO_HOST', 'apimart')
VIDEO_RETENTION_DAYS = 3
MAX_FREE_RESUBMITS = 3                  # unpaid attempts re-queued per segment, over all runs
NO_RESUBMIT_HTTP = {401, 402, 403}      # key, balance, permission: the user has to act


# ---------------------------------------------------------------- credentials / http
def credential():
    key = os.environ.get('APIMART_API_KEY', '').strip()
    if key: return key
    if sys.platform == 'darwin':
        r = subprocess.run(['security', 'find-generic-password', '-s', 'codex-seedance-apimart', '-a', 'api-key', '-w'], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip(): return r.stdout.strip()
    else:
        try:      # optional: pip install keyring (Windows Credential Manager / Linux Secret Service)
            if sys.platform == 'win32': from keyring.backends.Windows import WinVaultKeyring as Ring
            else: from keyring.backends.SecretService import Keyring as Ring
            key = (Ring().get_password('codex-seedance-apimart', 'api-key') or '').strip()
            if key: return key
        except Exception: pass
    raise AdapterError('APIMART key not found (APIMART_API_KEY, or the system credential store entry codex-seedance-apimart/api-key)')


class HTTPFailure(AdapterError):
    def __init__(self, code, message): super().__init__(f'APIMART HTTP {code}: {message}'); self.code = code


def call(key, path, payload=None, body=None, headers=None, method=None, timeout=180):
    h = {'Authorization': 'Bearer ' + key, **(headers or {})}
    data = body if body is not None else (json.dumps(payload).encode() if payload is not None else None)
    if payload is not None: h['Content-Type'] = 'application/json'
    req = urllib.request.Request(API + path, data=data, headers=h, method=method or ('POST' if data is not None else 'GET'))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = e.read()[:400].decode('utf-8', 'replace').replace(key, '***')
        try: msg = json.loads(msg).get('error', {}).get('message', msg)
        except Exception: pass
        raise HTTPFailure(e.code, msg) from None


def upload_image(key, path, cache_file):
    """Cached APIMART image upload. The thread lock serialises this process; the file lock next to the cache serialises
    every process sharing the RUN (read-modify-write under the lock, atomic save with a unique temp name), so parallel
    workers neither lose cache entries nor upload the same photo twice."""
    with _image_upload_lock, api.lock(str(cache_file) + '.lock', wait=600, what='the image upload cache'):
        return _upload_image_locked(key, path, cache_file)


def _upload_image_locked(key, path, cache_file):
    desc = api.file_description(path)
    cache = api.read(cache_file) if Path(cache_file).is_file() else {}
    hit = cache.get(desc['sha256'])
    if hit and hit.get('expires_unix', 0) > time.time() + 3 * 3600: return hit['url']
    boundary = 'LL' + secrets.token_hex(16); ext = mimetypes.guess_extension(desc['mime_type']) or '.png'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="ref{ext}"\r\nContent-Type: {desc["mime_type"]}\r\n\r\n').encode() \
        + Path(desc['path']).read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    r = call(key, '/uploads/images', body=body, headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    url = r.get('url')
    if not url or not url.startswith('https://'): raise AdapterError('APIMART image upload returned no https URL')
    cache[desc['sha256']] = {'url': url, 'path': desc['path'], 'expires_unix': time.time() + 72 * 3600}
    api.save(cache_file, cache)
    return url


def upload_video(key, path, cache_file):
    """Cached APIMART video upload (POST /v1/uploads/videos?retention_days=3, multipart field `file`) -> CDN URL.
    Cached by content hash in RUN/apimart/video_uploads.json and reused while it has more than a day left."""
    with _video_upload_lock, api.lock(str(cache_file) + '.lock', wait=900, what='the video upload cache'):
        desc = api.file_description(path)
        cache = api.read(cache_file) if Path(cache_file).is_file() else {}
        hit = cache.get(desc['sha256'])
        if hit and hit.get('expires_unix', 0) > time.time() + 24 * 3600: return hit['url']
        boundary = 'LL' + secrets.token_hex(16)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{Path(path).name}"\r\nContent-Type: video/mp4\r\n\r\n').encode() \
            + Path(desc['path']).read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        r = call(key, f'/uploads/videos?retention_days={VIDEO_RETENTION_DAYS}', body=body,
                 headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, timeout=600)
        url = r.get('url')
        if not url or not url.startswith('https://'): raise AdapterError('APIMART video upload returned no https URL')
        cache[desc['sha256']] = {'url': url, 'path': desc['path'], 'bytes': r.get('bytes'),
                                 'expires_unix': float(r.get('expires_at') or time.time() + VIDEO_RETENTION_DAYS * 86400)}
        api.save(cache_file, cache)
        return url


# ---------------------------------------------------------------- job contract
def validate(job):
    for k in ('job_id', 'run_dir', 'prompt_file', 'references'):
        if k not in job:
            raise AdapterError('Job missing ' + k)
    m = re.fullmatch(r'G[0-9]+-a([0-9]+)', job['job_id'])
    shot = re.fullmatch(r'G[0-9]+-[A-Za-z0-9_]+', job['job_id']) and (job.get('shot_regen') or {}).get('quote')
    if not m and not shot:
        raise AdapterError('Only G##-a1 job IDs are allowed (a shot regeneration G##-NAME carries the user\'s words in shot_regen)')
    if m and m.group(1) != '1' and not (job.get('user_approved_regen') or {}).get('quote'):
        raise AdapterError('Only the a1 job ID is allowed for each target segment; a later number needs the user\'s own request (user_approved_regen)')
    if job.get('model', MODEL) not in MODEL_SETTINGS:
        raise AdapterError(f'this skill only generates with {MODEL}')
    cards = job.get('generation_mode') == MODE_CARDS
    if job.get('generation_mode') not in (MODE, MODE_CARDS):
        raise AdapterError('generation mode must be reference (video reference generation) or cards (a goof redo)')
    if cards and not (job.get('shot_regen') or {}).get('quote'):
        raise AdapterError('card mode is for goof redos (shot_regen) only: a first generation uses the reference video')
    seconds = job.get('output_seconds')
    if type(seconds) is not int or not OUTPUT_MIN <= seconds <= OUTPUT_MAX:
        raise AdapterError(f'output_seconds must be a whole number of seconds from {OUTPUT_MIN} to {OUTPUT_MAX}')
    if str(job.get('resolution', settings_for(job)['resolution'])).lower() != settings_for(job)['resolution']:
        raise AdapterError(f"output is fixed to {settings_for(job)['resolution']}")
    if job.get('aspect_ratio', ASPECT_RATIO) != ASPECT_RATIO:
        raise AdapterError('output is fixed to 9:16')
    refs = job['references']
    if any(r['kind'] not in ('video', 'image', 'audio') for r in refs):
        raise AdapterError('references are video, image and (card mode) audio only')
    videos = [r for r in refs if r['kind'] == 'video']
    if sum(r['kind'] == 'image' for r in refs) > 9:
        raise AdapterError('Seedance 2.0 accepts at most nine reference images')
    if cards:
        if videos: raise AdapterError('card mode sends no reference video')
        if not any(r['kind'] == 'image' for r in refs): raise AdapterError('card mode needs the character cards and product images')
        audios = [r for r in refs if r['kind'] == 'audio']
        if len(audios) > AUDIO_MAX: raise AdapterError(f'at most {AUDIO_MAX} voice clips')
        for r in audios:
            d = media.probe(r['path']).get('format_duration') or 0
            if not AUDIO_MIN_S <= float(d) <= AUDIO_MAX_S + 0.05:
                raise AdapterError(f"voice clip {Path(r['path']).name}: {float(d):.1f} s, must be {AUDIO_MIN_S:g}-{AUDIO_MAX_S:g} s")
        if type(job.get('candidates', 1)) is not int or job.get('candidates', 1) != 1:
            raise AdapterError('exactly one candidate is allowed')
        return job
    if any(r['kind'] == 'audio' for r in refs):
        raise AdapterError('a reference-video job takes video and image only (voice clips go only with card mode)')
    if len(videos) != 1:
        raise AdapterError('exactly one reference video per segment')
    v = media.probe(videos[0]['path'])
    w, h = v['video']['width'], v['video']['height']
    if abs(w / h - 9 / 16) > .005:
        raise AdapterError(f'the reference video must be 9:16: {w}x{h}')
    if not 409600 <= w * h <= 8295044 or min(w, h) < 300 or max(w, h) > 6000:
        raise AdapterError(f'reference dimensions exceed Seedance video limits: {w}x{h}')
    if not REFERENCE_MIN <= v['video_duration'] < REFERENCE_MAX:
        raise AdapterError('the reference video must be at least 4 seconds and at most 15 seconds')
    if type(job.get('candidates', 1)) is not int or job.get('candidates', 1) != 1:
        raise AdapterError('exactly one candidate is allowed')
    return job


def signature(job):
    refs = []
    for r in job['references']:
        digest = api.file_sha(r['path'])
        if 'sha256' in r and digest != r['sha256']:
            raise AdapterError(f"Prepared reference changed: {r['path']}")
        refs.append([digest, r['kind']])
    return {**settings_for(job), 'duration': job.get('output_seconds'), 'references': refs}


def task_data(key, task_id):
    return call(key, '/tasks/' + urllib.parse.quote(task_id, safe=''), timeout=60).get('data', {})


def result_url(data):
    for v in (data.get('result') or {}).get('videos', []):
        u = v.get('url'); u = u[0] if isinstance(u, list) and u else u
        if isinstance(u, str) and u.startswith('https://'): return u
    found = []
    def walk(x):
        if isinstance(x, dict): [walk(v) for v in x.values()]
        elif isinstance(x, list): [walk(v) for v in x]
        elif isinstance(x, str) and x.startswith('https://') and re.search(r'\.(mp4|mov)(\?|$)', x): found.append(x)
    walk(data.get('result') or {})
    return found[0] if found else None


def payload(job, prompt, images, videos, audios=()):
    cards = job.get('generation_mode') == MODE_CARDS
    if cards and videos: raise AdapterError('card mode sends no reference video')
    if not cards and len(videos) != 1:
        raise AdapterError('exactly one reference video URL per segment')
    if job.get('model', MODEL) != MODEL:
        raise AdapterError(f"{job['job_id']}: {job.get('model')} is a legacy model; its task is only polled or downloaded, never submitted")
    body = {**SETTINGS, 'duration': int(job['output_seconds']), 'prompt': prompt, 'generate_audio': True}
    if not cards: body['video_urls'] = videos
    if images:
        body['image_urls'] = images
    if audios:
        body['audio_urls'] = list(audios)
    return body


def reopen_unpaid(c, reason):
    """Put a failed attempt that cost nothing back in the queue (status 'new'), keeping it in `unpaid_attempts`.
    False when it was paid, needs the user, or the segment used its MAX_FREE_RESUBMITS."""
    if c.get('status') == 'downloaded' or float(c.get('cost_usd') or 0) > 0: return False
    if c.get('http_status') in NO_RESUBMIT_HTTP: return False
    attempts = c.setdefault('unpaid_attempts', [])
    if len(attempts) >= MAX_FREE_RESUBMITS: return False
    attempts.append({**{k: c[k] for k in ('task_id', 'status', 'remote_status', 'error', 'http_status', 'submitted_at') if c.get(k) is not None},
                     'reason': reason, 'requeued_at': api.utc()})
    for k in ('task_id', 'remote_status', 'progress', 'error', 'http_status', 'request_payload', 'submitted_at', 'last_error'): c.pop(k, None)
    c['status'] = 'new'
    return True


# ---------------------------------------------------------------- batch
class _Job:
    """One job's ledger, file-locked for the whole batch, written by one thread only."""
    def __init__(self, job_path, candidates):
        self.job = validate(api.read(job_path)); self.run = Path(self.job['run_dir']).resolve()
        self.prompt = Path(self.job['prompt_file']).read_text(encoding='utf-8'); self.prompt_sha = api.sha(self.prompt.encode())
        if self.job.get('prompt_sha256', self.prompt_sha) != self.prompt_sha:
            raise AdapterError(f"{self.job['job_id']}: prepared prompt changed")
        job_signature = signature(self.job)
        self.path = self.run/GEN_DIR/'jobs'/(self.job['job_id'] + '.json'); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.results = self.run/GEN_DIR/'results'; self.results.mkdir(parents=True, exist_ok=True)
        self.work = self.run/'apimart'; self.work.mkdir(exist_ok=True)
        self.mutex = threading.Lock(); self.want = candidates or self.job.get('candidates', 1)
        self._lock = api.lock(self.path.with_suffix('.lock')); self._lock.__enter__()
        self.ledger = api.read(self.path) if self.path.exists() else {'type': 'apimart_job_ledger', 'provider': 'apimart', 'job': self.job,
                                                                       'job_id': self.job['job_id'], 'prompt_sha256': self.prompt_sha,
                                                                       'signature': job_signature, 'candidates': {}, 'created_at': api.utc()}
        if self.ledger.get('provider', 'apimart') != 'apimart':
            self.close(); raise AdapterError(f"{self.job['job_id']} is a {self.ledger.get('provider')} ledger (swap.py splice); only a1 can be submitted")
        if self.ledger['prompt_sha256'] != self.prompt_sha or self.ledger['signature'] != job_signature:
            self.close(); raise AdapterError(f"{self.job['job_id']}: job inputs changed since first submission; keep the ledger and original job inputs")
        self.cands = {f'c{i}': self.ledger['candidates'].setdefault(f'c{i}', {'status': 'new'}) for i in range(1, self.want + 1)}
        for cid, c in self.cands.items():
            if c.get('status') == 'submitting' and not c.get('task_id'):
                self.close(); raise AdapterError(f"{self.job['job_id']} {cid} was submitted but no task id was recorded; check APIMART task history before re-running")

    def persist(self):
        with self.mutex: self.ledger['updated_at'] = api.utc(); api.save(self.path, self.ledger)

    def close(self):
        if self._lock: self._lock.__exit__(None, None, None); self._lock = None


def run_batch(job_paths, candidates=None, max_wait=2400, workers=WORKERS):
    """Submit new jobs; re-queue attempts that cost nothing (reopen_unpaid) until each segment has one paid result or used
    its free resubmits. On resume, poll and download the existing tasks."""
    from concurrent.futures import ThreadPoolExecutor
    if candidates not in (None, 1):
        raise AdapterError('exactly one candidate is allowed')
    key = credential(); jobs = []
    try:
        for p in job_paths: jobs.append(_Job(p, candidates))
        refs = {}

        def body_for(j):
            images = [upload_image(key, r['path'], j.work/'image_uploads.json') for r in j.job['references'] if r['kind'] == 'image']
            videos = []
            for r in j.job['references']:
                if r['kind'] == 'video':
                    k = str(Path(r['path']).resolve())
                    videos.append(refs[k])
            # voice clips (card mode): APIMART has no audio upload endpoint; the shared host serves them
            audios = [hostd.publish(str(Path(r['path']).resolve())) for r in j.job['references'] if r['kind'] == 'audio']
            return payload(j.job, j.prompt, images, videos, audios)

        def submit(j, cid):
            c = j.cands[cid]
            try:
                body = body_for(j)
            except Exception as e:
                c.update(error=f'references: {e}'[:300]); j.persist(); return
            # machine-wide fetch gate (hostd.reserve): waits while FETCH_SLOTS references are still being pulled by
            # APIMART, so no download is starved past APIMART's 60 s limit however many sessions submit at once
            vids = [(refs[str(Path(r['path']).resolve())], r['path']) for r in j.job['references'] if r['kind'] == 'video']
            gate = None
            if VIDEO_HOST == 'tunnel':      # only the tunnel route needs the machine-wide fetch gate
                try: gate = hostd.reserve(vids, note=f"{j.job['job_id']}/{cid}")
                except Exception as e: c.update(error=f'fetch gate: {e}'[:300]); j.persist(); return
            c.pop('error', None)
            c.update(status='submitting', submitted_at=api.utc(), generation_submit_count=int(c.get('generation_submit_count') or 0) + 1, request_payload=body); j.persist()
            try: r = call(key, '/videos/generations', payload=body)
            except HTTPFailure as e:
                if gate and 400 <= e.code < 500 and e.code != 408: hostd.release(gate)     # refused before any fetch
                c.update(status='known_failed' if 400 <= e.code < 500 and e.code != 408 else 'outcome_unknown', error=str(e)[:300], http_status=e.code); j.persist(); return
            except Exception as e:
                c.update(status='outcome_unknown', error=type(e).__name__); j.persist(); return
            data = r.get('data'); data = data[0] if isinstance(data, list) and data else (data or {})
            tid = data.get('task_id') or data.get('id')
            if not tid: c.update(status='outcome_unknown', error='no task id in response'); j.persist(); return
            c.update(status='running', task_id=tid); j.persist()

        def requeue():
            """Unpaid failures (and references that never got submitted) go back to 'new'."""
            for j in jobs:
                for cid, c in j.cands.items():
                    st = c.get('status')
                    why = {'failed': 'task failed (refunded)', 'known_failed': 'submit refused before any charge'}.get(st)
                    if st == 'new' and c.get('error') and not c.get('task_id'): why = 'not submitted: ' + c['error'][:80]
                    if why and reopen_unpaid(c, why): j.persist()

        def submit_round():
            todo = [(j, cid) for j in jobs for cid, c in j.cands.items() if not c.get('task_id') and c.get('status') == 'new']
            if not todo: return False
            cache_of = {}
            for j, _ in todo:          # publish every distinct reference once, in parallel, before submitting
                for r in j.job['references']:
                    if r['kind'] == 'video':
                        k = str(Path(r['path']).resolve()); refs.setdefault(k, None); cache_of.setdefault(k, j.work/'video_uploads.json')
            todo_refs = [k for k in refs if not refs[k]]
            if VIDEO_HOST == 'tunnel':
                # at most PUBLISH_WORKERS verifications at once: they share one tunnel with APIMART's own downloads
                keys = list(refs)       # republished every round: a restarted tunnel changes the URL
                with ThreadPoolExecutor(max_workers=min(PUBLISH_WORKERS, max(1, len(keys)))) as ex:
                    for k, url in zip(keys, ex.map(hostd.publish, keys)): refs[k] = url
            else:
                with ThreadPoolExecutor(max_workers=min(4, max(1, len(todo_refs)))) as ex:
                    for k, url in zip(todo_refs, ex.map(lambda k: upload_video(key, k, cache_of[k]), todo_refs)): refs[k] = url
            by_job = {}
            for j, cid in todo: by_job.setdefault(id(j), (j, []))[1].append(cid)
            with ThreadPoolExecutor(max_workers=workers) as ex: list(ex.map(lambda jc: [submit(jc[0], cid) for cid in jc[1]], by_job.values()))
            return True

        def poll(j, cid):
            c = j.cands[cid]
            out = j.results/f"{j.job['job_id']}-{cid}.mp4"
            if out.is_file() and c.get('output_sha256') == api.file_sha(out): c['status'] = 'downloaded'; return False
            try: data = task_data(key, c['task_id'])
            except Exception as e: c['last_error'] = type(e).__name__; return True
            status = str(data.get('status', '')).lower(); c.update(remote_status=status, progress=data.get('progress'))
            if status in FAILED:
                err = str(data.get('error') or data.get('message') or '')[:300]
                c.update(status='failed', error=err, cost_usd=data.get('cost')); j.persist(); return False
            url = result_url(data) if status in DONE else None
            if not url: j.persist(); return True
            with urllib.request.urlopen(url, timeout=600) as s: out.write_bytes(s.read())
            info = media.probe(str(out))
            c.update(status='downloaded', output_path=str(out), output_sha256=api.file_sha(out), cost_usd=data.get('cost'),
                     generation_seconds=data.get('actual_time'), duration_seconds=info['video_duration'], frame_count=info['frame_count'],
                     fps=info['fps'], has_audio=bool(info['audio']), result_url=url); j.persist()
            return False

        def open_cands(j): return [cid for cid, c in j.cands.items() if c.get('task_id') and c.get('status') not in ('downloaded', 'known_failed', 'failed')]
        deadline = time.monotonic() + max_wait
        requeue()                                 # unpaid failures left by an earlier run
        for _ in range(MAX_FREE_RESUBMITS + 1):
            refs.clear()                          # republish every round: a failed fetch may need a fresh link
            submit_round()
            with ThreadPoolExecutor(max_workers=workers) as ex:
                while True:
                    live = [j for j in jobs if open_cands(j)]
                    if not live or not any(list(ex.map(lambda j: any([poll(j, cid) for cid in open_cands(j)]), live))) or time.monotonic() > deadline: break
                    time.sleep(20)
            requeue()
            if not any(c.get('status') == 'new' for j in jobs for c in j.cands.values()) or time.monotonic() > deadline: break
        for j in jobs: j.persist()
        return [j.ledger for j in jobs]
    finally:
        for j in jobs: j.close()


def run_job(job_path, candidates=None, max_wait=2400):
    return run_batch([job_path], candidates, max_wait)[0]


def main():
    p = argparse.ArgumentParser(description=__doc__); s = p.add_subparsers(dest='command', required=True)
    r = s.add_parser('run-job'); r.add_argument('job'); r.add_argument('--candidates', type=int)
    b = s.add_parser('run-batch', help='submit every job at once and poll them together'); b.add_argument('jobs', nargs='+'); b.add_argument('--candidates', type=int)
    a = p.parse_args()
    try:
        ledgers = run_batch(a.jobs if a.command == 'run-batch' else [a.job], a.candidates)
        out = [{'job_id': l['job_id'], 'candidates': {k: {f: v.get(f) for f in ('status', 'task_id', 'cost_usd', 'output_path', 'error', 'generation_submit_count')}
                                                      for k, v in l['candidates'].items()}} for l in ledgers]
        print(json.dumps(out[0] if a.command == 'run-job' else out, ensure_ascii=False, indent=2))
    except AdapterError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); sys.exit(1)


if __name__ == '__main__': main()
