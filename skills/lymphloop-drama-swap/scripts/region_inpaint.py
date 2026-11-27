#!/usr/bin/env python3
"""Targeted post-fix: TELEA-inpaint one rectangle over a frame range of a rendered master.

Used for residue the automatic passes missed (a burned-in caption whose words no longer match the
new audio, a faint watermark ghost at a position the tracker dropped). Audio is copied untouched.

  region_inpaint.py IN.mp4 OUT.mp4 --box X,Y,W,H[:GATE] --frames A,B [--box ... --frames ...] [--gate GATE ...]

Each --box pairs with the --frames given in the same order. The gate is per box: `--box X,Y,W,H:purple`, or
--gate given once per --box in the same order; a single --gate (old form) applies to every box without its own
`:GATE`. GATE is purple, ink or none. With purple only pixels of the caption highlight colour (and the white glyphs next
to them) are inpainted, and frames in the range without any such pixels are left untouched, so a loose range is
safe; ink inpaints only grey or dark printing on a light, unsaturated surface inside the box (old brand text left on
a card or a paper label), leaving skin and the surface edges alone; none inpaints the whole box.
"""
import argparse, json, subprocess, sys, os
from pathlib import Path
import numpy as np, cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_pipeline as _media  # noqa: E402  (the one tool lookup: media_pipeline.binary)
FFMPEG = _media.binary('ffmpeg'); FFPROBE = _media.binary('ffprobe')
GATES = {'purple', 'ink', 'none'}

def probe(path):
    out = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate,nb_frames',
                          '-of', 'json', str(path)], check=True, capture_output=True, text=True).stdout
    s = json.loads(out)['streams'][0]
    num, den = s['r_frame_rate'].split('/'); return int(s['width']), int(s['height']), f'{num}/{den}', int(s.get('nb_frames') or 0)

def parse_jobs(boxes, frames, gates=None):
    """[(x, y, w, h, f0, f1, gate)] from the paired --box/--frames lists and the --gate list (see the module doc)."""
    if len(boxes) != len(frames): raise ValueError('--box and --frames must pair up')
    gates = list(gates or [])
    if len(gates) not in (0, 1, len(boxes)): raise ValueError('--gate: give it once (all boxes) or once per --box, in order')
    jobs = []
    for i, (box, fr) in enumerate(zip(boxes, frames)):
        rect, _, own = box.partition(':')
        gate = own or (gates[i] if len(gates) == len(boxes) and len(boxes) > 1 else (gates[0] if gates else 'none'))
        gate = (gate or 'none').lower()
        if gate not in GATES: raise ValueError(f'unknown gate {gate!r} (known: {", ".join(sorted(GATES))})')
        x, y, w, h = map(int, rect.split(',')); f0, f1 = map(int, fr.split(','))
        jobs.append((x, y, w, h, f0, f1, gate))
    return jobs


def gate_mask(roi, gate):
    if gate == 'purple':
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        purple = cv2.inRange(hsv, (120, 60, 90), (165, 255, 255))
        white = cv2.inRange(roi, (215, 215, 215), (255, 255, 255))
        if cv2.countNonZero(purple) < 40: return None
        m = cv2.dilate(purple | white, np.ones((5, 5), np.uint8), iterations=2)
        return m
    if gate == 'ink':
        # Grey or dark printing on a light, unsaturated surface (a card, a paper label) that a generation left with the
        # old brand on it (2026-11-11, Metabolae: the card read LYMPHLOOP in the close-up and METABOLAE in the moving
        # shot). Only the ink is inpainted; skin, the surface edge and everything else in the box stay.
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV); sat, val = hsv[..., 1], hsv[..., 2]
        light = ((sat < 70) & (val > 120)).astype(np.uint8) * 255
        if cv2.countNonZero(light) < 0.1 * light.size: return None
        surface = cv2.morphologyEx(light, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
        level = float(np.percentile(val[light > 0], 75))
        ink = ((surface > 0) & (val < level - 14) & (sat < 90)).astype(np.uint8) * 255
        ink &= cv2.erode(surface, np.ones((3, 3), np.uint8))
        if cv2.countNonZero(ink) < 10: return None
        return cv2.dilate(ink, np.ones((3, 3), np.uint8), iterations=2) & surface
    return np.full(roi.shape[:2], 255, np.uint8)

def generated_ranges(src):
    """[(out_in, out_out, picture_id)] of the generated pictures in a recut master, read from `<src>.timeline.json`
    (written by recut next to OUT/master.mp4). Empty when there is no timeline next to the file."""
    tl = Path(str(src) + '.timeline.json')
    if not tl.is_file(): return []
    try: segs = json.loads(tl.read_text()).get('segments') or []
    except (OSError, ValueError): return []
    return [(int(s['out_in']), int(s['out_out']), s.get('picture_id')) for s in segs
            if '/generation/results/' in str(s.get('path') or '') and 'out_in' in s and 'out_out' in s]


def forbidden_jobs(jobs, ranges):
    """Jobs that would paint over a generated picture with a whole-box fill (gate none).

    User 2026-11-14: patching (贴图) a generated picture to hide a goof is not allowed; a goof in a generated picture is
    cut, replaced by a whole shot from a sibling film of the same footage, or regenerated with the user's consent.
    Text-only gates (purple captions, ink printing) stay allowed: they remove leftover caption / old-brand text, not a
    product or an action."""
    bad = []
    for j in jobs:
        x, y, w, h, f0, f1, gate = j
        if gate != 'none': continue
        for a, b, pid in ranges:
            if f0 < b and a <= f1: bad.append((j, pid)); break
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('src'); ap.add_argument('dst')
    ap.add_argument('--box', action='append', required=True); ap.add_argument('--frames', action='append', required=True)
    ap.add_argument('--gate', action='append', help='purple|ink|none; once = every box, or once per --box in order (a box\'s own :GATE wins)')
    ap.add_argument('--radius', type=int, default=3); ap.add_argument('--crf', default='18')
    ap.add_argument('--report')
    ap.add_argument('--user-approved', help="the user's own words allowing a whole-box fill on a generated picture for this fix "
                    "(2026-11-21 film 1753 G17: \"直接用第二次补做的成片现用版本，本地抹除这个边缘瓶子即可\"); recorded in the report")
    a = ap.parse_args()
    try: jobs = parse_jobs(a.box, a.frames, a.gate)
    except ValueError as e: sys.exit(str(e))
    bad = forbidden_jobs(jobs, generated_ranges(a.src))
    if bad and a.user_approved:
        print('user-approved whole-box fill on a generated picture: ' + a.user_approved, file=sys.stderr); bad = []
    if bad:
        sys.exit('禁止在生成段画面上贴图补救（用户 2026-11-14）：' + '; '.join(
            f'--box {x},{y},{w},{h} --frames {f0},{f1} 落在生成段 {pid}' for (x, y, w, h, f0, f1, _), pid in bad)
            + '。改用剪掉这几帧 / 借同素材兄弟片的整镜，或报告用户决定是否付费重生成。只抹字幕或旧字用 :purple / :ink。')
    W, H, fps, n = probe(a.src)
    dec = subprocess.Popen([FFMPEG, '-v', 'error', '-i', a.src, '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], stdout=subprocess.PIPE)
    tmp = Path(a.dst).with_suffix('.video.mp4')
    enc = subprocess.Popen([FFMPEG, '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', fps, '-i', '-',
                            '-c:v', 'libx264', '-preset', 'medium', '-crf', a.crf, '-pix_fmt', 'yuv420p', str(tmp)], stdin=subprocess.PIPE)
    touched = []; per_box = [0] * len(jobs); i = 0; size = W*H*3
    while True:
        raw = dec.stdout.read(size)
        if len(raw) < size: break
        frame = np.frombuffer(raw, np.uint8).reshape(H, W, 3)
        out = None
        for k, (x, y, w, h, f0, f1, gate) in enumerate(jobs):
            if f0 <= i <= f1:
                roi = (out if out is not None else frame)[y:y+h, x:x+w]
                m = gate_mask(roi, gate)
                if m is None: continue
                if out is None: out = frame.copy()
                mask = np.zeros((H, W), np.uint8); mask[y:y+h, x:x+w] = m
                out = cv2.inpaint(out, mask, a.radius, cv2.INPAINT_TELEA); per_box[k] += 1
                if not touched or touched[-1] != i: touched.append(i)
        enc.stdin.write((out if out is not None else frame).tobytes()); i += 1
    enc.stdin.close(); enc.wait(); dec.wait()
    subprocess.run([FFMPEG, '-y', '-v', 'error', '-i', str(tmp), '-i', a.src, '-map', '0:v:0', '-map', '1:a:0?', '-c:v', 'copy', '-c:a', 'copy',
                    '-movflags', '+faststart', a.dst], check=True)
    tmp.unlink()
    rep = {'type': 'lymphloop_region_inpaint', 'source': a.src, 'output': a.dst,
           'jobs': [{**dict(zip(('x','y','w','h','f0','f1','gate'), j)), 'frames_touched': n} for j, n in zip(jobs, per_box)],
           'gate': a.gate, 'frames_read': i, 'frames_touched': len(touched), 'first_touched': touched[:5], 'last_touched': touched[-5:],
           'user_approved': a.user_approved}
    if a.report: Path(a.report).write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep))

if __name__ == '__main__':
    main()
