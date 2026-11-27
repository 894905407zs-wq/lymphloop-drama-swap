#!/usr/bin/env python3
"""Find where one film's footage sits in another, frame for frame (the matching half of `swap.py reuse`).

User 2026-11-22: in the three batches of 26 films only 11 were generated; 15 were re-edits of the same footage (another
hook, another ending, a re-upload) and were given the reviewed results of a representative film by hand. This makes
that a command: every frame of both films is reduced to a tiny grey picture (a print), a generated segment of the donor
is searched in the target by a few anchor frames and then checked over its whole length at one offset.

  reuse.py match DONOR.mp4 TARGET.mp4 --range A-B [--cache DIR]     # where donor frames [A, B) are in the target
"""
from __future__ import annotations
import argparse, hashlib, json, subprocess, sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media  # noqa: E402

PW, PH = 18, 32            # print size (9:16); 576 grey values a frame
VERSION = 2
# Measured 2026-11-22 on five films of one batch (three with another first second, two with another hook): the same
# frame in two encodes differs by 0.1-0.4 on average and 1.1 at worst; other footage by 21-36. The limits stay close
# to the encode noise on purpose: the same shot one second later (a locked-off talking head) is only a few units away,
# and taking it for the same frames would put the donor's clip in at the wrong moment. Too strict costs a generation;
# too loose costs a wrong film.
SAME = 1.5                 # mean abs difference (0-255) under which two prints are the same frame
ANCHOR = 3.0               # looser, for finding candidates
FRAC_OK = 0.98             # share of a range's frames that must be under SAME ...
WORST = 5.0                # ... and no frame above this
TOLERANCE = 1.0            # swap.py reuse --tolerance scales the three limits (heavier re-encodes)

def prints(video, cache_dir=None):
    """uint8 array [frames, PW*PH] of the film (blurred by the downscale; borders, captions and the floating mark move
    it very little). Cached by the file's bytes."""
    video = Path(video)
    key = None
    if cache_dir:
        h = hashlib.sha256()
        with open(video, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
        key = Path(cache_dir)/f'{h.hexdigest()[:24]}.v{VERSION}.npy'
        if key.is_file(): return np.load(key)
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-i', str(video), '-an', '-vf', f'scale={PW}:{PH}:flags=area,format=gray',
                          '-fps_mode', 'passthrough', '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
    arr = np.frombuffer(raw, np.uint8).reshape(-1, PW * PH).copy()
    if key:
        key.parent.mkdir(parents=True, exist_ok=True); tmp = key.with_suffix('.part.npy'); np.save(tmp, arr); tmp.replace(key)
    return arr


def _dist(a, b):
    """Mean abs difference per frame of two equally long print stacks, brightness offset removed (encodes differ in level)."""
    a = a.astype(np.float32); b = b.astype(np.float32)
    a -= a.mean(axis=1, keepdims=True); b -= b.mean(axis=1, keepdims=True)
    return np.abs(a - b).mean(axis=1)


def range_distance(dp, tp, d0, d1, off):
    """Per-frame distance between donor frames [d0, d1) and target frames at +off; None when it runs off the target."""
    t0, t1 = d0 + off, d1 + off
    if t0 < 0 or d0 < 0 or d1 > len(dp): return None
    if t1 > len(tp):
        if d1 == len(dp) and t1 - len(tp) <= 3 and len(tp) - t0 >= 3:
            trim = t1 - len(tp)
            return _dist(dp[d0:d1 - trim], tp[t0:len(tp)])
        return None
    return _dist(dp[d0:d1], tp[t0:t1])


def judge(d):
    return {'mean': round(float(d.mean()), 2), 'worst': round(float(d.max()), 2), 'share_same': round(float((d < SAME * TOLERANCE).mean()), 4),
            'ok': bool((d < SAME * TOLERANCE).mean() >= FRAC_OK and d.max() < WORST * TOLERANCE)}


def locate(dp, tp, d0, d1, anchors=5, top=40):
    """Where donor frames [d0, d1) are in the target: {'offset', 'mean', 'worst', 'share_same', 'ok'} of the best offset
    (target frame = donor frame + offset), or None when no offset is supported by two anchors. `ok` only when the whole
    range is the same picture."""
    n = d1 - d0
    if n < 3 or not len(tp): return None
    tpf = tp.astype(np.float32); tpf -= tpf.mean(axis=1, keepdims=True)
    votes = {}
    for k in range(1, anchors + 1):
        i = d0 + (n * k) // (anchors + 1)
        a = dp[i].astype(np.float32); a -= a.mean()
        dist = np.abs(tpf - a).mean(axis=1)
        for j in np.argsort(dist)[:top]:
            if dist[j] < ANCHOR * TOLERANCE: votes.setdefault(int(j) - i, []).append(float(dist[j]))
    cands = [o for o, v in votes.items() if len(v) >= 2] or [o for o, v in votes.items() if len(v) == 1 and anchors == 1]
    best = None
    for off in cands:
        d = range_distance(dp, tp, d0, d1, off)
        if d is None: continue
        if best is None or d.mean() < best[1].mean(): best = (off, d)
    if best is None: return None
    off, d = best
    for delta in (-2, -1, 1, 2):            # a still shot lets several offsets pass the anchors; take the tightest
        e = range_distance(dp, tp, d0, d1, off + delta)
        if e is not None and e.mean() < d.mean() - 0.05: off, d = off + delta, e
    return {'offset': off, **judge(d)}


def same_at(dp, tp, t0, t1, off):
    """True when target frames [t0, t1) are the donor's frames at t - off (the target stretch exists in the donor film)."""
    if t1 <= t0: return True
    d = range_distance(dp, tp, t0 - off, t1 - off, off)
    return d is not None and judge(d)['ok']


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest='cmd', required=True)
    q = s.add_parser('match'); q.add_argument('donor'); q.add_argument('target'); q.add_argument('--range', required=True); q.add_argument('--cache')
    a = p.parse_args(argv)
    d0, d1 = (int(x) for x in a.range.split('-'))
    dp, tp = prints(a.donor, a.cache), prints(a.target, a.cache)
    print(json.dumps({'donor_frames': len(dp), 'target_frames': len(tp), 'match': locate(dp, tp, d0, d1)}, indent=1))


if __name__ == '__main__':
    main()
