#!/usr/bin/env python3
"""Default generation channel (user 2026-11-26): 100x `100x-minimax-h3` (`standard`, `768P`, `9:16`) for jobs already
prepared in RUN/jobs/ (also supports `100x-seedance-2.0` `720P` `9:16` via `LYMPHLOOP_X100_MODEL=100x-seedance-2.0` or
`job['x100']['model']`). Switch back to APIMART via ~/.cache/lymphloop-models/local-slots/channel.txt (`apimart`) or
LYMPHLOOP_GEN_CHANNEL=apimart if needed.

Ratio, duration and resolution are request parameters, never prompt text (user 2026-11-20). 100x seedance-2.5 is not
used: it has no 9:16 parameter, only 'adaptive', which follows the prompt (11-19 G14 came back landscape).

    x100_gen.py quote     --run RUN JOB...            # free: uploads + price, prints credits and the balance
    x100_gen.py submit    --run RUN JOB...            # paid, once per job; the task id is on disk before anything else
    x100_gen.py poll      --run RUN JOB...            # waits, downloads, writes the skill ledger (provider x100)
    x100_gen.py run-batch JOB_JSON... [--candidates 1] # drop-in replacement for apimart_wan.py run-batch (JSON stdout)

Rules kept from the main channel: one paid result per job; a job id other than G##-a1 is a regeneration the user
asked for (`user_approved_regen` or `shot_regen` carries their words or goof rule); a job whose ledger already has a
downloaded result is reused without re-charging. Credentials stay in the 100x bridge (keychain `100x-codex`)."""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, time, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MODEL = '100x-minimax-h3'        # default 100x model (user 2026-11-26: standard / 768P / 9:16)
SETTINGS = {
    '100x-minimax-h3': {'resolution': '768P', 'aspect_ratio': '9:16'},
    '100x-seedance-2.0': {'resolution': '720P', 'aspect_ratio': '9:16'},
}   # every setting is a request parameter
GEN_DIR = 'generation'
CLIENT_DIRS = [Path(os.environ['X100_CLIENT_DIR'])] if os.environ.get('X100_CLIENT_DIR') else []
SKILL = Path(__file__).resolve().parents[1]
# the 100x client belongs to the ai-song-ad skill: next to this skill, else in any agent's skills folder (~/.<agent>/skills)
CLIENT_DIRS += [HERE,   # bundled x100.py
                SKILL.parent/'ai-song-ad'/'scripts', *sorted(Path.home().glob('.*/skills/ai-song-ad/scripts'))]
NODE_DIRS = [Path.home()/'.local/node22/bin']      # the bridge needs Node >= 22 (system node was 18 on this Mac)
SUPPORTED_AUDIO_EXTS = {'.mp3', '.wav'}


def job_settings(job):
    x = job.get('x100') or {}
    env_model = os.environ.get('LYMPHLOOP_X100_MODEL', '').strip()
    env_res = os.environ.get('LYMPHLOOP_X100_RESOLUTION', '').strip()
    if env_model:
        model = env_model
        if model not in SETTINGS:
            raise SystemExit(f"{job.get('job_id')}: {model} has no 9:16 parameter; only {', '.join(SETTINGS)} (user 2026-11-20)")
        base = SETTINGS[model]
        return model, env_res or base['resolution'], x.get('aspect_ratio') or base['aspect_ratio']
    model = x.get('model') or (job.get('model') if str(job.get('model', '')).startswith('100x-') else MODEL)
    if model not in SETTINGS:
        raise SystemExit(f"{job.get('job_id')}: {model} has no 9:16 parameter; only {', '.join(SETTINGS)} (user 2026-11-20)")
    base = SETTINGS[model]
    return model, x.get('resolution') or env_res or base['resolution'], x.get('aspect_ratio') or base['aspect_ratio']


def read(p): return json.loads(Path(p).read_text())
def save(p, d):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S')


def client_module():
    for d in NODE_DIRS:
        if d.is_dir() and str(d) not in os.environ.get('PATH', '').split(':'):
            os.environ['PATH'] = f"{d}:{os.environ.get('PATH', '')}"
    for d in CLIENT_DIRS:
        if (d/'x100.py').is_file():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            import x100   # noqa: E402
            return x100
    raise SystemExit('100x client x100.py not found (ai-song-ad skill); set X100_CLIENT_DIR')


def _probe_duration(path: Path) -> float | None:
    try:
        import media_pipeline as media
        ffprobe = media.binary('ffprobe')
    except Exception:
        ffprobe = 'ffprobe'
    try:
        r = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True, text=True, check=True,
        )
        v = float(r.stdout.strip())
        return v if v > 0 else None
    except Exception:
        return None


def _ensure_uploadable_media(path: Path, kind: str, cache_dir: Path) -> tuple[Path, float | None]:
    """Ensure audio/video file has a MIME type accepted by 100x media.mjs and a valid duration."""
    if kind == 'image':
        return path, None
    up_path = path
    if kind == 'audio' and path.suffix.lower() not in SUPPORTED_AUDIO_EXTS:
        conv_dir = cache_dir / 'audio_conv'
        conv_dir.mkdir(parents=True, exist_ok=True)
        out = conv_dir / f'{path.stem}_{sha(path)[:10]}.mp3'
        if not out.is_file():
            try:
                import media_pipeline as media
                ffmpeg = media.binary('ffmpeg')
            except Exception:
                ffmpeg = 'ffmpeg'
            subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(path), '-vn', '-c:a', 'libmp3lame', '-q:a', '2', str(out)], check=True)
        up_path = out
    dur = _probe_duration(up_path)
    return up_path, dur


def approval_problem(job, ledger):
    """None when this job may be paid for on 100x, else the reason."""
    c = (ledger or {}).get('candidates', {}).get('c1', {})
    if c.get('status') == 'downloaded' or c.get('output_path'):
        return f"{job['job_id']}: already has a paid result ({c.get('output_path')}); a new try needs a new job id"
    if re.fullmatch(r'G\d+-a1', job['job_id']):
        return None
    sr = job.get('shot_regen') or {}
    quote = (job.get('user_approved_regen') or {}).get('quote') or sr.get('quote') or sr.get('goof')
    if not quote:
        return (f"{job['job_id']}: not a first attempt and no user words recorded; regenerations only when the user asked "
                "(swap.py generate --user-approved-regen / shot_regen.py prepare --user-quote)")
    return None


class Channel:
    def __init__(self, run):
        self.run = Path(run).resolve(); self.led_dir = self.run/'x100'; self.led_dir.mkdir(exist_ok=True)
        self.x = client_module()

    def ledger(self, job_id):
        p = self.led_dir/f'{job_id}.json'
        return (read(p) if p.is_file() else {'job_id': job_id, 'model': MODEL}), p

    def prepare(self, job_id, job_path: Path | None = None):
        jp = Path(job_path) if job_path else (self.run/'jobs'/f'{job_id}.json')
        job = read(jp)
        if sha(job['prompt_file']) != job['prompt_sha256']:
            raise SystemExit(f'{job_id}: prompt changed since prepare')
        problem = approval_problem(job, read(self.skill_ledger(job_id)) if self.skill_ledger(job_id).is_file() else None)
        led, p = self.ledger(job_id)
        if problem and not led.get('task_id'):
            raise SystemExit(problem)
        prompt = Path(job['prompt_file']).read_text()
        assets = led.setdefault('assets', {}); ids = []
        for r in job['references']:                     # order kept: @视频1 is the video, @图片N the Nth image, @音频N the Nth audio
            if sha(r['path']) != r['sha256']:
                raise SystemExit(f"{job_id}: {r['path']} changed since prepare")
            if r['sha256'] not in assets:
                kind = r.get('kind', 'image')
                up_path, probed_dur = _ensure_uploadable_media(Path(r['path']), kind, self.led_dir)
                dur = None
                if kind in ('video', 'audio'):
                    dur = r.get('duration_seconds') or probed_dur
                    if dur is None:
                        raise SystemExit(f"{job_id}: cannot determine duration for {kind} ref {r['path']}")
                assets[r['sha256']] = self.x.upload(up_path, float(dur) if dur is not None else None)
                save(p, led)
            ids.append(assets[r['sha256']])
        model, resolution, aspect = job_settings(job)
        line = os.environ.get('LYMPHLOOP_X100_LINE', 'standard').strip() or 'standard'
        qargs = {'model': model, 'prompt': prompt, 'mode': 'reference', 'aspect_ratio': aspect, 'resolution': resolution,
                 'duration_seconds': int(job['output_seconds']), 'references': ids, 'line': line}
        led.update(model=model, job=job, qargs={k: v for k, v in qargs.items() if k != 'prompt'}, prompt_sha256=job['prompt_sha256'])
        save(p, led)
        return led, p, qargs

    def skill_ledger(self, job_id): return self.run/GEN_DIR/'jobs'/f'{job_id}.json'

    def write_skill_ledger(self, led):
        """RUN/generation/jobs/<job>.json in the skill's format: review / select / splice / captions / recut take it as
        this job's paid result. Earlier unpaid APIMART attempts stay under `apimart_attempts`."""
        path = self.skill_ledger(led['job_id']); old = read(path) if path.is_file() else {}
        out = Path(led['file'])
        rec = {'type': 'apimart_job_ledger', 'provider': 'x100', 'job_id': led['job_id'],
               'job': {**led['job'], 'model': led.get('qargs', {}).get('model', MODEL)}, 'prompt_sha256': led['prompt_sha256'],
               'apimart_attempts': old.get('candidates') if old.get('provider') != 'x100' else old.get('apimart_attempts'),
               'x100_ledger': str(self.led_dir/f"{led['job_id']}.json"),
               'candidates': {'c1': {'status': 'downloaded', 'task_id': led['task_id'], 'output_path': str(out),
                                     'output_sha256': sha(out), 'cost_credits': led.get('credits_charged'),
                                     'generation_submit_count': int(led.get('attempts') or 1), 'downloaded_at': now()}},
               'created_at': old.get('created_at') or now(), 'updated_at': now()}
        save(path, rec)
        return rec

    def write_failed_ledger(self, led):
        path = self.skill_ledger(led['job_id']); old = read(path) if path.is_file() else {}
        rec = {'type': 'apimart_job_ledger', 'provider': 'x100', 'job_id': led['job_id'],
               'job': {**led.get('job', {}), 'model': led.get('qargs', {}).get('model', MODEL)},
               'prompt_sha256': led.get('prompt_sha256', ''),
               'apimart_attempts': old.get('candidates') if old.get('provider') != 'x100' else old.get('apimart_attempts'),
               'x100_ledger': str(self.led_dir/f"{led['job_id']}.json"),
               'candidates': {'c1': {'status': 'known_failed', 'task_id': led.get('task_id'),
                                     'error': str(led.get('error') or '100x failed')[:400],
                                     'generation_submit_count': int(led.get('attempts') or 1)}},
               'created_at': old.get('created_at') or now(), 'updated_at': now()}
        save(path, rec)
        return rec


def _download_atomic(x, url: str, out: Path):
    """Download via media.mjs (which opens O_EXCL) to a unique temp file, then atomically rename."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f'.{out.stem}.{uuid.uuid4().hex[:8]}.tmp{out.suffix}'
    try:
        x.download(url, tmp)
        tmp.replace(out)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def run_batch(job_paths: list[str], timeout_s: int = 3600, max_retries: int = 2) -> list[dict]:
    """Drop-in replacement for `apimart_wan.py run-batch`: submits all pending jobs to 100x seedance-2.0
    (720P, 9:16), polls in parallel, retries unpaid generation failures up to `max_retries` times,
    downloads results, writes canonical skill ledgers, and returns the list of ledgers."""
    by_run: dict[Path, list[tuple[Path, str]]] = {}
    order: list[tuple[Path, str]] = []
    for jp_str in job_paths:
        jp = Path(jp_str).resolve()
        job = read(jp)
        run_dir = Path(job.get('run_dir') or jp.parents[1]).resolve()
        jid = job['job_id']
        by_run.setdefault(run_dir, []).append((jp, jid))
        order.append((run_dir, jid))

    ledgers_out: dict[tuple[Path, str], dict] = {}
    x = client_module()
    with x.Client() as c:
        bal = c.balance()
        print(f'[x100 run-batch] balance={bal:.2f} credits, jobs={len(order)}', file=sys.stderr, flush=True)
        for run_dir, items in by_run.items():
            ch = Channel(run_dir)
            pending: list[tuple[dict, Path, dict]] = []  # (led, x100_path, qargs)
            for jp, jid in items:
                sk_path = ch.skill_ledger(jid)
                if sk_path.is_file():
                    sk = read(sk_path)
                    c1 = (sk.get('candidates') or {}).get('c1') or {}
                    if c1.get('status') == 'downloaded' and c1.get('output_path') and Path(c1['output_path']).is_file():
                        print(f'[x100 run-batch] {jid}: already downloaded ({c1["output_path"]}), skipping', file=sys.stderr, flush=True)
                        ledgers_out[(run_dir, jid)] = sk
                        continue
                led, lpath, qargs = ch.prepare(jid, job_path=jp)
                # If a prior 100x attempt already downloaded cleanly, sync skill ledger and skip
                if led.get('status') == 'downloaded' and led.get('file') and Path(led['file']).is_file():
                    ledgers_out[(run_dir, jid)] = ch.write_skill_ledger(led)
                    continue
                # If a prior 100x attempt failed (unpaid), clear task/quote so this invocation submits cleanly
                if led.get('status') in ('failed', 'download_missing'):
                    hist = led.setdefault('failed_history', [])
                    if led.get('task_id'):
                        hist.append({'task_id': led.get('task_id'), 'error': led.get('error'), 'at': now()})
                    for k in ('task_id', 'quote_id', 'status', 'status_raw', 'error', 'submit_resp'):
                        led.pop(k, None)
                    save(lpath, led)
                pending.append((led, lpath, qargs))

            # Submit all pending jobs in batch
            for led, lpath, qargs in pending:
                if led.get('task_id'):
                    print(f"[x100 run-batch] {led['job_id']}: resuming existing task {led['task_id']}", file=sys.stderr, flush=True)
                    continue
                led['key'] = f"{led['job_id']}-{uuid.uuid4().hex[:16]}"
                led['attempts'] = int(led.get('attempts') or 0) + 1
                save(lpath, led)
                try:
                    x.submit(c, 'video', qargs, led)
                    print(f"[x100 run-batch] {led['job_id']}: submitted {led.get('task_id')} ({led.get('credits')} credits)", file=sys.stderr, flush=True)
                except x.X100Error as err:
                    led.update(status='failed', error=f'{err.code}: {err}'[:400])
                    print(f"[x100 run-batch] {led['job_id']}: submit failed: {led['error']}", file=sys.stderr, flush=True)
                finally:
                    save(lpath, led)

            # Poll all submitted jobs in parallel; retry unpaid 100X_GENERATION_FAILED up to max_retries
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                live = [(led, lpath, qargs) for led, lpath, qargs in pending
                        if led.get('task_id') and led.get('status') not in ('downloaded', 'failed', 'download_missing')]
                if not live:
                    break
                for led, lpath, qargs in live:
                    try:
                        t = c.task(led['task_id'])
                    except x.X100Error as err:
                        if err.code == '100X_GENERATION_FAILED':
                            led.update(status='failed', error=str(err)[:400])
                        else:
                            led['last_error'] = str(err)[:300]
                            continue
                    else:
                        st = str(t.get('status', '')).lower(); led['status_raw'] = st
                        if st in x.DONE:
                            urls = x.output_urls(t)
                            out = ch.run/GEN_DIR/'results'/f"{led['job_id']}-c1.mp4"
                            if not urls:
                                led.update(status='download_missing', error='task succeeded with no output URL')
                            else:
                                if not out.exists():
                                    _download_atomic(x, urls[0], out)
                                led.update(status='downloaded', file=str(out), credits_charged=t.get('credits', led.get('credits')))
                                print(f"[x100 run-batch] {led['job_id']}: downloaded ({led.get('credits_charged')} credits)", file=sys.stderr, flush=True)
                        elif st in x.FAILED:
                            led.update(status='failed', error=str(t.get('error') or t)[:400])
                        else:
                            led['status'] = 'running'

                    # Auto-retry unpaid generation failures (up to max_retries)
                    if led.get('status') == 'failed' and int(led.get('attempts') or 1) <= max_retries:
                        err_s = str(led.get('error') or '')
                        if not any(k in err_s for k in ('INSUFFICIENT', 'BALANCE', 'BUDGET', 'MODERATION', 'NSFW')):
                            led.setdefault('failed_history', []).append(
                                {'task_id': led.get('task_id'), 'error': err_s, 'at': now()})
                            for k in ('task_id', 'quote_id', 'status', 'status_raw', 'error', 'submit_resp'):
                                led.pop(k, None)
                            led['key'] = f"{led['job_id']}-{uuid.uuid4().hex[:16]}"
                            led['attempts'] = int(led.get('attempts') or 1) + 1
                            save(lpath, led)
                            try:
                                x.submit(c, 'video', qargs, led)
                                print(f"[x100 run-batch] {led['job_id']}: retry #{led['attempts']} submitted {led.get('task_id')}", file=sys.stderr, flush=True)
                            except x.X100Error as rerr:
                                led.update(status='failed', error=f'{rerr.code}: {rerr}'[:400])
                    save(lpath, led)
                still_live = [led for led, _, _ in pending
                              if led.get('task_id') and led.get('status') not in ('downloaded', 'failed', 'download_missing')]
                if not still_live:
                    break
                time.sleep(12)

            for led, lpath, _ in pending:
                if led.get('status') not in ('downloaded', 'failed', 'download_missing'):
                    led.update(status='failed', error=f'timeout after {timeout_s}s')
                save(lpath, led)
                if led.get('status') == 'downloaded':
                    ledgers_out[(run_dir, led['job_id'])] = ch.write_skill_ledger(led)
                else:
                    ledgers_out[(run_dir, led['job_id'])] = ch.write_failed_ledger(led)

    return [ledgers_out[k] for k in order]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('action', choices=['quote', 'submit', 'poll', 'run-batch'])
    p.add_argument('--run', default=None)
    p.add_argument('jobs', nargs='+')
    p.add_argument('--candidates', type=int, default=1)
    p.add_argument('--timeout', type=int, default=3600)
    a = p.parse_args()
    if a.action == 'run-batch':
        res = run_batch(a.jobs, timeout_s=a.timeout)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        failed = [r['job_id'] for r in res if (r.get('candidates', {}).get('c1', {}).get('status') != 'downloaded')]
        if failed:
            sys.exit(1)
        return
    if not a.run:
        raise SystemExit('--run is required for quote/submit/poll')
    ch = Channel(a.run); x = ch.x
    with x.Client() as c:
        if a.action in ('quote', 'submit'):
            print('balance', c.balance()); total = 0
            for j in a.jobs:
                led, path, qargs = ch.prepare(j)
                if led.get('task_id'): print(j, 'already submitted', led['task_id']); continue
                if a.action == 'quote':
                    q = c.quote(**qargs); total += q['credits']; print(j, 'credits', q['credits']); continue
                led.setdefault('key', f'{j}-{uuid.uuid4().hex[:16]}'); save(path, led)
                try: x.submit(c, 'video', qargs, led)
                finally: save(path, led)              # task id (or the error) is on disk before anything else
                print(j, 'submitted', led.get('task_id'), 'credits', led.get('credits'))
            if a.action == 'quote': print('total credits', total)
            return
        leds = [ch.ledger(j) for j in a.jobs]
        def save_all():
            for l, pth in leds: save(pth, l)
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            live = [l for l, _ in leds if l.get('task_id') and l.get('status') not in ('downloaded', 'failed', 'download_missing')]
            if not live: break
            for e in live:
                try: t = c.task(e['task_id'])
                except x.X100Error as err:          # 11-19 G19: a failed task comes back as an error, never as a status
                    if err.code == '100X_GENERATION_FAILED': e.update(status='failed', error=str(err)[:300])
                    else: e['last_error'] = str(err)[:300]
                    continue
                st = str(t.get('status', '')).lower(); e['status_raw'] = st
                if st in x.DONE:
                    urls = x.output_urls(t); out = ch.run/GEN_DIR/'results'/f"{e['job_id']}-c1.mp4"
                    if not urls: e['status'] = 'download_missing'
                    else:
                        if not out.exists(): _download_atomic(x, urls[0], out)
                        e.update(status='downloaded', file=str(out), credits_charged=t.get('credits', e.get('credits')))
                elif st in x.FAILED: e.update(status='failed', error=str(t.get('error') or t)[:300])
                else: e['status'] = 'running'
            save_all(); time.sleep(15)
        save_all()
        for led, _ in leds:
            print(led['job_id'], led.get('status'), led.get('credits_charged'), led.get('error') or '')
            if led.get('status') == 'downloaded': ch.write_skill_ledger(led)


if __name__ == '__main__':
    main()
