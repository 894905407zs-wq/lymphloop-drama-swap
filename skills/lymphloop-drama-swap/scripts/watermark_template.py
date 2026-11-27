"""Floating watermark by a stored template instead of OCR (user 2026-11-21: "扫水印这件事太慢了 ... 把 skill 放到云端上跑，
都非常占 CPU"; then "固定位置+有水印才盖").

Every LYMPHORIA.CO film scanned so far (15 films, three Codex batches and our own runs) carries the same mark in the same
six places of the 720x1280 picture, within 3 px: one vertical droplet+LYMPHORIA in each corner and one horizontal one in
the upper left and lower right of the middle. The mark is a fixed semi-transparent picture, so whether it is on screen
in a frame is a template match of its strokes, not a reading:

  1. decode the film once (grey, 720x1280),
  2. per place: high-pass the small search window and match it against the stored stroke template (normalised
     cross-correlation, +-SEARCH px for the film's own drift),
  3. per place: frames scoring >= HI are the mark; runs are grown over neighbours >= LO (its fade in/out), short gaps
     bridged and PAD frames added.

No OCR, no Vision, no GPU: plain numpy/OpenCV, so it runs the same on a Linux server. 1506620285858447 (10 min,
18,682 frames, 6 places): 23 s against the every-frame OCR scan's ~6,300 reads per place pass; every OCR-read frame
scored 0.36-0.97, every frame far from a read scored <= 0.16.

The output is an ordinary scan.json (floating_overlay detections in the fixed boxes, `positions` with orientation), so
`watermark apply`, `brand` and `final-screen` work unchanged. A film whose picture does not carry this mark (no place
scores HI anywhere) gets `None` and the caller falls back to the OCR scan.
"""
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import json, subprocess, sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media

TEMPLATE_DIR = HERE.parent/'assets/watermarks'
REF_W, REF_H = 720, 1280
SIGMA = 3.0          # high-pass: picture minus its Gaussian blur, so the background drops out and the strokes stay
SEARCH = 6           # px the film's mark may sit off the stored place (15 films: within 3 px)
MARGIN = 12          # extra px read around the search window so the blur has context at the window edge
HI = 0.30            # a frame scoring this is the mark (calibrated: OCR reads >= 0.36, unmarked frames <= 0.16)
LO = 0.20            # neighbours of a HI run scoring this are its fade in/out
GAP = 8              # frames: a shorter dip inside one appearance is bridged
PAD = 6              # frames added on each side of a run: the mark comes and goes on a hard cut, and its first 1-3
                     # frames score 0.10-0.16 although it is visible (3328544050673750, six bursts looked at)
MIN_FRAMES = 12      # the film carries the mark only if some place has at least this many HI frames


def available(target):
    """The stored template for this watermark word, or None."""
    if not target: return None
    meta = TEMPLATE_DIR/f'{target.lower()}-fixed.json'
    return meta if meta.is_file() and meta.with_suffix('.npz').is_file() else None


def load(meta_path):
    meta = json.loads(Path(meta_path).read_text())
    arrays = np.load(Path(meta_path).with_suffix('.npz'))
    for p in meta['places']: p['template'] = arrays[f"place{p['id']}"].astype(np.float32)
    return meta


def _highpass(img):
    import cv2
    img = img.astype(np.float32)
    return img - cv2.GaussianBlur(img, (0, 0), SIGMA)


def _window(box, extra=SEARCH + MARGIN):
    x, y, bw, bh = box
    return x - extra, y - extra, x + bw + extra, y + bh + extra


def _crop(img, x0, y0, x1, y1):
    """img[y0:y1, x0:x1] with the part outside the picture mirrored in, so a corner place keeps its full window."""
    import cv2
    h, w = img.shape
    c = img[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    l, t, r, b = max(0, -x0), max(0, -y0), max(0, x1 - w), max(0, y1 - h)
    return cv2.copyMakeBorder(c, t, b, l, r, cv2.BORDER_REFLECT) if l or t or r or b else c


def _frames(video, scale):
    """Grey frames at REF_W x REF_H, one at a time."""
    vf = f'scale={REF_W}:{REF_H}:flags=area,format=gray' if scale else 'format=gray'
    proc = subprocess.Popen([media.binary('ffmpeg'), '-v', 'error', '-i', str(video), '-map', '0:v:0', '-vf', vf,
                             '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], stdout=subprocess.PIPE, bufsize=REF_W * REF_H * 4)
    size = REF_W * REF_H
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size: break
            yield np.frombuffer(buf, np.uint8).reshape(REF_H, REF_W)
    finally:
        proc.stdout.close(); proc.wait()


def geometry(video):
    """(width, height, needs_scale) or None when the picture is not 9:16."""
    info = media.probe(str(video))
    w, h = int(info['video']['width']), int(info['video']['height'])
    if abs(w / h - REF_W / REF_H) > 0.01: return None
    return w, h, (w, h) != (REF_W, REF_H)


def scores(video, meta):
    """Per frame and place: the best match score and where (x, y of the template's top-left, REF pixels)."""
    import cv2
    geo = geometry(video)
    if not geo: return None
    places = meta['places']
    wins = [_window(p['box']) for p in places]
    s, at = [], []
    for img in _frames(video, geo[2]):
        row, pos = [], []
        for p, (x0, y0, x1, y1) in zip(places, wins):
            hp = _highpass(_crop(img, x0, y0, x1, y1))[MARGIN:-MARGIN, MARGIN:-MARGIN]
            r = cv2.matchTemplate(hp, p['template'], cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            row.append(mx); pos.append((x0 + MARGIN + loc[0], y0 + MARGIN + loc[1]))
        s.append(row); at.append(pos)
    return np.array(s, np.float32), np.array(at, np.int32)


def runs(col, hi=HI, lo=LO, gap=GAP, pad=PAD):
    """Frames of one place that carry the mark: HI seeds grown over LO neighbours, gaps <= gap bridged, padded."""
    n = len(col); on = np.zeros(n, bool)
    seeds = np.flatnonzero(col >= hi)
    if not len(seeds): return on
    low = col >= lo
    for f in seeds:
        if on[f]: continue
        a = f
        while a > 0 and low[a - 1]: a -= 1
        b = f
        while b + 1 < n and low[b + 1]: b += 1
        on[a:b + 1] = True
    idx = np.flatnonzero(on)
    for a, b in zip(idx[:-1], idx[1:]):
        if 1 < b - a <= gap + 1: on[a:b] = True
    out = on.copy()
    for f in np.flatnonzero(on):
        out[max(0, f - pad):min(n, f + pad + 1)] = True
    return out


def scan(video, target, out_dir=None, require=True):
    """scan.json-shaped dict for `video`, or None when the stored template does not apply to it. require=False (a short
    reference clip): no minimum number of marked frames, only the picture must be 9:16."""
    meta_path = available(target)
    if not meta_path: return None
    meta = load(meta_path)
    geo = geometry(video)
    if not geo: return None
    W, H, _ = geo
    sx, sy = W / REF_W, H / REF_H
    got = scores(video, meta)
    if got is None: return None
    s, at = got
    n = len(s)
    if not n or (require and int((s >= HI).sum(axis=0).max()) < MIN_FRAMES): return None
    detections, positions, stats = [], [], []
    for i, p in enumerate(meta['places']):
        col = s[:, i]; on = runs(col)
        hits = np.flatnonzero(col >= HI)
        # where this film's mark sits: the median match position over its clear frames, stored box if none
        if len(hits):
            ox, oy = np.median(at[hits, i, 0]), np.median(at[hits, i, 1])
            tx, ty = p['box'][0] - p['tpl_offset'][0], p['box'][1] - p['tpl_offset'][1]
            dx, dy = int(round(ox - tx)), int(round(oy - ty))
        else:
            dx = dy = 0
        bx, by, bw, bh = p['box'][0] + dx, p['box'][1] + dy, p['box'][2], p['box'][3]
        box = [int(round(bx * sx)), int(round(by * sy)), int(round(bw * sx)), int(round(bh * sy))]
        box[0], box[1] = max(0, box[0]), max(0, box[1]); box[2], box[3] = min(W - box[0], box[2]), min(H - box[1], box[3])
        frames = np.flatnonzero(on)
        bursts = int(np.count_nonzero(np.diff(np.r_[0, on.astype(np.int8)]) == 1))
        for f in frames.tolist():
            detections.append({'frame': f, 'bbox': box, 'text': target, 'classification': 'floating_overlay', 'icon_box': True,
                               'orient': p['orient'], 'pass': 'template', 'score': round(float(col[f]), 3),
                               **({'synthetic': 'template_fade'} if col[f] < HI else {})})
        positions.append({'id': p['id'], 'box': box, 'vertical': p['vertical'], 'orient': p['orient'], 'frames': int(len(frames)),
                          'bursts': bursts, 'first': int(frames[0]) if len(frames) else None, 'last': int(frames[-1]) if len(frames) else None,
                          'measured': box, 'size': 'template', 'kind': 'floating_watermark', 'offset_px': [dx, dy]})
        stats.append({'place': p['id'], 'hi_frames': int(len(hits)), 'frames': int(len(frames)), 'bursts': bursts,
                      'score_p50_on': round(float(np.median(col[hits])), 3) if len(hits) else None,
                      'score_p99_off': round(float(np.percentile(col[~on], 99)), 3) if (~on).any() else None})
    out = {'detections': detections, 'positions': positions, 'scanned_frames': n, 'coverage': 'every_frame',
           'method': 'template', 'template': {'meta': str(meta_path), 'name': meta.get('name'), 'hi': HI, 'lo': LO, 'gap': GAP, 'pad': PAD,
                                              'search_px': SEARCH, 'places': stats}}
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        np.save(Path(out_dir)/'template_scores.npy', s)
    return out


def left(video, target, stamped, out_dir=None):
    """final-screen: master frames where the stored mark still shows outside the frames brand stamped at that place.
    `stamped` is {place index: set of master frames}. None when no template applies."""
    meta_path = available(target)
    if not meta_path: return None
    meta = load(meta_path)
    geo = geometry(video)
    got = scores(video, meta) if geo else None
    if got is None: return None
    s, at = got
    W, H, _ = geo; sx, sy = W / REF_W, H / REF_H
    dets = []
    for i, p in enumerate(meta['places']):
        hits = np.flatnonzero(s[:, i] >= HI)
        for f in hits.tolist():
            if f not in stamped.get(i, ()):
                x = at[f, i, 0] + p['tpl_offset'][0]; y = at[f, i, 1] + p['tpl_offset'][1]       # the mark's box in this frame
                box = [max(0, int(round(x * sx))), max(0, int(round(y * sy))), int(round(p['box'][2] * sx)), int(round(p['box'][3] * sy))]
                box[2], box[3] = min(W - box[0], box[2]), min(H - box[1], box[3])
                dets.append({'frame': f, 'bbox': box, 'text': target, 'pass': 'final_template', 'score': round(float(s[f, i]), 3)})
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True); np.save(Path(out_dir)/'template_scores.npy', s)
    return dets


def build(films, name, target, out_dir=TEMPLATE_DIR):
    """Stored template from films already scanned by OCR: [(video, scan.json)]. Each place's template is the mean
    high-passed picture of its box over the frames OCR read the mark in (the background averages away)."""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    boxes, orient, sums, counts = None, None, None, None
    for video, scan_path in films:
        d = json.loads(Path(scan_path).read_text())
        pos = sorted(d['positions'], key=lambda p: (p['box'][1] > REF_H / 2, p['box'][0]))
        if boxes is None:
            boxes = [list(p['box']) for p in pos]
            orient = [p.get('orient') or ('v_down' if p['box'][3] > 1.6 * p['box'][2] else 'h') for p in pos]
            sums = [np.zeros((b[3] + 2, b[2] + 2), np.float64) for b in boxes]; counts = [0] * len(boxes)
        own = [list(p['box']) for p in pos]
        read = [set() for _ in own]
        for det in d['detections']:
            if det.get('classification') != 'floating_overlay' or det.get('synthetic'): continue
            for i, b in enumerate(own):
                x, y, w, h = det['bbox']
                ix = max(0, min(x + w, b[0] + b[2]) - max(x, b[0])); iy = max(0, min(y + h, b[1] + b[3]) - max(y, b[1]))
                if ix * iy > 0.3 * min(w * h, b[2] * b[3]): read[i].add(det['frame'])
        for f, img in enumerate(_frames(video, geometry(video)[2])):
            for i, b in enumerate(own):
                if f in read[i]:
                    x0, y0 = b[0] - 1 - MARGIN, b[1] - 1 - MARGIN
                    crop = _crop(img, x0, y0, x0 + b[2] + 2 + 2 * MARGIN, y0 + b[3] + 2 + 2 * MARGIN)
                    sums[i] += _highpass(crop)[MARGIN:-MARGIN, MARGIN:-MARGIN]; counts[i] += 1
    arrays, places = {}, []
    for i, b in enumerate(boxes):
        arrays[f'place{i}'] = (sums[i] / max(1, counts[i])).astype(np.float32)
        places.append({'id': i, 'box': b, 'vertical': b[3] > 1.6 * b[2], 'orient': orient[i], 'tpl_offset': [1, 1], 'frames_averaged': counts[i]})
    np.savez_compressed(out_dir/f'{target.lower()}-fixed.npz', **arrays)
    meta = {'name': name, 'target': target, 'ref_size': [REF_W, REF_H], 'places': places,
            'built_from': [str(Path(v).name) for v, _ in films], 'note': __doc__.split('\n\n')[1]}
    (out_dir/f'{target.lower()}-fixed.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build'); b.add_argument('--film', nargs=2, action='append', metavar=('VIDEO', 'SCAN_JSON'), required=True)
    b.add_argument('--name', required=True); b.add_argument('--target', required=True)
    s = sub.add_parser('scan'); s.add_argument('video'); s.add_argument('--target', required=True); s.add_argument('--out')
    a = ap.parse_args()
    if a.cmd == 'build':
        m = build([tuple(x) for x in a.film], a.name, a.target.upper())
        print(json.dumps({k: v for k, v in m.items() if k != 'note'}, ensure_ascii=False, indent=1))
    else:
        r = scan(a.video, a.target.upper())
        if r is None: print('template does not apply'); sys.exit(3)
        if a.out: Path(a.out).write_text(json.dumps(r, ensure_ascii=False))
        print(json.dumps({'frames_with_mark': len({d['frame'] for d in r['detections']}), **r['template']}, ensure_ascii=False, indent=1))
