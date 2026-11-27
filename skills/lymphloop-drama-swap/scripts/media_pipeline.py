#!/usr/bin/env python3
"""Deterministic local media operations. Source ranges are half-open decoded-frame indices."""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import inspect
from fractions import Fraction

SR = 48000


SKILL_BIN = Path(__file__).resolve().parents[1] / 'bin'
CODEX_TOOLS = Path.home() / '.codex/tools/ffmpeg'   # last resort: the Codex machine-wide ffmpeg install


def binary(name):
    """The one tool lookup every script and test uses, so the same files run from ~/.claude/skills/... and
    ~/.codex/skills/...: env override (FFMPEG_BIN / FFPROBE_BIN; ffprobe also next to FFMPEG_BIN) -> this skill's
    bin/<name> -> PATH -> Homebrew / /usr/local / ~/.local/bin -> ~/.codex/tools/ffmpeg/<name>."""
    override = os.environ.get(name.upper() + '_BIN')
    choices = [override]
    if name == 'ffprobe' and os.environ.get('FFMPEG_BIN'):
        choices.append(str(Path(os.environ['FFMPEG_BIN']).with_name(oscompat.exe('ffprobe'))))
    file = oscompat.exe(name)      # ffmpeg.exe on Windows
    choices += [str(SKILL_BIN / file), shutil.which(name),
                f'/opt/homebrew/bin/{name}', f'/usr/local/bin/{name}', str(Path.home() / '.local/bin' / file), str(CODEX_TOOLS / file)]
    for value in choices:
        if value and Path(value).is_file() and os.access(value, os.X_OK):
            return str(Path(value).resolve())
    raise ValueError(f'{name} unavailable; install it or set {name.upper()}_BIN')


def run(args):
    p = subprocess.run(list(map(str, args)), capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f'Command failed ({p.returncode}): {p.stderr[-7000:]}')
    return p.stdout


def ff(args):
    return run([binary('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', *args])


def save(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def ratio(value):
    f = Fraction(str(value))
    if f <= 0:
        raise ValueError('Frame rate must be positive')
    return f


def rounded(value):
    """Round nonnegative rational values half up, independently of Python banker's rounding."""
    f = Fraction(value)
    return (2 * f.numerator + f.denominator) // (2 * f.denominator)


def seconds(value):
    return Fraction(str(value))


def stream_duration_exact(info, kind):
    if kind == 'video':
        return Fraction(info['frame_count']) / ratio(info['fps'])
    stream = info['audio']
    if stream.get('duration_ts') is not None and stream.get('time_base'):
        return Fraction(int(stream['duration_ts'])) * Fraction(stream['time_base'])
    return seconds(stream.get('duration', info['format_duration']))


def snap_near_grid(value, rate):
    nearest = Fraction(rounded(value * rate)) / rate
    return nearest if abs(nearest-value) <= Fraction(1, 1000000) else value


def frame_to_tc(frame, fps):
    nominal = rounded(ratio(fps))
    h, r = divmod(int(frame), nominal * 3600)
    m, r = divmod(r, nominal * 60)
    s, f = divmod(r, nominal)
    return f'[{h:02d}:{m:02d}:{s:02d}:{f:02d}]'


def _probe_json(path, flags):
    """Reuse a completed decode only while the file and decoder remain unchanged."""
    source = Path(path).resolve(); decoder = Path(binary('ffprobe'))
    def identity(p):
        s = p.stat()
        return [str(p), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, oscompat.change_ns(p, s)]
    stamp = [identity(source), identity(decoder), list(flags), 1]
    key = hashlib.sha256(json.dumps(stamp).encode()).hexdigest()
    root = Path(os.environ.get('LYMPHLOOP_PROBE_CACHE', str(Path.home()/'.cache/lymphloop-media-probe')))
    cache = root/f'{key}.json'
    try:
        record = json.loads(cache.read_text())
        if record['identity'] == stamp: return record['result']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result = json.loads(run([str(decoder), '-v', 'error', *flags, '-of', 'json', str(source)]))
    # A file being rendered must never acquire a reusable partial probe.
    if stamp[:2] == [identity(source), identity(decoder)]:
        tmp = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile('w', dir=root, prefix=key+'.', suffix='.tmp', delete=False) as f:
                tmp = Path(f.name); json.dump({'identity': stamp, 'result': result}, f)
            oscompat.replace(tmp, cache)
        except OSError:
            pass
        finally:
            if tmp is not None: tmp.unlink(missing_ok=True)
    return result


def probe(path, include_frames=False):
    raw = _probe_json(path, ['-count_frames', '-show_streams', '-show_format'])
    video = next((s for s in raw['streams'] if s['codec_type'] == 'video'), None)
    audio = next((s for s in raw['streams'] if s['codec_type'] == 'audio'), None)
    answer = {'path': str(Path(path).resolve()), 'video': video, 'audio': audio,
              'format_duration': float(raw.get('format', {}).get('duration', 0))}
    if video:
        rate = video.get('avg_frame_rate', '0/0')
        if rate == '0/0':
            rate = video.get('r_frame_rate', '0/0')
        answer['fps'] = str(ratio(rate))
        answer['frame_count'] = int(video.get('nb_read_frames') or video.get('nb_frames') or 0)
        answer['video_duration'] = answer['frame_count'] / float(ratio(rate))
        if include_frames:
            frames = _probe_json(path, ['-select_streams', 'v:0',
                '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time,pkt_duration_time',
                ])['frames']
            pts = [float(f['best_effort_timestamp_time']) for f in frames]
            delta = [b - a for a, b in zip(pts, pts[1:])]
            expected = 1 / float(ratio(answer['fps']))
            tolerance = max(0.00001, float(Fraction(video.get('time_base', '1/1000000'))) * 1.1)
            answer['cfr'] = not delta or all(abs(d - expected) <= tolerance for d in delta)
            answer['pts_start'] = pts[0] if pts else 0
            answer['frame_pts'] = pts
            answer['delta_median'] = statistics.median(delta) if delta else expected
    return answer


def require_cfr(info):
    if not info.get('video') or not info.get('frame_count'):
        raise ValueError('A decodable video stream is required')
    if not info.get('cfr', False):
        raise ValueError('Variable frame timing: run normalize and use its output as the source timeline')
    if abs(info.get('pts_start', 0)) > 0.0001:
        raise ValueError('Nonzero starting video PTS: normalize before indexing a timeline')


def protect_output(source, output):
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError('Output must differ from the source')
    Path(output).parent.mkdir(parents=True, exist_ok=True)


def normalize(source, output, fps=None):
    protect_output(source, output)
    before = probe(source, True)
    target = ratio(fps or rounded(ratio(before['fps'])))
    if target.denominator != 1:
        raise ValueError('Normalization uses an integer CFR rate; pass an integer --fps')
    # Keep the input audio/video relative offset, then force a zero-based video edit grid.
    vstart = before.get('pts_start', 0)
    vf = f'setpts=PTS-({vstart:.12f})/TB,fps={target},setsar=1,format=yuv420p'
    cmd = ['-y', '-reinit_filter', '0', '-i', source, '-map', '0:v:0', '-vf', vf, '-c:v', 'libx264',
           '-preset', 'fast', '-crf', '18', '-fps_mode', 'cfr', '-r', str(target)]
    if before.get('audio'):
        # Preserve gaps/offsets relative to the first video PTS; resample fills leading gaps.
        cmd += ['-map', '0:a:0', '-af', f'asetpts=PTS-({vstart:.12f})/TB,aresample={SR}:async=1:first_pts=0',
                '-c:a', 'aac', '-b:a', '192k']
    cmd += ['-map_metadata', '-1', '-movflags', '+faststart', output]
    ff(cmd)
    after = probe(output, True)
    require_cfr(after)
    origin = before.get('frame_pts', [])
    # Record nearest source frame for auditing the normalized frame grid.
    import bisect
    mapping = []
    for i, p in enumerate(after['frame_pts']):
        t = p + vstart
        j = bisect.bisect_left(origin, t)
        candidates = [k for k in (j - 1, j) if 0 <= k < len(origin)]
        k = min(candidates, key=lambda n: abs(origin[n] - t)) if candidates else 0
        mapping.append({'working_frame': i, 'working_pts': p, 'original_nearest_frame': k,
                        'original_pts': origin[k] if origin else None})
    record = {'source': str(Path(source).resolve()), 'working_source': str(Path(output).resolve()),
              'fps': str(target), 'original_fps': before['fps'], 'original_was_cfr': before['cfr'],
              'frame_count': after['frame_count'], 'coordinate_system': 'normalized working_source decoded frames [in,out)',
              'mapping_note': 'Nearest original timestamp is an audit correspondence; ffmpeg fps may duplicate/drop frames.',
              'original_unchanged': True, 'mapping': mapping}
    save(str(output) + '.normalization.json', record)
    return {k: v for k, v in record.items() if k != 'mapping'}


def font_path():
    choices = [os.environ.get('TIMECODE_FONT'), '/System/Library/Fonts/Supplemental/Courier New Bold.ttf',
               '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
               '/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf',
               'C:/Windows/Fonts/consolab.ttf']
    return next((str(Path(p)) for p in choices if p and Path(p).is_file()), None)


def filter_quote(value):
    return str(value).replace('\\', '\\\\').replace(':', '\\:').replace("'", "'\\''")


def burn_timecode(source, output, fps=None):
    protect_output(source, output)
    info = probe(source, True)
    require_cfr(info)
    actual = ratio(info['fps'])
    if fps and ratio(fps) != actual:
        raise ValueError('--fps is an assertion, not a rate conversion; normalize first')
    nominal = rounded(actual)
    folder = Path(output).parent / (Path(output).stem + '_timecode_work')
    folder.mkdir(exist_ok=True)
    textfile = folder / 'timecode.txt'
    textfile.write_text(f'[%{{eif:trunc(n/{nominal*3600}):d:2}}:%{{eif:mod(trunc(n/{nominal*60}),60):d:2}}:%{{eif:mod(trunc(n/{nominal}),60):d:2}}:%{{eif:mod(n,{nominal}):d:2}}]')
    w, h = info['video']['width'], info['video']['height']
    size = max(12, min(round(h * .034), math.floor(w / 10.5)))
    margin = max(4, round(w * .022))
    pad = max(4, round(size * .35))
    font = font_path()
    prefix = f"fontfile='{filter_quote(font)}':" if font else 'font=monospace:'
    vf = f"drawtext={prefix}textfile='{filter_quote(textfile.resolve())}':fontsize={size}:fontcolor=black:box=1:boxcolor=white:boxborderw={pad}:x={margin+pad}:y={margin+pad}"
    ff(['-y', '-reinit_filter', '0', '-i', source, '-map', '0:v:0', '-map', '0:a:0?', '-vf', vf,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-c:a', 'copy',
        '-frames:v', info['frame_count'], '-fps_mode', 'passthrough', '-movflags', '+faststart', output])
    verified = verify(output, info['frame_count'], str(actual))
    record = {'source': str(Path(source).resolve()), 'timecoded_video': str(Path(output).resolve()),
              'fps': str(actual), 'nominal_timecode_fps': nominal, 'timecode_mode': 'NDF frame count',
              'fractional_rate_notice': ('HH:MM:SS is nominal timecode, not elapsed wall-clock time. Use frame indices and exact fps for editing.' if actual.denominator != 1 else None),
              'first_timecode': frame_to_tc(0, actual), 'last_timecode': frame_to_tc(info['frame_count']-1, actual),
              'frame_count': info['frame_count'], 'verification': verified}
    save(str(output) + '.timecode.json', record)
    return record


def extract(source, directory, start, end, step=1):
    info = probe(source, True)
    require_cfr(info)
    if not (0 <= start < end <= info['frame_count']) or step < 1:
        raise ValueError('Invalid [start,end) frame range or step')
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    pattern = dest / '.extract_%09d.png'
    vf = f'select=gte(n\\,{start})*lt(n\\,{end})*not(mod(n-{start}\\,{step}))'
    ff(['-y', '-reinit_filter', '0', '-i', source, '-vf', vf, '-fps_mode', 'passthrough', '-start_number', '0', pattern])
    frames = list(range(start, end, step))
    rows = []
    for i, f in enumerate(frames):
        tmp = dest / f'.extract_{i:09d}.png'
        if not tmp.exists():
            raise RuntimeError(f'Missing extracted source frame {f}')
        target = dest / f'F{f:09d}.png'
        oscompat.replace(tmp, target)
        rows.append({'source_frame': f, 'pts': info['frame_pts'][f],
                     'timecode': frame_to_tc(f, info['fps']), 'path': str(target.resolve())})
    record = {'source': str(Path(source).resolve()), 'fps': info['fps'], 'range': [start, end],
              'step': step, 'boundary_convention': '[in,out)', 'frames': rows}
    save(dest / 'frames.json', record)
    return {'frames_json': str((dest / 'frames.json').resolve()), 'count': len(rows)}


def scenes(source, threshold=.3):
    info = probe(source, True)
    require_cfr(info)
    if not 0 < threshold < 1:
        raise ValueError('Scene threshold must be between 0 and 1')
    p = subprocess.run([binary('ffmpeg'), '-hide_banner', '-loglevel', 'info', '-nostdin', '-reinit_filter', '0', '-i', str(source),
        '-vf', f'select=gt(scene\\,{threshold}),showinfo', '-an', '-f', 'null', '-'], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[-4000:])
    pts = [float(x) for x in re.findall(r'pts_time:([0-9.+eE-]+)', p.stderr)]
    return {'source': str(Path(source).resolve()), 'fps': info['fps'], 'threshold': threshold,
            'purpose': 'Candidate transitions only; inspect adjacent decoded frames to confirm hard cuts.',
            'candidates': [{'source_frame': rounded(seconds(p) * ratio(info['fps'])), 'pts': p,
                            'timecode': frame_to_tc(rounded(seconds(p)*ratio(info['fps'])), info['fps'])} for p in pts]}


def file_identity(path):
    p = Path(path).resolve()
    st = p.stat()
    return {'path': str(p), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def json_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def hashed_identity(path):
    return {**file_identity(path), 'sha256': file_sha256(path)}


STILL_SIZE = (180, 320)       # frame differences are measured on grey frames of this size (0-255)
STILL_MOTION = 0.3            # mean |frame - previous frame| over STILL_WINDOW_S below this = a still picture
STILL_WINDOW_S = 0.25


def still_start_index(diffs, fps, min_s=0.25, window_s=STILL_WINDOW_S, motion=STILL_MOTION):
    """First frame of the still stretch that runs to the last frame, or None. `diffs[i]` is the mean absolute grey
    difference between frame i-1 and frame i (diffs[0] is not used). A window of `window_s` starting at a frame whose
    mean difference stays under `motion` all the way to the end counts as still: this catches a picture that drifts a
    little while it holds (2026-11-23 film 2114 G03: two redo shots held 1.4 s and 1.7 s with frame differences of
    0.03-0.25 against 0.3-2.4 while the coach talked; freezedetect compares with the first still frame, the drift
    crossed its tolerance, and only the exact duplicates at the very end were cut - the user saw a freeze at each join)."""
    n = len(diffs); w = max(2, round(window_s * fps))
    if n < w + 1: return None
    start = None
    for i in range(n - 1, 0, -1):
        win = diffs[i:min(n, i + w)]
        if sum(win) / len(win) < motion: start = i
        else: break
    if start is None or (n - start) / fps < min_s: return None
    return start


def frame_diffs(path, start_s, size=STILL_SIZE):
    """Mean absolute grey differences between consecutive frames from `start_s` to the end, or None."""
    try:
        import numpy as np
    except ImportError:
        return None
    w, h = size
    r = subprocess.run([binary('ffmpeg'), '-hide_banner', '-v', 'error', '-ss', f'{start_s:.3f}', '-i', str(path), '-map', '0:v:0',
                        '-vf', f'scale={w}:{h},format=gray', '-f', 'rawvideo', '-'], capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 2 * w * h: return None
    frames = np.frombuffer(r.stdout[:len(r.stdout) // (w * h) * w * h], np.uint8).reshape(-1, h, w).astype(np.float32)
    return [0.0] + [float(x) for x in np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))]


def frozen_tail(path, min_s=0.25, look_s=4.0, noise=0.002):
    """Seconds where a still picture that runs to the end of the clip starts, or None. A generated clip copies the held
    last frame its padded reference ended on (2026-11-22: 16 of 17 delivered films had 0.3-2 s of still picture at the
    end of a generated segment, half of them with the voice still talking). A still picture that drifts slowly counts
    too (see still_start_index); freezedetect is only the fallback when the frames cannot be read."""
    try:
        info = probe(str(path)); dur = float(info.get('video_duration') or info['duration'])
        fps = float(Fraction(str(info['fps'])))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OSError, subprocess.SubprocessError):
        return None
    start = max(0.0, dur - look_s)
    old = _freezedetect_tail(path, dur, start, min_s, noise)
    diffs = frame_diffs(path, start)
    s = None if diffs is None else still_start_index(diffs, fps, min_s)
    new = None if s is None else round(max(start, dur - (len(diffs) - s) / fps), 3)
    # the earlier of the two: drift is only seen by the differences, a still stretch that a few last frames of another
    # picture end (2026-11-22 film 1585 G10) only by freezedetect
    found = [x for x in (old, new) if x is not None]
    return min(found) if found else None


def _freezedetect_tail(path, dur, start, min_s, noise):
    r = subprocess.run([binary('ffmpeg'), '-hide_banner', '-v', 'info', '-ss', f'{start:.3f}', '-i', str(path), '-map', '0:v:0',
                        '-vf', f'freezedetect=n={noise}:d={min_s}', '-f', 'null', '-'], capture_output=True, text=True)
    import re as _re
    starts = [float(x) for x in _re.findall(r'freeze_start: ([0-9.]+)', r.stderr)]
    ends = [float(x) for x in _re.findall(r'freeze_end: ([0-9.]+)', r.stderr)]
    if not starts: return None
    # the last still stretch ends before the clip does; freezedetect often reports the end a frame or two before the last
    # frame (2026-11-22 film 998 G13: still 6.25-6.92 s of a 7.00 s clip was missed with 0.08 s)
    if len(ends) >= len(starts) and ends[-1] < (dur - start) - 0.2: return None
    return round(start + starts[-1], 3)


def freeze(manifest_path, output, review_records):
    """Seal selected media and upstream reviews; rendering makes no new semantic choices."""
    if not review_records:
        raise ValueError('At least one upstream generated_index/speech_review JSON record is required')
    frozen_plan = plan(manifest_path)
    records = []
    for path in review_records:
        record = json.loads(Path(path).read_text())
        if not isinstance(record, (dict, list)) or not record:
            raise ValueError('Review records must be nonempty JSON objects or lists')
        records.append(hashed_identity(path))
    document = {
        'schema_version': 1, 'type': 'lymphloop-frozen-edit-plan',
        'manifest': hashed_identity(manifest_path),
        'renderer_implementation_sha256': file_sha256(__file__),
        'inputs': [hashed_identity(item['path']) for item in frozen_plan['inputs']],
        'review_records': records, 'plan': frozen_plan, 'plan_sha256': json_sha256(frozen_plan),
    }
    for item in [document['manifest'], *document['inputs'], *records]:
        protect_output(item['path'], output)
    document['seal_sha256'] = json_sha256(document)
    save(output, document)
    return document


def verify_frozen_plan(frozen_path, manifest_path=None):
    """Validate a frozen edit plan by hashes only, without model calls or repeated ASR."""
    document = json.loads(Path(frozen_path).read_text())
    if document.get('type') != 'lymphloop-frozen-edit-plan' or document.get('schema_version') != 1:
        raise ValueError('Unsupported frozen edit plan')
    seal = document.get('seal_sha256')
    if seal != json_sha256({key: value for key, value in document.items() if key != 'seal_sha256'}):
        raise ValueError('Frozen edit plan seal mismatch; return to the selection/freeze step')
    if document.get('plan_sha256') != json_sha256(document['plan']):
        raise ValueError('Frozen timeline content changed')
    if manifest_path and str(Path(manifest_path).resolve()) != document['manifest']['path']:
        raise ValueError('Frozen edit plan belongs to another manifest')
    if document['renderer_implementation_sha256'] != file_sha256(__file__):
        raise ValueError('Renderer implementation changed; explicitly re-freeze before rendering')
    if not document.get('review_records'):
        raise ValueError('Frozen edit plan has no upstream review records')
    if {item['path'] for item in document['inputs']} != {item['path'] for item in document['plan']['inputs']}:
        raise ValueError('Frozen input coverage differs from timeline inputs')
    for item in [document['manifest'], *document['inputs'], *document['review_records']]:
        if not Path(item['path']).is_file() or file_sha256(item['path']) != item['sha256']:
            raise ValueError(f'Frozen dependency changed: {item["path"]}; return to its upstream stage')
    return document


def resolve_path(base, value):
    p = Path(value).expanduser()
    return str((base / p).resolve()) if not p.is_absolute() else str(p.resolve())


def validate_non_speaking_tail(item, base):
    """Bind a reviewed quiet tail to this exact native Dialogue and whole-speech review."""
    reference = item.get('non_speaking_tail_review')
    if not isinstance(reference, dict):
        raise ValueError('VO overlaps Dialogue picture without a verified non-speaking tail')
    dependencies = []
    def checked(ref):
        if not isinstance(ref, dict) or not ref.get('path') or not ref.get('sha256'):
            raise ValueError('Non-speaking tail needs hashed review/evidence references')
        path = resolve_path(base, ref['path'])
        if file_sha256(path) != ref['sha256']:
            raise ValueError('Non-speaking tail review/evidence hash mismatch')
        dependencies.append(path)
        return path
    proof = json.loads(Path(checked(reference)).read_text())
    if proof.get('type') != 'lymphloop_non_speaking_tail_review' or proof.get('task_id') != item['id'] or proof.get('media_sha256') != file_sha256(item['path']):
        raise ValueError('Non-speaking tail is bound to different selected media/task')
    if proof.get('no_speaking_lips') is not True or proof.get('no_dialogue_audio') is not True or not proof.get('reviewer') or not proof.get('review_note') or not proof.get('visual_evidence'):
        raise ValueError('Actual quiet-tail frame and audio review is required')
    for ref in proof['visual_evidence']:
        checked(ref)
    speech = json.loads(Path(checked(proof.get('speech_review'))).read_text())
    reviews = [r for r in speech.get('items', []) if r.get('task_id') == item['id']]
    if len(reviews) != 1 or reviews[0].get('complete') is not True or reviews[0].get('media_sha256') != proof['media_sha256']:
        raise ValueError('Non-speaking tail needs the exact complete native speech review')
    review = reviews[0]
    utterance = review.get('utterance_range', [])
    tail_source = proof.get('source_tail_start')
    tail_clip = seconds(proof.get('clip_tail_start', -1))
    clip_in, clip_out = seconds(item['clip_in']), seconds(item['clip_out'])
    if len(utterance) != 2 or not clip_in <= seconds(utterance[0]) < seconds(utterance[1]) <= tail_clip < clip_out:
        raise ValueError('Non-speaking tail begins before the complete reviewed utterance ends')
    if type(tail_source) is not int or not item['source_in'] < tail_source < item['source_out']:
        raise ValueError('Invalid non-speaking source tail boundary')
    mapped = clip_in + Fraction(tail_source-item['source_in'], item['source_out']-item['source_in'])*(clip_out-clip_in)
    if mapped < tail_clip:
        raise ValueError('Source tail boundary maps before the verified quiet generated tail')
    return proof, dependencies


def nonoverlap(items, label):
    ordered = sorted(items, key=lambda x: (x['source_in'], x['source_out']))
    for a, b in zip(ordered, ordered[1:]):
        if a['source_out'] > b['source_in']:
            raise ValueError(f'Overlapping {label}: {a["id"]} and {b["id"]}; consolidate explicitly')


def plan(manifest_path):
    mp = Path(manifest_path).resolve()
    manifest = json.loads(mp.read_text())
    if manifest.get('schema_version', 1) != 1:
        raise ValueError('Unsupported schema_version')
    base = mp.parent
    source = resolve_path(base, manifest['source'])
    source_info = probe(source, True)
    require_cfr(source_info)
    sfps = ratio(source_info['fps'])
    if 'fps' in manifest and ratio(manifest['fps']) != sfps:
        raise ValueError('Manifest fps differs from source fps')
    ofps = ratio(manifest.get('output_fps', str(sfps)))
    count = source_info['frame_count']
    width = int(manifest.get('width', source_info['video']['width']))
    height = int(manifest.get('height', source_info['video']['height']))
    if width % 2 or height % 2 or min(width, height) < 2:
        raise ValueError('Output width and height must be positive even numbers')
    maximum = float(manifest.get('max_speed', 1.2))
    if not 1 <= maximum <= 1.2:
        raise ValueError('max_speed must be between 1 and 1.2')
    ripple = manifest.get('timing') == 'ripple'
    pictures = [dict(x) for x in manifest.get('picture_replacements', [])]
    audios = [dict(x) for x in manifest.get('audio_replacements', [])]
    seen_ids = set()
    picture_speed_exceptions = {}
    speed_exception_dependencies = set()
    cache = {source: source_info}
    for item in pictures + audios:
        if not item.get('id') or item['id'] in seen_ids:
            raise ValueError('Each picture/audio entry requires a globally unique id')
        seen_ids.add(item['id'])
        a, b = item['source_in'], item['source_out']
        if not isinstance(a, int) or not isinstance(b, int) or not 0 <= a < b <= count:
            raise ValueError(f'{item["id"]}: invalid original-source frame range')
        if not item.get('evidence'):
            raise ValueError(f'{item["id"]}: local frame/audio evidence is required')
        item['path'] = resolve_path(base, item['path'])
        cache.setdefault(item['path'], None)
        if cache[item['path']] is None:
            cache[item['path']] = probe(item['path'])
        info = cache[item['path']]
        is_picture = item in pictures
        if is_picture:
            if item.get('kind') not in ('Visual', 'Dialogue', 'Ingredient') or not info['video']:
                raise ValueError(f'{item["id"]}: invalid picture kind or missing video')
            available = stream_duration_exact(info, 'video')
        else:
            if item.get('kind') not in ('VO', 'Dialogue') or not info['audio']:
                raise ValueError(f'{item["id"]}: invalid audio kind or missing audio')
            available = stream_duration_exact(info, 'audio')
        start = seconds(item.get('clip_in', 0))
        end = seconds(item.get('clip_out', available))
        if is_picture:
            start, end = (snap_near_grid(x, ratio(info['fps'])) for x in (start, end))
        else:
            sample_rate = int(info['audio'].get('sample_rate', SR))
            start, end = (Fraction(rounded(x*sample_rate), sample_rate) for x in (start, end))
        if not 0 <= start < end <= available + Fraction(1, 10000):
            raise ValueError(f'{item["id"]}: selected clip range exceeds available media')
        selected = end-start
        item.update(clip_in=float(start), clip_out=float(end), selected_seconds=float(selected), selected_seconds_exact=str(selected))
        if item.get('picture_speed_exception'):
            if not is_picture:
                raise ValueError('Picture speed exceptions cannot alter audio speed')
            import cg_speed_exception
            cap, dependencies = cg_speed_exception.validate(item['picture_speed_exception'], base,
                item['id'], item['kind'], [a,b], item['path'], float(start), float(end))
            picture_speed_exceptions[item['id']] = cap
            speed_exception_dependencies.update(str(p) for p in dependencies)
        if is_picture:
            base_duration = selected if ripple or item['kind'] == 'Dialogue' else Fraction(b-a)/sfps
            item['base_seconds'] = float(base_duration)
            item['base_seconds_exact'] = str(base_duration)
            # Enforce the cap on actual rendered atoms after complete VO has
            # stretched its covered picture window. A longer VO can make this
            # selected action fit naturally without any excessive speed-up.
        else:
            speed = float(item.get('speed', 1))
            if not 0 < speed <= maximum:
                raise ValueError(f'{item["id"]}: invalid audio speed; default is 1, maximum {maximum}')
            if item['kind'] == 'Dialogue' and abs(speed - 1) > 1e-9:
                raise ValueError(f'{item["id"]}: independent Dialogue audio speed is forbidden; preprocess picture and audio together, then pass the synchronized asset at speed 1')
            if ripple and (speed != 1 or selected > Fraction(b-a)/sfps + Fraction(1, sample_rate)):
                raise ValueError(f'{item["id"]}: audio-only recut must fit the source picture at original speed; use a reviewed picture-and-audio clip for a longer line')
            natural = selected/seconds(speed)
            item.update(speed=speed, natural_seconds=float(natural), natural_seconds_exact=str(natural))
            if item.get('background_path'):
                item['background_path'] = resolve_path(base, item['background_path'])
                if not item.get('background_isolated', False):
                    raise ValueError(f'{item["id"]}: background_isolated=true is required; do not mix original speech back in')
    nonoverlap(pictures, 'picture replacements')
    tail_dependencies = set()
    internal_partial_audio = set()
    # Ripple edits take the selected picture and its own soundtrack as a single unit.
    for item in pictures:
        if item['kind'] != 'Dialogue' and not ripple:
            continue
        overlaps = [x for x in audios if x['source_in'] < item['source_out'] and x['source_out'] > item['source_in']]
        if ripple and overlaps:
            raise ValueError(f'{item["id"]}: ripple pictures use their own soundtrack; review and mux any off-screen audio into the clip before selecting it')
        if overlaps:
            if any(x['kind'] == 'VO' for x in overlaps):
                proof, dependencies = validate_non_speaking_tail(item, base)
                tail_dependencies.update(dependencies)
                if any(x['kind'] != 'VO' or max(x['source_in'], item['source_in']) < proof['source_tail_start'] for x in overlaps):
                    raise ValueError(f'{item["id"]}: VO overlaps speaking Dialogue instead of its verified quiet tail')
                if not cache[item['path']]['audio']:
                    raise ValueError('Reviewed Dialogue quiet tail requires native generated audio')
                nonoverlap(overlaps, 'VO quiet-tail windows')
                cursor = item['source_in']
                gaps = []
                for audio in sorted(overlaps, key=lambda x: x['source_in']):
                    a, b = max(audio['source_in'], item['source_in']), min(audio['source_out'], item['source_out'])
                    if cursor < a:
                        gaps.append((cursor, a))
                    cursor = b
                if cursor < item['source_out']:
                    gaps.append((cursor, item['source_out']))
                for number, (a, b) in enumerate(gaps):
                    automatic_id = item['id']+'_native_'+str(number)
                    if automatic_id in seen_ids:
                        raise ValueError('Automatic Dialogue audio id collides with an input id')
                    seen_ids.add(automatic_id)
                    internal_partial_audio.add(automatic_id)
                    selected = seconds(item['selected_seconds_exact'])
                    start = seconds(item['clip_in']) + Fraction(a-item['source_in'], item['source_out']-item['source_in'])*selected
                    duration = Fraction(b-a, item['source_out']-item['source_in'])*selected
                    audios.append({'id': automatic_id, 'kind': 'Dialogue', 'source_in': a, 'source_out': b,
                        'path': item['path'], 'clip_in': float(start), 'clip_out': float(start+duration),
                        'selected_seconds': float(duration), 'speed': 1, 'natural_seconds': float(duration),
                        'natural_seconds_exact': str(duration), 'evidence': item['evidence'],
                        'automatic_dialogue_audio': True, 'parent_picture_id': item['id']})
                continue
            if len(overlaps) != 1 or overlaps[0]['kind'] != 'Dialogue' or (overlaps[0]['source_in'], overlaps[0]['source_out']) != (item['source_in'], item['source_out']):
                raise ValueError(f'{item["id"]}: Dialogue audio must cover exactly its complete source window')
            allowed_delta = max(Fraction(1)/ratio(cache[item['path']]['fps']), Fraction(1, 40))
            if abs(seconds(overlaps[0]['natural_seconds_exact']) - seconds(item['selected_seconds_exact'])) > allowed_delta:
                raise ValueError(f'{item["id"]}: external Dialogue audio duration differs from selected picture; preprocess synchronized audio and picture together')
        else:
            if not cache[item['path']]['audio']:
                if ripple and item['kind'] != 'Dialogue':
                    continue
                raise ValueError(f'{item["id"]}: Dialogue picture needs an explicit audio replacement or its own audio')
            automatic_id = item['id']+'_audio'
            if automatic_id in seen_ids:
                raise ValueError(f'Automatic Dialogue audio id {automatic_id} collides with an input id; rename the input id')
            seen_ids.add(automatic_id)
            audios.append({'id': automatic_id, 'kind': 'Dialogue', 'source_in': item['source_in'],
                'source_out': item['source_out'], 'path': item['path'], 'clip_in': item['clip_in'],
                'clip_out': item['clip_out'], 'selected_seconds': item['selected_seconds'], 'speed': 1,
                'natural_seconds': item['selected_seconds'], 'natural_seconds_exact': item['selected_seconds_exact'],
                'evidence': item['evidence'], 'automatic_dialogue_audio': True})
    for audio in audios:
        if audio['kind'] == 'Dialogue' and audio['id'] not in internal_partial_audio and not any((ripple or x['kind'] == 'Dialogue') and (x['source_in'], x['source_out']) == (audio['source_in'], audio['source_out']) for x in pictures):
            raise ValueError(f'{audio["id"]}: Dialogue audio requires a matching complete Dialogue picture replacement')
    nonoverlap(audios, 'audio replacements')
    bounds = sorted({0, count} | {x[k] for x in pictures+audios for k in ('source_in', 'source_out')})
    atoms = []
    for a, b in zip(bounds, bounds[1:]):
        pic = next((x for x in pictures if x['source_in'] <= a and b <= x['source_out']), None)
        aud = next((x for x in audios if x['source_in'] <= a and b <= x['source_out']), None)
        if pic:
            fraction = Fraction(b-a, pic['source_out']-pic['source_in'])
            base_sec = seconds(pic['base_seconds_exact']) * fraction
            clip_start = pic['clip_in'] + (a-pic['source_in'])/(pic['source_out']-pic['source_in'])*pic['selected_seconds']
            clip_end = pic['clip_in'] + (b-pic['source_in'])/(pic['source_out']-pic['source_in'])*pic['selected_seconds']
        else:
            base_sec = Fraction(b-a, 1)/sfps
            clip_start, clip_end = float(Fraction(a)/sfps), float(Fraction(b)/sfps)
        atoms.append({'source_in': a, 'source_out': b, 'picture_id': pic['id'] if pic else None,
            'kind': pic['kind'] if pic else 'Original', 'path': pic['path'] if pic else source,
            'clip_in': clip_start, 'clip_out': clip_end, 'base_seconds': float(base_sec),
            '_duration': base_sec, 'audio_replacement_id': aud['id'] if aud else None})
    for aud in audios:
        if ripple:
            aud['picture_stretch'] = 1.0
            continue
        covered = [x for x in atoms if x['audio_replacement_id'] == aud['id']]
        base_duration = sum((x['_duration'] for x in covered), Fraction())
        # Complete speech must fit, including the last fractional frame of audio.
        needed = max(math.ceil(base_duration * ofps), math.ceil(seconds(aud['natural_seconds_exact']) * ofps))
        stretch = (Fraction(needed) / ofps) / base_duration
        for atom in covered:
            atom['_duration'] *= stretch
        aud['picture_stretch'] = float(stretch)
    cursor, elapsed = 0, Fraction()
    for i, atom in enumerate(atoms):
        natural = atom.pop('_duration')
        elapsed += natural
        # Keep the entire selected clip on the output frame grid, including its last partial frame.
        finish = cursor + math.ceil(natural * ofps) if ripple else rounded(elapsed * ofps)
        if finish <= cursor:
            raise ValueError('A mapped interval is shorter than one output frame; consolidate its boundaries')
        duration = Fraction(finish-cursor)/ofps
        atom.update(index=i, out_in=cursor, out_out=finish, frames=finish-cursor,
                    output_seconds=float(duration), picture_speed=1.0 if ripple else (atom['clip_out']-atom['clip_in'])/float(duration),
                    source_audio_speed=1.0 if ripple else ((atom['source_out']-atom['source_in'])/float(sfps))/float(duration),
                    audio_samples=rounded(Fraction(finish)*SR/ofps)-rounded(Fraction(cursor)*SR/ofps))
        # One-frame quantization tolerance is allowed; selected clip material is never cut silently.
        cap = picture_speed_exceptions.get(atom['picture_id'], maximum)
        if atom['picture_speed'] > cap + float(ofps)/max(atom['frames'], 1)*.001:
            raise ValueError(f'{atom["picture_id"]}: rendered picture speed exceeds max_speed; inspect action then set explicit clip_in/clip_out')
        if atom['picture_id'] in picture_speed_exceptions:
            if abs(atom['source_audio_speed']-1)>1e-6 or atom['audio_replacement_id']:
                raise ValueError('CG speed exception must retain unmodified original audio and timing')
            atom['user_authorized_picture_speed_cap'] = cap
        cursor = finish
    for aud in audios:
        covered = [x for x in atoms if x['audio_replacement_id'] == aud['id']]
        aud['out_in'] = covered[0]['out_in']
        aud['out_out'] = covered[-1]['out_out']
        aud['start_sample'] = rounded(Fraction(aud['out_in'])*SR/ofps)
        aud['end_sample'] = rounded(Fraction(aud['out_out'])*SR/ofps)
        aud['audio_policy'] = 'Old source audio muted for entire window; new complete speech plus optional isolated background only.'
    answer = {'schema_version': 1, 'manifest': str(mp), 'source': source, 'source_fps': str(sfps),
              'source_frames': count, 'source_has_audio': bool(source_info['audio']), 'output_fps': str(ofps),
              'width': width, 'height': height, 'output_frames': cursor, 'output_seconds': float(Fraction(cursor)/ofps),
              'output_samples': rounded(Fraction(cursor)*SR/ofps), 'sample_rate': SR, 'max_speed': maximum,
              'boundary_convention': '[in,out)', 'segments': atoms, 'audio_replacements': audios,
              'inputs': [file_identity(p) for p in sorted({source} | {x['path'] for x in pictures+audios} |
                         {x['background_path'] for x in audios if x.get('background_path')} | tail_dependencies | speed_exception_dependencies)],
              'no_added_subtitles': True, 'background_policy': 'Within replaced speech windows original mix is muted unless a separately isolated bed is supplied.'}
    if ripple:
        answer['timing'] = 'ripple'
    return answer


def atempo(value):
    values = []
    while value < .5:
        values.append(.5); value /= .5
    while value > 2:
        values.append(2); value /= 2
    values.append(value)
    return ','.join(f'atempo={x:.12f}' for x in values)


def concat_path(path):
    return "file '" + str(Path(path).resolve()).replace("'", "'\\''") + "'"


def decoded_audio_samples(path, sample_rate=SR):
    """Count decoded PCM frames without loading the whole soundtrack into memory."""
    command = [binary('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-i', str(path),
               '-map', '0:a:0', '-vn', '-ar', str(sample_rate), '-ac', '2',
               '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1']
    count = 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        while True:
            block = process.stdout.read(1024 * 1024)
            if not block:
                break
            count += len(block)
        process.stdout.close()
        code = process.wait()
        if code:
            errors.seek(0)
            raise RuntimeError('Audio decode failed: ' + errors.read().decode(errors='replace')[-4000:])
    if count % 4:
        raise ValueError('Decoded stereo PCM ends on an incomplete sample')
    return count // 4


def verify_audio(path, expected_samples=None, sample_rate=SR, info=None):
    info = info or probe(path)
    audio = info.get('audio')
    if not audio:
        if expected_samples is not None:
            raise ValueError('Expected complete audio track, but output has no audio stream')
        return {'has_audio': False, 'expected_samples': None, 'checked': False}
    count = decoded_audio_samples(path, sample_rate)
    native_rate = int(audio.get('sample_rate', sample_rate))
    codec = audio.get('codec_name', '')
    codec_block = {'aac': 1024, 'mp3': 1152, 'opus': 960}.get(codec, 0)
    # AAC decoding may expose up to one padded codec frame beyond the declared duration.
    padding = math.ceil(codec_block * sample_rate / native_rate) + 2
    start = float(audio.get('start_time', 0))
    declared_duration = audio.get('duration')
    declared_end = start + float(declared_duration) if declared_duration is not None else None
    target = int(expected_samples) if expected_samples is not None else None
    if target is not None:
        if target < 0:
            raise ValueError('expected_audio_samples must be nonnegative')
        if abs(count-target) > padding:
            raise ValueError(f'Audio sample count mismatch: decoded {count}, expected {target}, codec tolerance {padding}')
        target_seconds = target / sample_rate
        # A codec padding allowance must not conceal an actually truncated declared tail.
        metadata_rounding = max(.001, 2/sample_rate)
        if abs(start) > metadata_rounding:
            raise ValueError(f'Audio starts at {start:.6f}s instead of the planned zero point')
        if declared_end is not None and declared_end < target_seconds-metadata_rounding:
            raise ValueError(f'Audio tail is short: declared end {declared_end:.6f}s, planned {target_seconds:.6f}s')
        if declared_end is not None and declared_end > target_seconds+padding/sample_rate+metadata_rounding:
            raise ValueError(f'Audio tail exceeds planned duration: {declared_end:.6f}s versus {target_seconds:.6f}s')
    return {'has_audio': True, 'checked': True, 'sample_rate_checked': sample_rate,
            'decoded_samples': count, 'expected_samples': target, 'codec_padding_tolerance_samples': padding,
            'start_seconds': start, 'declared_end_seconds': declared_end,
            'planned_tail_present': True if target is not None else None,
            'note': 'Checks decoded length and declared tail timing, not semantic completeness of spoken words.'}


def verify(path, expected_frames=None, fps=None, expected_audio_samples=None, audio_sample_rate=SR,
           require_audio=False):
    info = probe(path, True)
    require_cfr(info)
    if expected_frames is not None and info['frame_count'] != int(expected_frames):
        raise ValueError(f'Frame count mismatch: {info["frame_count"]} != {expected_frames}')
    if fps and ratio(info['fps']) != ratio(fps):
        raise ValueError('Output fps mismatch')
    if require_audio and expected_audio_samples is None:
        expected_audio_samples = rounded(Fraction(info['frame_count'])*audio_sample_rate/ratio(info['fps']))
    audio_check = verify_audio(path, expected_audio_samples, audio_sample_rate, info)
    ff(['-i', path, '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'])
    return {'path': str(Path(path).resolve()), 'frame_count': info['frame_count'], 'fps': info['fps'],
            'video_seconds': info['video_duration'], 'container_seconds': info['format_duration'],
            'width': info['video']['width'], 'height': info['video']['height'], 'has_audio': bool(info['audio']),
            'audio_verification': audio_check, 'full_decode_passed': True, 'cfr': True,
            'limitation': 'Technical verification only. Semantic product/action quality and utterance completeness require separate review evidence.'}


# ------------------------------------------------------------------ per-atom render cache
# User 2026-11-22 ("顺剪有个问题，只要中间有一个镜头穿帮，回剪之后所有的镜头流程都要从头来过" -> "改，按内容缓存"): every
# atom (a stretch of original film or one generated picture) is encoded on its own and keyed by WHAT it is -- the file's
# bytes, the frames or seconds taken from it, the output grid -- never by where it lands in the film. A goof redo that
# changes one segment's length re-encodes that segment only; everything after it is the same atom at a new position,
# joined again by stream copy. All atoms use x264 stitchable=1 so pieces encoded at different times (and brand's
# re-stamped atoms at another crf) carry identical stream headers and concatenate cleanly.
X264_STITCH = ['-x264-params', 'stitchable=1']
_render_started = 0.0


def _ident(path, cache):
    path = str(Path(path).resolve())
    if path not in cache: cache[path] = file_sha256(path)
    return cache[path]


def atom_key(atom, p, idents):
    fields = {k: atom.get(k) for k in ('kind', 'source_in', 'source_out', 'clip_in', 'clip_out', 'frames', 'output_seconds',
                                       'audio_samples', 'source_audio_speed')}
    fields.update(picture=atom.get('picture_id') is not None, audio_replaced=atom.get('audio_replacement_id') is not None,
                  path=_ident(atom['path'], idents), source=_ident(p['source'], idents) if p.get('source_has_audio') else None,
                  source_fps=str(p['source_fps']), output_fps=str(p['output_fps']), width=p['width'], height=p['height'],
                  source_has_audio=bool(p.get('source_has_audio')), timing=p.get('timing'), sr=SR,
                  code=hashlib.sha256(inspect.getsource(render_atom).encode()).hexdigest())
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def render_atom(atom, p, fps, ripple, video, audio):
    tmpv, tmpa = video.with_suffix('.part.mp4'), audio.with_suffix('.part.wav')
    if atom['kind'] == 'Original':
        trim = f'trim=start_frame={atom["source_in"]}:end_frame={atom["source_out"]}'
        timing = f'{trim},setpts=(PTS-STARTPTS)'
    else:
        # Select on the output sampling grid. An audio boundary can split
        # a 30fps atom between two 24fps input timestamps; trimming input
        # first would then produce no frames. Shifting the full input and
        # fps(start_time=0) retains the normal neighboring-frame sample.
        timing = f'setpts=(PTS-({atom["clip_in"]:.12f})/TB)'
    factor = 1.0 if ripple else atom['output_seconds']/(atom['clip_out']-atom['clip_in'])
    # tpad covers only timestamp quantization at the final frame, never missing action footage.
    vf = (f'{timing}*{factor:.12f},fps={fps}:start_time=0,'
          f'tpad=stop_mode=clone:stop_duration={2/float(fps):.12f},'
          f'trim=end_frame={atom["frames"]},scale={p["width"]}:{p["height"]}:force_original_aspect_ratio=decrease,'
          f'pad={p["width"]}:{p["height"]}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p')
    ff(['-y', '-reinit_filter', '0', '-i', atom['path'], '-vf', vf, '-an', '-frames:v', atom['frames'], '-r', str(fps),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', *X264_STITCH, '-threads', '2', '-video_track_timescale',
        fps.numerator, '-movflags', '+faststart', '-f', 'mp4', tmpv])
    if probe(tmpv)['frame_count'] != atom['frames']:
        raise RuntimeError(f'Segment {atom["index"]:04d} produced wrong frame count')
    if atom['audio_replacement_id'] or not p['source_has_audio'] or (ripple and atom['picture_id']):
        cmd = ['-f', 'lavfi', '-i', f'anullsrc=r={SR}:cl=stereo']
        af = f'atrim=end_sample={atom["audio_samples"]},asetpts=PTS-STARTPTS'
    else:
        cmd = ['-i', p['source']]
        a = atom['source_in']/float(ratio(p['source_fps']))
        b = atom['source_out']/float(ratio(p['source_fps']))
        af = (f'aresample={SR}:async=1:first_pts=0,atrim=start={a:.12f}:end={b:.12f},asetpts=PTS-STARTPTS,'
              f'{"" if ripple else atempo(atom["source_audio_speed"]) + ","}aresample={SR},apad,'
              f'atrim=end_sample={atom["audio_samples"]}')
    ff(['-y', *cmd, '-vn', '-af', af, '-ar', SR, '-ac', '2', '-c:a', 'pcm_s16le', '-f', 'wav', tmpa])
    tmpv.replace(video); tmpa.replace(audio)


def prune_atoms(w, keys, keep_renders=3):
    """Keep the atoms of the last few renders (so going back to an earlier selection is free too); drop the rest and the
    index-named pieces of the old whole-plan cache."""
    used = w/'atoms'/'renders.json'
    hist = json.loads(used.read_text()) if used.is_file() else []
    hist = (hist + [sorted(set(keys))])[-keep_renders:]
    used.write_text(json.dumps(hist))
    keep = set().union(*map(set, hist))
    for f in (w/'atoms').glob('*/*'):
        if f.stem.split('.')[0] not in keep: f.unlink(missing_ok=True)
    for f in w.glob('[0-9][0-9][0-9][0-9].*'):
        if f.suffix in ('.mp4', '.wav', '.json'): f.unlink(missing_ok=True)


def render(manifest_path, output, work_dir=None, frozen_plan=None):
    if frozen_plan:
        frozen = verify_frozen_plan(frozen_plan, manifest_path)
        p = frozen['plan']
        p['frozen_edit_plan'] = {'path': str(Path(frozen_plan).resolve()), 'sha256': file_sha256(frozen_plan)}
        protect_output(frozen_plan, output)
        for record in frozen['review_records']:
            protect_output(record['path'], output)
    else:
        p = plan(manifest_path)
    protect_output(p['source'], output)
    for x in p['inputs']:
        protect_output(x['path'], output)
    w = Path(work_dir or (str(output) + '.work')).resolve()
    w.mkdir(parents=True, exist_ok=True)
    global _render_started
    _render_started = time.time() - 1
    p['renderer_implementation_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    p['frame_filter_state_policy'] = 'Input reinit_filter=0 preserves n/trim/setpts across color metadata changes; no forced colorspace conversion or retagging.'
    save(w / 'render_plan.json', p)
    videos, audios = [], []
    fps = ratio(p['output_fps'])
    ripple = p.get('timing') == 'ripple'
    idents = {}
    for atom in p['segments']:
        key = atom_key(atom, p, idents)
        adir = w/'atoms'/key[:2]
        video, audio, meta = adir/(key+'.mp4'), adir/(key+'.wav'), adir/(key+'.json')
        atom['atom_key'] = key; atom['atom_video'] = str(video); atom['atom_audio'] = str(audio)
        if not (meta.exists() and video.exists() and audio.exists()):
            adir.mkdir(parents=True, exist_ok=True)
            render_atom(atom, p, fps, ripple, video, audio)
            save(meta, {'key': key, 'segment': {k: v for k, v in atom.items() if k not in ('index', 'out_in', 'out_out')}})
        videos.append(video); audios.append(audio)
        print(json.dumps({'progress': atom['index']+1, 'segments': len(p['segments']), 'cached': meta.stat().st_mtime < _render_started}), file=sys.stderr, flush=True)
    prune_atoms(w, [a['atom_key'] for a in p['segments']])
    vlist, alist = w/'video_concat.txt', w/'audio_concat.txt'
    vlist.write_text(''.join(concat_path(x) + (f'\nduration {atom["output_seconds"]:.9f}' if ripple else '') + '\n'
                             for x, atom in zip(videos, p['segments'])))
    alist.write_text('\n'.join(concat_path(x) for x in audios)+'\n')
    picture, base_audio = w/'picture.mp4', w/'base_audio.wav'
    ff(['-y', '-f', 'concat', '-safe', '0', '-i', vlist, '-c', 'copy', '-an', picture])
    ff(['-y', '-f', 'concat', '-safe', '0', '-i', alist, '-c:a', 'pcm_s16le', base_audio])
    cmd = ['-y', '-i', base_audio]
    filters, labels = ['[0:a]asetpts=PTS-STARTPTS[a0]'], ['[a0]']
    input_index = 1
    for i, aud in enumerate(p['audio_replacements']):
        cmd += ['-i', aud['path']]
        length = aud['end_sample']-aud['start_sample']
        tempo = '' if ripple else atempo(aud['speed']) + ','
        align = f'aresample={SR}:async=1:first_pts=0,' if ripple else ''
        filters.append(f'[{input_index}:a]{align}atrim=start={aud["clip_in"]:.12f}:end={aud["clip_out"]:.12f},'
            f'asetpts=PTS-STARTPTS,{tempo}aresample={SR},aformat=channel_layouts=stereo,'
            f'apad,atrim=end_sample={length},adelay={aud["start_sample"]}S:all=1[voice{i}]')
        labels.append(f'[voice{i}]'); input_index += 1
        if aud.get('background_path'):
            cmd += ['-i', aud['background_path']]
            offset = float(aud.get('background_clip_in', 0))
            gain = float(aud.get('background_gain', 1))
            filters.append(f'[{input_index}:a]atrim=start={offset:.12f},asetpts=PTS-STARTPTS,aresample={SR},'
                f'aformat=channel_layouts=stereo,apad,atrim=end_sample={length},volume={gain:.8f},'
                f'adelay={aud["start_sample"]}S:all=1[bed{i}]')
            labels.append(f'[bed{i}]'); input_index += 1
    filters.append(''.join(labels)+f'amix=inputs={len(labels)}:duration=longest:normalize=0,'
                   f'apad,atrim=end_sample={p["output_samples"]}[mix]')
    mix = w/'final_mix.wav'
    ff([*cmd, '-filter_complex', ';'.join(filters), '-map', '[mix]', '-c:a', 'pcm_s16le', '-ar', SR, '-ac', 2, mix])
    pcm_validation = verify_audio(mix, p['output_samples'], SR)
    ff(['-y', '-i', picture, '-i', mix, '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k', '-map_metadata', '-1', '-movflags', '+faststart', output])
    validation = verify(output, p['output_frames'], p['output_fps'], p['output_samples'], SR, require_audio=True)
    validation['pcm_mix_verification'] = pcm_validation
    save(str(output)+'.timeline.json', p)
    save(str(output)+'.verification.json', validation)
    return {'output': str(Path(output).resolve()), 'timeline': str(Path(str(output)+'.timeline.json').resolve()),
            'verification': validation, 'work_dir': str(w)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    q = sub.add_parser('probe'); q.add_argument('input'); q.add_argument('--frames', action='store_true'); q.add_argument('--output')
    q = sub.add_parser('normalize'); q.add_argument('input'); q.add_argument('output'); q.add_argument('--fps')
    q = sub.add_parser('burn-timecode'); q.add_argument('input'); q.add_argument('output'); q.add_argument('--fps')
    q = sub.add_parser('extract'); q.add_argument('input'); q.add_argument('directory'); q.add_argument('--start-frame', type=int, required=True); q.add_argument('--end-frame', type=int, required=True); q.add_argument('--step', type=int, default=1)
    q = sub.add_parser('scenes'); q.add_argument('input'); q.add_argument('--threshold', type=float, default=.3); q.add_argument('--output')
    q = sub.add_parser('plan'); q.add_argument('manifest'); q.add_argument('--output', required=True)
    q = sub.add_parser('freeze'); q.add_argument('manifest'); q.add_argument('--output', required=True); q.add_argument('--review-record', action='append', required=True)
    q = sub.add_parser('verify-frozen'); q.add_argument('frozen_plan'); q.add_argument('--manifest')
    q = sub.add_parser('render'); q.add_argument('manifest'); q.add_argument('--output', required=True); q.add_argument('--work-dir'); q.add_argument('--frozen-plan')
    q = sub.add_parser('verify'); q.add_argument('input'); q.add_argument('--expected-frames', type=int); q.add_argument('--fps'); q.add_argument('--expected-audio-samples', type=int); q.add_argument('--audio-sample-rate', type=int, default=SR); q.add_argument('--require-audio', action='store_true'); q.add_argument('--output')
    args = ap.parse_args(argv)
    if args.command == 'probe':
        result = probe(args.input, args.frames)
    elif args.command == 'normalize':
        result = normalize(args.input, args.output, args.fps)
    elif args.command == 'burn-timecode':
        result = burn_timecode(args.input, args.output, args.fps)
    elif args.command == 'extract':
        result = extract(args.input, args.directory, args.start_frame, args.end_frame, args.step)
    elif args.command == 'scenes':
        result = scenes(args.input, args.threshold)
    elif args.command == 'plan':
        result = plan(args.manifest)
    elif args.command == 'freeze':
        result = freeze(args.manifest, args.output, args.review_record)
    elif args.command == 'verify-frozen':
        checked = verify_frozen_plan(args.frozen_plan, args.manifest)
        result = {'verified': True, 'frozen_plan': str(Path(args.frozen_plan).resolve()),
                  'output_frames': checked['plan']['output_frames'], 'semantic_review_repeated': False}
    elif args.command == 'render':
        result = render(args.manifest, args.output, args.work_dir, args.frozen_plan)
    else:
        result = verify(args.input, args.expected_frames, args.fps, args.expected_audio_samples, args.audio_sample_rate, args.require_audio)
    if args.command in ('probe', 'scenes', 'plan', 'verify') and args.output:
        save(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
