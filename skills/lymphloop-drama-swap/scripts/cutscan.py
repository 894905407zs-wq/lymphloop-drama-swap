"""Frame-exact hard cuts, nothing merged (user 2026-11-21: "杜绝滴剂瓶闪回").

shots.py folds shots shorter than 0.4 s into a neighbour and Flash samples 2-3 frames a second, so a 0.25-0.5 s piece of
the competitor's shot next to a segment edge (or inside a spliced generated segment) was never planned and never seen
(3389 G01: two 0.25 s flashes of the dropper bottle; 2210 G06/G07: 1.4 s). This module lists every cut on a tiny gray
decode (about a minute for a 10-minute film), so plan can put segment edges on real cuts and final-screen can look at
every frame next to a seam and every short fragment.

A cut is a frame whose mean absolute difference to the previous frame is > ABS and > REL x the local median + OFF
(validated by eye on 26 masters, 2026-11-21: dissolves and light leaks give a few extra cuts, which only make a segment
or a checked window a little longer).
"""
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import json, subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np

import media_pipeline as media

W, H = 72, 128
ABS, REL, OFF, LOCAL = 18.0, 3.0, 6.0, 8
VERSION = 1


def _diffs(video):
    info = media.probe(str(video))
    fps = float(Fraction(str(info['fps'])))
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-i', str(video), '-an', '-vf', f'scale={W}:{H}',
                          '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], capture_output=True, check=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.float32)
    return np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2)), len(fr), fps


def cuts_from_diffs(d):
    """First frames of shots (frame i+1 when the jump is between i and i+1)."""
    out = []
    for i in range(len(d)):
        lo, hi = max(0, i - LOCAL), min(len(d), i + LOCAL + 1)
        nb = np.r_[d[lo:i], d[i + 1:hi]]
        loc = float(np.median(nb)) if len(nb) else 0.0
        if d[i] > ABS and d[i] > REL * loc + OFF:
            out.append(i + 1)
    return out


def cuts(video, cache=None):
    """{'cuts': [...], 'frames': n, 'fps': f}. `cache` is a json path; reused while the video's bytes are the same."""
    video = Path(video)
    key = {'sha256': _sha(video), 'version': VERSION}
    if cache and Path(cache).is_file():
        c = json.loads(Path(cache).read_text())
        if c.get('key') == key:
            return c
    d, n, fps = _diffs(video)
    res = {'key': key, 'cuts': cuts_from_diffs(d), 'frames': n, 'fps': fps}
    if cache:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        Path(cache).write_text(json.dumps(res))
    return res


def _sha(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def snap_edges(segments, raw_cuts, fps, total, window_s=0.5, max_s=15.0):
    """Put each segment edge on a frame-exact cut and pull short pieces next to an edge into the segment.

    - An edge with no cut within 1 frame (and not a planned in-shot cut) moves OUT to the nearest cut within
      `window_s`; never in (1190 G10 ends inside a white dissolve: moving in would leave the dissolve's original
      frames, which may hold the old product, outside the segment). No cut outside: a warning to check the seam.
    - Then, while a cut lies OUTSIDE an edge within `window_s` (a piece of at most window_s between that cut and the
      edge would stay original), the edge moves out to it.
    Never crosses into another segment, never makes a segment longer than `max_s`; what cannot be done is a warning.
    Edits segments in place; returns a list of notes."""
    cutset = sorted(set(raw_cuts))
    W_ = int(round(window_s * fps))
    notes = []
    order = sorted(range(len(segments)), key=lambda k: segments[k]['in_frame'])

    def near(f, lo, hi):
        return [c for c in cutset if lo <= c <= hi and c != f]

    for pos, k in enumerate(order):
        s = segments[k]
        prev_out = segments[order[pos - 1]]['out_frame'] if pos > 0 else 0
        next_in = segments[order[pos + 1]]['in_frame'] if pos + 1 < len(order) else total
        old = (s['in_frame'], s['out_frame'])
        a, b = old
        msgs = []

        def on_cut(f):
            return f in (0, total) or any(abs(c - f) <= 1 for c in cutset)

        # 1. edges that are not on a cut
        if not s.get('in_shot_cut_in') and not on_cut(a) and a > prev_out:
            out = [c for c in near(a, max(prev_out, a - W_), a - 1)]
            if out: a = max(out); msgs.append(f'in edge {old[0]} not on a cut: out to cut {a}')
            else: msgs.append(f'in edge {old[0]} not on a cut and no cut within {window_s}s before it: check the seam')
        if not s.get('in_shot_cut_out') and not on_cut(b) and b < next_in:
            out = [c for c in near(b, b + 1, min(next_in, b + W_))]
            if out: b = min(out); msgs.append(f'out edge {old[1]} not on a cut: out to cut {b}')
            else: msgs.append(f'out edge {old[1]} not on a cut and no cut within {window_s}s after it: check the seam')
        # 2. short pieces just outside an edge
        if not s.get('in_shot_cut_in'):
            while True:
                out = near(a, max(prev_out, a - W_), a - 1)
                if not out: break
                c = min(out)
                if (b - c) / fps > max_s:
                    msgs.append(f'piece {c}-{a} (<= {window_s}s) before the segment stays original: segment would pass {max_s}s, check it'); break
                msgs.append(f'short piece {c}-{a} before the segment pulled in'); a = c
        if not s.get('in_shot_cut_out'):
            while True:
                out = near(b, b + 1, min(next_in, b + W_))
                if not out: break
                c = max(out)
                if (c - a) / fps > max_s:
                    msgs.append(f'piece {b}-{c} (<= {window_s}s) after the segment stays original: segment would pass {max_s}s, check it'); break
                msgs.append(f'short piece {b}-{c} after the segment pulled in'); b = c
        if (b - a) / fps > max_s:           # step 1 outward move pushed it over: undo
            a, b = old; msgs.append(f'snapping would pass {max_s}s: edges left as planned, check the seams')
        if (a, b) != old:
            _shift(s, a, b, fps)
        if msgs:
            s['edge_snap'] = msgs; notes.append({'id': s['id'], 'from': list(old), 'to': [a, b], 'notes': msgs})
    return notes


def _shift(s, a, b, fps):
    d = (s['in_frame'] - a) / fps          # seconds added at the head (negative when the head moved in)
    s['in_frame'], s['out_frame'] = a, b
    s['duration_s'] = round((b - a) / fps, 4)
    for x in s.get('speech') or []:
        x['start'] = round(x['start'] + d, 3); x['end'] = round(x['end'] + d, 3)
    if s.get('shot_times'):
        st = [[i, round(x + d, 2), round(y + d, 2)] for i, x, y in s['shot_times']]
        st[0][1] = 0.0; st[-1][2] = round((b - a) / fps, 2)
        s['shot_times'] = st


def flashback_spots(timeline_segments, raw_cuts, total, fps, ranges=None, window_s=0.5, frag_s=0.5, step=3, joins=()):
    """Where a competitor flashback can hide, for the AGENT to look at (user 2026-11-21: "让gemini看帧版不如agent自己看"):
    - each seam where a generated picture meets anything, and each splice join from the splice recipes (`joins`, master
      frames: a join between two takes of the same camera is no cut at all, 3389 G01 F33): every `step`-th frame
      within `window_s` each side, plus the two frames at the join (3389's flashes were 6 frames, so every 3rd frame
      shows each of them twice);
    - every frame of each fragment of at most `frag_s` between two cuts, plus 2 frames each side.
    Overlapping spots merge. Limited to `ranges` when given. Returns [{'kinds': [...], 'at': [...], 'frames': [...]}]
    in film order. Shots inside a generated segment are the review step's job (the agent's frame sheets), not this."""
    W_ = int(round(window_s * fps)); F_ = int(round(frag_s * fps))
    raw = []
    tl = sorted(timeline_segments, key=lambda e: e['out_in'])
    for x, y in zip(tl, tl[1:]):
        if not (x.get('picture_id') or y.get('picture_id')): continue
        j = y['out_in']
        raw.append(('seam ' + (x.get('picture_id') or 'original') + '|' + (y.get('picture_id') or 'original'), j,
                    set(range(max(0, j - W_), min(total, j + W_), step)) | {j - 1, j}))
    for j in sorted(set(joins)):
        raw.append(('splice join', j, set(range(max(0, j - W_), min(total, j + W_), step)) | {j - 1, j}))
    cs = sorted(set([0, *raw_cuts, total]))
    for c0, c1 in zip(cs, cs[1:]):
        if c1 - c0 <= F_:
            raw.append((f'short shot {c1 - c0}f', c0, set(range(max(0, c0 - 2), min(total, c1 + 2)))))
    if ranges is not None:
        raw = [(k, j, {f for f in fr if any(r0 <= f < r1 for r0, r1 in ranges)}) for k, j, fr in raw]
    raw = sorted(((k, j, fr) for k, j, fr in raw if fr), key=lambda r: min(r[2]))
    groups = []
    for k, j, fr in raw:
        if groups and min(fr) <= max(groups[-1]['frames']) + 1:
            g = groups[-1]; g['kinds'].append(k); g['at'].append(j); g['frames'] = sorted(set(g['frames']) | fr)
        else:
            groups.append({'kinds': [k], 'at': [j], 'frames': sorted(fr)})
    for g in groups: g['frames'] = [f for f in g['frames'] if 0 <= f < total]
    return [g for g in groups if g['frames']]


def spot_sheets(video, spots, out_dir, fps, cols=5, max_rows=3, w=300):
    """One sheet per spot (or several when a spot has more than cols x max_rows frames), cells 300 px wide so a sheet
    stays under ~1600 px and is not shrunk when the agent opens it: a bottle in a bag or a hand is a few dozen pixels
    on the 720 master and disappears in 110-240 px cells. Each cell is labelled with its master frame and time, the
    sheet with what the spot is. Returns [{'path', 'kinds', 'at', 'frames'}]."""
    from PIL import Image, ImageDraw, ImageFont
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    info = media.probe(str(video)); vw, vh = int(info['video']['width']), int(info['video']['height'])
    h = int(round(w * vh / vw / 2) * 2)
    face = oscompat.find_font('Helvetica.ttc', *oscompat.SANS_REGULAR)
    try: font = ImageFont.truetype(face, 20); big = ImageFont.truetype(face, 24)
    except (OSError, AttributeError, TypeError): font = big = ImageFont.load_default()
    out, n = [], 0
    for sp in spots:
        fr = sp['frames']; a, b = fr[0], fr[-1] + 1
        raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-ss', f'{a / fps:.4f}', '-i', str(video), '-frames:v', str(b - a),
                              '-vf', f'scale={w}:{h}', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
        want = set(fr); imgs = {}
        for i in range(len(raw) // (w * h * 3)):
            if a + i in want: imgs[a + i] = Image.frombytes('RGB', (w, h), raw[i * w * h * 3:(i + 1) * w * h * 3])
        got = [f for f in fr if f in imgs]; per = cols * max_rows
        for p in range(0, len(got), per):
            chunk = got[p:p + per]; rows = (len(chunk) + cols - 1) // cols
            sheet = Image.new('RGB', (cols * w, rows * h + 36), 'white'); dr = ImageDraw.Draw(sheet)
            title = ' + '.join(f"{k} @ {int(j / fps // 60)}:{j / fps % 60:05.2f} (f{j})" for k, j in zip(sp['kinds'], sp['at']))
            dr.text((6, 6), title[:150], fill='black', font=big)
            for i, f in enumerate(chunk):
                x, y = (i % cols) * w, 36 + (i // cols) * h
                sheet.paste(imgs[f], (x, y))
                t = f / fps; lab = f'f{f} {int(t // 60)}:{t % 60:05.2f}'
                dr.rectangle([x, y, x + 150, y + 26], fill='black'); dr.text((x + 4, y + 2), lab, fill='yellow', font=font)
                if f in sp['at']: dr.rectangle([x, y, x + w - 1, y + h - 1], outline='red', width=4)
            n += 1; path = out_dir / f'spot_{n:03d}.jpg'; sheet.save(path, quality=88)
            out.append({'path': str(path), 'kinds': sp['kinds'], 'at': sp['at'], 'frames': chunk})
    return out
