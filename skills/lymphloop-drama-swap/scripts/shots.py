#!/usr/bin/env python3
"""Shot segmentation, labelled contact sheets and reference clips for agent review. Ranges are [in,out) decoded frames."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import sys
import threading
from fractions import Fraction
from pathlib import Path
import media_pipeline as media

CELL_WIDTH = 480
_CUT_SOURCE_PROBES = {}
_CUT_SOURCE_PROBES_LOCK = threading.Lock()


def _cut_source_probe(source):
    """Probe a stable source once per process, even when shots cut in threads.

    Frame PTS verification is intentionally kept on the first call.  The
    path/size/mtime key invalidates the cache if a master is replaced.
    """
    source = Path(source).resolve()
    stat = source.stat()
    key = (str(source), stat.st_size, stat.st_mtime_ns)
    with _CUT_SOURCE_PROBES_LOCK:
        if key not in _CUT_SOURCE_PROBES:
            _CUT_SOURCE_PROBES[key] = media.probe(str(source), include_frames=True)
        return _CUT_SOURCE_PROBES[key]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def seek_seconds(frame, fps):
    """Input seek half a frame before the target so the first decoded frame is exactly `frame`."""
    return max(Fraction(0), (Fraction(frame) - Fraction(1, 2)) / media.ratio(fps))


def sample_indices(count, fps, fps_sample, max_frames):
    if count <= 0:
        raise ValueError('Empty shot')
    wanted = min(max_frames, max(1, math.ceil(count / float(media.ratio(fps)) * fps_sample)), count)
    if wanted == 1:
        return [count // 2]
    return sorted({round(i * (count - 1) / (wanted - 1)) for i in range(wanted)})


def label_file(folder, in_frame, fps, offset_seconds=0):
    """drawtext template: F<source frame> mm:ss.ff where ff is the nominal frame within the second."""
    nominal = media.rounded(media.ratio(fps))
    base = f'n+{int(in_frame)}'
    shift = int(round(offset_seconds * nominal))
    t = f'({base}+{shift})' if shift else f'({base})'
    text = (f'F%{{eif:{base}:d}} %{{eif:trunc({t}/{nominal}/60):d:2}}:%{{eif:mod(trunc({t}/{nominal}),60):d:2}}'
            f'.%{{eif:mod({t},{nominal}):d:2}}')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'label_{int(in_frame)}_{shift}.txt'
    path.write_text(text)
    return path


def drawtext(textfile, fontsize=20):
    font = media.font_path()
    prefix = f"fontfile='{media.filter_quote(font)}':" if font else 'font=monospace:'
    return (f"drawtext={prefix}textfile='{media.filter_quote(Path(textfile).resolve())}':fontsize={fontsize}:"
            f"fontcolor=black:box=1:boxcolor=white:boxborderw=4:x=6:y=6")


def contact_sheet(source, out_png, in_frame, out_frame, fps, picks, cols=4, label_offset_seconds=0, cell_width=CELL_WIDTH):
    """One PNG grid of the picked local frame offsets, every cell labelled with its source frame and time."""
    out_png = Path(out_png).resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    count = out_frame - in_frame
    picks = sorted({int(p) for p in picks if 0 <= int(p) < count})
    if not picks:
        raise ValueError('No frames picked for the contact sheet')
    rows = math.ceil(len(picks) / cols)
    textfile = label_file(out_png.parent / '.labels', in_frame, fps, label_offset_seconds)
    select = '+'.join(f'eq(n\\,{p})' for p in picks)
    vf = (f'trim=end_frame={count},{drawtext(textfile)},select={select},'
          f'scale={cell_width}:-2,tile={cols}x{rows}:padding=4:margin=4:color=0x202020')
    media.ff(['-y', '-reinit_filter', '0', '-ss', str(float(seek_seconds(in_frame, fps))), '-i', str(source),
              '-map', '0:v:0', '-an', '-vf', vf, '-frames:v', '1', '-update', '1', str(out_png)])
    if not out_png.is_file():
        raise RuntimeError('Contact sheet was not written: ' + str(out_png))
    return {'path': str(out_png), 'frames': [in_frame + p for p in picks], 'cols': cols, 'rows': rows}


def single_frame(source, frame, fps, out_jpg):
    out_jpg = Path(out_jpg).resolve()
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    media.ff(['-y', '-ss', str(float(seek_seconds(frame, fps))), '-i', str(source), '-map', '0:v:0', '-an',
              '-frames:v', '1', '-q:v', '2', '-update', '1', str(out_jpg)])
    return str(out_jpg)


def merge_short(boundaries, fps, min_seconds):
    """Boundaries [0, c1, ..., N]; fold fragments shorter than min_seconds into a neighbour."""
    rate = float(media.ratio(fps))
    bounds = list(boundaries)
    while len(bounds) > 2:
        lengths = [(bounds[i + 1] - bounds[i]) / rate for i in range(len(bounds) - 1)]
        i = min(range(len(lengths)), key=lambda k: lengths[k])
        if lengths[i] >= min_seconds:
            break
        # Merge the shortest fragment into its shorter neighbour; the first/last can only merge inward.
        if i == 0:
            bounds.pop(1)
        elif i == len(lengths) - 1:
            bounds.pop(i)
        else:
            bounds.pop(i if lengths[i - 1] <= lengths[i + 1] else i + 1)
    return bounds


def detect(source, out_dir, threshold=.3, min_shot_seconds=.4, fps_sample=2, cols=4, max_frames=16):
    source = Path(source).resolve()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    info = media.probe(str(source), include_frames=True)
    media.require_cfr(info)
    n, fps = info['frame_count'], info['fps']
    cuts = sorted({int(c['source_frame']) for c in media.scenes(str(source), threshold)['candidates'] if 0 < int(c['source_frame']) < n})
    bounds = merge_short([0, *cuts, n], fps, min_shot_seconds)
    rate = float(media.ratio(fps))
    shots = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
        sid = f'S{i:03d}'
        picks = sample_indices(b - a, fps, fps_sample, max_frames)
        sheet = contact_sheet(source, out / 'sheets' / f'{sid}.png', a, b, fps, picks, cols)
        mid = single_frame(source, a + (b - a) // 2, fps, out / 'frames' / f'{sid}.jpg')
        shots.append({'id': sid, 'in_frame': a, 'out_frame': b, 'start_s': a / rate, 'end_s': b / rate,
                      'duration_s': (b - a) / rate, 'sheet': sheet['path'], 'mid_frame': mid,
                      'sampled_frames': sheet['frames']})
    record = {'type': 'lymphloop_shot_list', 'source': str(source), 'source_sha256': sha(source), 'fps': fps,
              'frame_count': n, 'duration_s': n / rate, 'scene_threshold': threshold,
              'min_shot_seconds': min_shot_seconds, 'boundary_convention': '[in,out)', 'shots': shots}
    media.save(out / 'shots.json', record)
    return record


def scale_filter(max_short_edge):
    return (f"scale=w='if(gt(iw,ih),-2,min(iw,{max_short_edge}))':h='if(gt(iw,ih),min(ih,{max_short_edge}),-2)'")


def cut(source, in_frame, out_frame, out_path, mute=False, max_short_edge=720, max_mb=19):
    """Frame-accurate small clip of a shot (used for Flash screening); optionally silent and downscaled."""
    source = Path(source).resolve()
    out_path = Path(out_path).resolve()
    media.protect_output(source, out_path)
    info = _cut_source_probe(source)
    media.require_cfr(info)
    fps = info['fps']
    if not 0 <= in_frame < out_frame <= info['frame_count']:
        raise ValueError('Invalid [in,out) frame range for this source')
    count = out_frame - in_frame
    rate = media.ratio(fps)
    natural = Fraction(count) / rate
    vf = [f'trim=end_frame={count}', scale_filter(max_short_edge)]
    af = [f'atrim=end={float(natural):.6f}', 'asetpts=PTS-STARTPTS']
    has_audio = bool(info['audio']) and not mute
    import agent_native
    crf = 23
    for attempt in range(4):
        v_enc = (agent_native.hw_encode_args(max(40, 65 - attempt * 8))
                 if agent_native.hw_encode_enabled() else
                 ['-c:v', 'libx264', '-preset', 'fast', '-crf', str(crf), '-pix_fmt', 'yuv420p'])
        args = ['-y', '-reinit_filter', '0', '-ss', str(float(seek_seconds(in_frame, fps))), '-i', str(source),
                '-map', '0:v:0', '-vf', ','.join(vf), *v_enc, '-movflags', '+faststart']
        if has_audio:
            args += ['-map', '0:a:0', '-af', ','.join(af), '-c:a', 'aac', '-b:a', '128k']
        else:
            args += ['-an']
        args += ['-frames:v', str(count)]
        media.ff(args + [str(out_path)])
        if out_path.stat().st_size <= max_mb * 1024 * 1024 or attempt == 3:
            break
        crf += 6
    done = media.probe(str(out_path))
    record = {'path': str(out_path), 'source': str(source), 'in_frame': in_frame, 'out_frame': out_frame,
              'duration_s': done['video_duration'], 'frame_count': done['frame_count'], 'fps': done['fps'],
              'size_bytes': out_path.stat().st_size, 'sha256': sha(out_path), 'muted': not has_audio,
              'has_audio': bool(done['audio']), 'crf': crf}
    if out_path.stat().st_size > max_mb * 1024 * 1024:
        record['oversize'] = True
    return record


def sheet(video, out_png, fps_sample=2, cols=4, max_frames=16, label_offset_seconds=0):
    video = Path(video).resolve()
    info = media.probe(str(video))
    picks = sample_indices(info['frame_count'], info['fps'], fps_sample, max_frames)
    return contact_sheet(video, out_png, 0, info['frame_count'], info['fps'], picks, cols, label_offset_seconds)


def frames(video, out_dir, fps_sample=4, maximum=40):
    video = Path(video).resolve()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    info = media.probe(str(video))
    for old in out.glob('f*.jpg'):
        old.unlink()
    media.ff(['-y', '-i', str(video), '-map', '0:v:0', '-an', '-vf', f'fps={fps_sample}', '-frames:v', str(maximum),
              '-q:v', '2', str(out / 'f%04d.jpg')])
    files = sorted(out.glob('f*.jpg'))
    rows = [{'index': i, 'path': str(p), 'approx_seconds': i / fps_sample} for i, p in enumerate(files)]
    record = {'video': str(video), 'fps_sample': fps_sample, 'source_fps': info['fps'], 'frames': rows}
    media.save(out / 'frames.json', record)
    return record


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest='command', required=True)
    d = s.add_parser('detect')
    d.add_argument('source'); d.add_argument('--out', required=True); d.add_argument('--threshold', type=float, default=.3)
    d.add_argument('--min-shot-seconds', type=float, default=.4); d.add_argument('--fps-sample', type=float, default=2)
    d.add_argument('--cols', type=int, default=4); d.add_argument('--max-frames', type=int, default=16)
    c = s.add_parser('cut')
    c.add_argument('source'); c.add_argument('--in-frame', type=int, required=True); c.add_argument('--out-frame', type=int, required=True)
    c.add_argument('--out', required=True); c.add_argument('--mute', action='store_true')
    c.add_argument('--max-short-edge', type=int, default=720); c.add_argument('--max-mb', type=float, default=19)
    h = s.add_parser('sheet')
    h.add_argument('video'); h.add_argument('--out', required=True); h.add_argument('--fps', type=float, default=2)
    h.add_argument('--cols', type=int, default=4); h.add_argument('--max-frames', type=int, default=16)
    h.add_argument('--label-offset-seconds', type=float, default=0)
    f = s.add_parser('frames')
    f.add_argument('video'); f.add_argument('--out', required=True); f.add_argument('--fps', type=float, default=4)
    f.add_argument('--max', type=int, default=40)
    a = p.parse_args(argv)
    if a.command == 'detect':
        result = detect(a.source, a.out, a.threshold, a.min_shot_seconds, a.fps_sample, a.cols, a.max_frames)
        result = {k: v for k, v in result.items() if k != 'shots'} | {'shots': len(result['shots']), 'shots_json': str(Path(a.out).resolve() / 'shots.json')}
    elif a.command == 'cut':
        result = cut(a.source, a.in_frame, a.out_frame, a.out, a.mute, a.max_short_edge, a.max_mb)
    elif a.command == 'sheet':
        result = sheet(a.video, a.out, a.fps, a.cols, a.max_frames, a.label_offset_seconds)
    else:
        result = frames(a.video, a.out, a.fps, a.max)
        result = {k: v for k, v in result.items() if k != 'frames'} | {'count': len(result['frames'])}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
