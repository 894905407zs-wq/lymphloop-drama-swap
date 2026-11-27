#!/usr/bin/env python3
"""The floating LYMPHORIA watermark: find every frame of it, and stamp a white LYMPHLOOP over its footprint.

User rule (2026-10-25): the original watermark is a droplet icon + LYMPHORIA; after it is cleaned, the SAME place in the
finished film gets LYMPHLOOP in opaque white, same position, same size, same orientation, covering the icon and the word.

Why the edge pass (2026-10-25, batch 295-319): the watermark is often vertical, tiny (360 x 640 sources) and faint in a
corner; full-frame OCR read it as LYMPHORTA / LYMRHORIA / MPHORIA (fixed by the fuzzy match) or not at all (305 at 36.7 s).
Cropping each edge strip, turning it upright and enlarging it 4x reads it ("@ LYMPHORIA"). Seven of the batch's films were
delivered with watermark frames the scan had missed.

  watermark_brand.py edge-scan SOURCE OUT_JSON            # extra detections from the edge strips (merged by swap.py)
  watermark_brand.py plan SCAN_JSON OUT_JSON               # per-frame stamp boxes and orientation
  watermark_brand.py stamp MASTER PLAN_JSON OUT_MP4 RECEIPT_JSON
"""
from __future__ import annotations
import argparse, hashlib, json, re, subprocess, sys, time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_pipeline as media  # noqa: E402

TARGET = 'LYMPHORIA'                                     # the floating watermark word; set_target() for other competitors
FUZZY = re.compile(r'(PHOR|HORI|RHOR|MPHO(?!O))')        # on the upper-cased letters of an OCR line
CURSIVE = re.compile(r'L[yу]')                          # the LymphLoop logo on the pack reads "LyMPHOR"
STEP = 3                                                 # edge pass on every 3rd frame; swap.py's fade padding bridges the rest
EDGE_WORKERS = 4
SCAN_WORKERS = 4                                         # processes OCR-ing disjoint frame ranges at once (user 2026-11-19: 15 min -> ~2 min)
SPARSE_STEP = 4                                          # first pass: every 4th frame; a mark is on screen for seconds
DENSE_PAD = 12                                           # then every frame within this many frames of a sparse hit
TEXT = 'LYMPHLOOP'
FONTS = ['/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Helvetica.ttc', '/Library/Fonts/Arial.ttf']


def set_target(target):
    """Point every matcher here at another competitor's watermark word (2026-11-11: any competitor). The fuzzy pattern is
    built by competitor.fuzzy_pattern: 4-letter pieces of the word that are not pieces of LYMPHLOOP/YESNAP."""
    global TARGET, FUZZY
    import competitor
    TARGET = re.sub(r'[^A-Z]', '', (target or 'LYMPHORIA').upper()) or 'LYMPHORIA'
    FUZZY = re.compile(competitor.fuzzy_pattern(TARGET))
    return FUZZY.pattern


def caption_line(text):
    """An OCR line that has other words besides the watermark ("LYMPHORIA 4 HERBS") is a caption or sign that names the
    competitor: watermark cleanup still removes it, but it is not the floating mark and gets no LYMPHLOOP stamp."""
    words = re.findall(r'[A-Za-z]{2,}', text or '')
    return any(not is_watermark_text(w) and w.upper() not in TARGET for w in words)   # 'LYM PHORIA' is one split mark


def is_watermark_text(text):
    letters = ''.join(c for c in (text or '').upper() if c.isalpha())
    return bool(FUZZY.search(letters)) and not CURSIVE.search(text or '')


# ---------------------------------------------------------------- edge pass
def edge_variants(width, height):
    """(name, crop x, y, w, h, transpose, contrast). transpose 1 = 90 deg clockwise (text that reads bottom-to-top becomes
    upright), 2 = counter-clockwise (top-to-bottom). Side strips for vertical text, top/bottom bands for horizontal."""
    sw, bh = max(16, int(width * 0.2) // 2 * 2), max(16, int(height * 0.15) // 2 * 2)
    out = []
    for side, x in (('left', 0), ('right', width - sw)):
        for t, d in ((1, 'cw'), (2, 'ccw')):
            for c in (False, True): out.append((f'{side}_{d}{"_c" if c else ""}', x, 0, sw, height, t, c))
    for band, y in (('top', 0), ('bottom', height - bh)):
        for c in (False, True): out.append((f'{band}{"_c" if c else ""}', 0, y, width, bh, None, c))
    return out


def _to_crop(x, y, t, cw, ch):
    """A point of the transposed crop back to crop coordinates (ffmpeg transpose=1: X = ch-1-cy, Y = cx; =2: X = cy, Y = cw-1-cx)."""
    if t == 1: return y, ch - 1 - x
    if t == 2: return cw - 1 - y, x
    return x, y


def map_box(box, variant, k):
    """bbox [x, y, w, h] found in the enlarged, transposed strip -> [x, y, w, h] in the source frame."""
    _, x0, y0, cw, ch, t, _ = variant
    bx, by, bw, bh = (v / k for v in box)
    pts = [_to_crop(px, py, t, cw, ch) for px, py in ((bx, by), (bx + bw, by), (bx, by + bh), (bx + bw, by + bh))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [int(x0 + min(xs)), int(y0 + min(ys)), int(round(max(xs) - min(xs))), int(round(max(ys) - min(ys)))]


def _sha(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()


def with_icon(det):
    """Main-scan (full-frame) box -> box that also covers the droplet icon. Horizontal lines grow on the left; a vertical
    line's reading direction is unknown there, so it grows at both ends."""
    x, y, w, h = det['bbox']; text = det.get('text', '')
    if text and not text.strip()[:1].isalnum(): return [x, y, w, h]
    if w >= h: return lead_icon([x, y, w, h], text)
    g = int(round(w * 1.1)); return [x, max(0, y - g), w, h + min(y, g) + g]


def lead_icon(box, text):
    """OCR often drops the droplet icon in front of the word: extend the line box by one text-height on its leading side
    (left, in upright reading coordinates) unless the line already starts with a symbol (@, ©, •, &)."""
    if text and not text.strip()[:1].isalnum(): return box
    x, y, w, h = box
    grow = int(round(h * 1.1))
    return [max(0, x - grow), y, w + min(x, grow), h]


def split_ranges(ranges, parts):
    """[(a, b)] end-exclusive frame ranges cut into about `parts` pieces of equal length."""
    total = sum(b - a for a, b in ranges)
    if total <= 0: return []
    size = max(1, -(-total // parts)); out = []
    for a, b in ranges:
        while a < b:
            out.append((a, min(b, a + size))); a += size
    return out


def merge_ranges(ranges, n_frames=None):
    out = []
    for a, b in sorted((max(0, a), b if n_frames is None else min(n_frames, b)) for a, b in ranges if b > a):
        if out and a <= out[-1][1]: out[-1] = (out[-1][0], max(out[-1][1], b))
        else: out.append((a, b))
    return out


def range_groups(ranges, workers):
    """At most workers decode passes, even for hundreds of short windows.

    Each group stays in source order and preserves every original frame range.
    One job per disconnected window decoded the whole source hundreds of times
    in position_scan's dense pass.
    """
    chunks = split_ranges(ranges, max(1, workers))
    bins = [[] for _ in range(min(max(1, workers), len(chunks)))]
    sizes = [0] * len(bins)
    for a, b in chunks:
        k = min(range(len(bins)), key=lambda i: sizes[i])
        bins[k].append((a, b)); sizes[k] += b - a
    return [sorted(group) for group in bins]


def fast_scan(binary, source, work, sha256, pattern, target, n_frames, ranges=None, step=SPARSE_STEP, pad=DENSE_PAD, workers=SCAN_WORKERS):
    """Fuzzy watermark OCR (scan_watermark.swift) over `ranges` (default the whole video): every `step`-th frame in
    `workers` processes at once, then every frame within `pad` frames of a hit. Returns the merged scan record."""
    from concurrent.futures import ThreadPoolExecutor
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    ranges = merge_ranges(ranges or [(0, n_frames)], n_frames)

    def run_pass(name, rngs, st):
        jobs = split_ranges(rngs, workers)
        def one(k):
            out = work/f'{name}-{k}.json'
            subprocess.run([str(binary), str(source), str(out), sha256, pattern, target, str(st), ','.join(f'{a}-{b}' for a, b in [jobs[k]])],
                           check=True, capture_output=True)
            return json.loads(out.read_text())
        with ThreadPoolExecutor(max(1, min(workers, len(jobs)))) as ex:
            return list(ex.map(one, range(len(jobs))))

    sparse = run_pass('sparse', ranges, step) if step > 1 else []
    hits = sorted({d['frame'] for r in sparse for d in r['detections']})
    dense_ranges = ranges if step <= 1 else merge_ranges([(f - pad, f + pad + 1) for f in hits], n_frames)
    dense = run_pass('dense', dense_ranges, 1) if dense_ranges else []
    seen = set(); dets = []
    for r in dense + sparse:
        for d in r['detections']:
            key = (d['frame'], tuple(d['bbox']))
            if key not in seen: seen.add(key); dets.append(d)
    scanned = sum(r.get('scanned_frames', 0) for r in sparse + dense)
    return {'type': 'lymphloop_watermark_scan', 'detections': sorted(dets, key=lambda d: d['frame']), 'frame_count': n_frames,
            'scanned_frames': scanned, 'coverage': f'every {step}th frame of {ranges}, every frame within {pad} of a hit',
            'sparse_hits': len(hits), 'dense_ranges': dense_ranges}


def edge_scan(source, out_json, work=None, step=STEP, ranges=None):
    import caption_clean
    source = Path(source).resolve(); work = Path(work or Path(out_json).parent/'edge_work'); work.mkdir(parents=True, exist_ok=True)
    info = media.probe(str(source)); W, H = int(info['video']['width']), int(info['video']['height'])
    scanner = caption_clean.scanner(work); ff = media.binary('ffmpeg')
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    scanner_sha = hashlib.sha256(Path(scanner).read_bytes()).hexdigest()

    def one(v):
        name, x0, y0, cw, ch, t, c = v; found = []
        long_side = max(cw, ch); k = max(1.0, min(4.0, 2600 / long_side))
        sel = f"not(mod(n\\,{step}))" if not ranges else '(' + '+'.join(f'between(n\\,{a}\\,{b - 1})' for a, b in ranges) + f")*not(mod(n\\,{step}))"
        vf = [f"select='{sel}'", f'crop={cw}:{ch}:{x0}:{y0}']
        if t: vf.append(f'transpose={t}')
        vf.append(f'scale=trunc(iw*{k:.4f}/2)*2:trunc(ih*{k:.4f}/2)*2:flags=lanczos')   # even sides: libx264 refused 2599x292 on a 720-wide clip (2026-11-11)
        if c: vf.append('eq=contrast=2.2:brightness=-0.05')
        clip = work/f'{name}.mp4'; scan = work/f'{name}.json'
        meta = work/f'{name}.cache.json'
        key = {'source_sha256': source_sha, 'filter': vf, 'ranges': ranges,
               'scanner_binary_sha256': scanner_sha,
               'expected_frames': len(picked), 'v': 1}
        cached = False
        if meta.is_file() and scan.is_file() and clip.is_file():
            old = json.loads(meta.read_text())
            cached = (old.get('key') == key and old.get('clip_sha256') == hashlib.sha256(clip.read_bytes()).hexdigest()
                      and old.get('scan_sha256') == hashlib.sha256(scan.read_bytes()).hexdigest()
                      and json.loads(scan.read_text()).get('frame_count') == len(picked))
        if not cached:
            subprocess.run([ff, '-v', 'error', '-y', '-i', str(source), '-an', '-vf', ','.join(vf), '-fps_mode', 'passthrough',
                            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '12', str(clip)], check=True)
            for attempt in range(3):
                try:
                    subprocess.run([str(scanner), str(clip), str(scan)], check=True, capture_output=True)
                    if json.loads(scan.read_text()).get('frame_count') != len(picked):
                        raise RuntimeError(f'Incomplete edge scan {name}: expected {len(picked)} frames')
                    break
                except (subprocess.CalledProcessError, RuntimeError):
                    if attempt == 2: raise
                    time.sleep(2*(attempt+1))
            record = {'key': key, 'clip_sha256': hashlib.sha256(clip.read_bytes()).hexdigest(),
                      'scan_sha256': hashlib.sha256(scan.read_bytes()).hexdigest()}
            tmp = meta.with_suffix('.tmp'); tmp.write_text(json.dumps(record)); tmp.replace(meta)
        for d in json.loads(scan.read_text())['detections']:
            if not is_watermark_text(d['text']): continue
            # the icon sits on the left of an upright line; a box taller than wide here is vertical text read sideways
            box = lead_icon(d['bbox'], d['text']) if d['bbox'][2] >= d['bbox'][3] else d['bbox']
            if d['frame'] >= len(picked): continue
            found.append({'frame': picked[d['frame']], 'text': d['text'], 'confidence': d.get('confidence', 0.0), 'bbox': map_box(box, v, k), 'pass': name})
        return found

    n_frames = int(media.probe(str(source), include_frames=True)['frame_count'])
    picked = [n for n in range(n_frames) if n % step == 0 and (not ranges or any(a <= n < b for a, b in ranges))]
    if not picked:
        rec = {'type': 'lymphloop_watermark_edge_scan', 'source': str(source), 'step': step, 'detections': [], 'variants': [], 'ranges': ranges}
        Path(out_json).write_text(json.dumps(rec, indent=1)); return rec
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(EDGE_WORKERS) as ex:   # each variant is an ffmpeg + OCR process pair; 3 at once roughly triples throughput
        dets = [d for found in ex.map(one, edge_variants(W, H)) for d in found]
    rec = {'type': 'lymphloop_watermark_edge_scan', 'source': str(source), 'step': step, 'width': W, 'height': H, 'ranges': ranges,
           'variants': [v[0] for v in edge_variants(W, H)], 'detections': dets}
    Path(out_json).write_text(json.dumps(rec, indent=1)); return rec


# ---------------------------------------------------------------- stamp plan
def _iou(a, b):
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0, min(ax2, bx2) - max(a[0], b[0])), max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih; union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def _median_box(boxes):
    a = np.array([[b[0], b[1], b[0] + b[2], b[1] + b[3]] for b in boxes], dtype=float)
    x0, y0, x1, y1 = (int(round(v)) for v in np.median(a, axis=0))
    return [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]


def _union(boxes):
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    return [x0, y0, max(b[0] + b[2] for b in boxes) - x0, max(b[1] + b[3] for b in boxes) - y0]


def plan(scan, width=None, height=None, smooth=4):
    """floating_overlay boxes -> per-frame stamps {frame: [{'box', 'orient'}]}. Overlapping boxes of one frame are merged
    (full-frame and edge passes see the same mark), frames are linked into tracks, each track's box is median-smoothed
    over +-smooth frames (OCR boxes jitter) and gets one orientation: what the edge pass saw, else the box shape."""
    captions = {}
    for d in scan.get('detections', []):
        if d.get('classification') == 'floating_overlay' and not d.get('synthetic') and caption_line(d.get('text', '')):
            captions.setdefault(d['frame'], []).append(d['bbox'])
    def near_caption(d):          # the caption line itself, or a fade-pad copy of it
        return any(_iou(d['bbox'], c) > 0.3 for g in range(d['frame'] - 12, d['frame'] + 13) for c in captions.get(g, ()))
    by_frame = {}
    for d in scan.get('detections', []):
        if d.get('classification') != 'floating_overlay' or (captions and near_caption(d)): continue
        by_frame.setdefault(d['frame'], []).append(d)
    merged = {}
    for f, ds in by_frame.items():
        groups = []
        for d in ds:
            for g in groups:
                if any(_iou(d['bbox'], e['bbox']) > 0.05 for e in g): g.append(d); break
            else: groups.append([d])
        # median of the passes' edges, not their union: one pass's loose box made the stamp run past the mark (305, 127 s)
        merged[f] = [{'box': _median_box([e['bbox'] for e in g]), 'orients': [e['orient'] for e in g if e.get('orient')]} for g in groups]   # orient: agent override only
    tracks = []
    for f in sorted(merged):
        for m in merged[f]:
            best = None
            for tr in tracks:
                lf, lb = tr['frames'][-1], tr['boxes'][-1]
                if 0 < f - lf <= 6 and _iou(m['box'], lb) > 0.2 and (best is None or _iou(m['box'], lb) > _iou(m['box'], best['boxes'][-1])): best = tr
            if best: best['frames'].append(f); best['boxes'].append(m['box']); best['orients'] += m['orients']
            else: tracks.append({'frames': [f], 'boxes': [m['box']], 'orients': list(m['orients'])})
    stamps = {}; summary = []
    for tid, tr in enumerate(tracks):
        arr = np.array(tr['boxes'], dtype=float)
        known = [o for o in tr['orients'] if o in ('v_up', 'v_down', 'h')]
        w, h = np.median(arr[:, 2]), np.median(arr[:, 3])
        vertical = h > 1.6 * w
        orient = max(set(known), key=known.count) if known else ('v_down' if vertical else 'h')   # vertical: orient_tracks() asks Flash
        mid = tr['frames'][len(tr['frames']) // 2]
        summary.append({'id': tid, 'first': tr['frames'][0], 'last': tr['frames'][-1], 'mid': mid, 'frames': len(tr['frames']),
                        'box': [int(round(v)) for v in np.median(arr, axis=0)], 'vertical': bool(vertical), 'orient': orient,
                        'orient_source': 'override' if known else ('pending' if vertical else 'horizontal')})
        for i, f in enumerate(tr['frames']):
            lo, hi = max(0, i - smooth), min(len(arr), i + smooth + 1)
            box = [int(round(v)) for v in np.median(arr[lo:hi], axis=0)]
            if width and height:
                box[0], box[1] = max(0, box[0]), max(0, box[1]); box[2], box[3] = min(width - box[0], box[2]), min(height - box[1], box[3])
            if box[2] > 1 and box[3] > 1: stamps.setdefault(f, []).append({'box': box, 'orient': orient, 'track': tid})
    return {'type': 'lymphloop_brand_plan', 'text': TEXT, 'color': 'white', 'opacity': 1.0, 'tracks': len(tracks), 'track_list': summary,
            'frames': len(stamps), 'stamps': {str(f): v for f, v in sorted(stamps.items())}}


MARK_MAX = 0.028      # of the frame height: the thickest a floating watermark line gets


def mark_position(box, width, height):
    """Can a floating watermark sit here: outside caption position and no thicker than a watermark line."""
    import caption_clean
    return not caption_clean.is_caption(box, width, height) and min(box[2], box[3]) <= MARK_MAX * height


def remap_source_stamps(plan_rec, timeline):
    """Move source watermark boxes onto the recut's frame grid. On a GENERATED picture only boxes where a floating
    watermark can sit are carried over: the scan also holds the brand word printed on the bottle and shown alone in a
    caption, and with the bottle and the caption gone those boxes stamped LYMPHLOOP across the middle of the new picture
    (Lymphoria reference-mode master, 2026-11-12: on a chest, over the carton front)."""
    stamps = {int(frame): boxes for frame, boxes in plan_rec['stamps'].items()}
    width, height = timeline.get('width'), timeline.get('height')
    detections = []
    for segment in timeline['segments']:
        source_in, source_out = segment['source_in'], segment['source_out']
        out_in, out_out = segment['out_in'], segment['out_out']
        source_length, out_length = source_out - source_in, out_out - out_in
        generated = segment.get('picture_id') is not None and bool(width and height)
        for frame in range(out_in, out_out):
            source_frame = source_in + (frame - out_in) * source_length // out_length
            detections.extend({'frame': frame, 'bbox': stamp['box'], 'orient': stamp['orient'],
                               'classification': 'floating_overlay'} for stamp in stamps.get(source_frame, ())
                              if not generated or mark_position(stamp['box'], width, height))
    return detections


DIRECTION_SCHEMA = {'type': 'OBJECT', 'properties': {
    'icon_end': {'type': 'STRING', 'enum': ['top', 'bottom', 'left', 'right', 'none']},
    'reads': {'type': 'STRING', 'enum': ['top_to_bottom', 'bottom_to_top', 'left_to_right', 'right_to_left']},
    'text': {'type': 'STRING'}}, 'required': ['icon_end', 'reads', 'text']}


def orient_tracks(plan_rec, source, run_dir, places=None, direction_cache=None):
    """Reading direction of every vertical track. Uses template/place orientation first; in Antigravity
    native mode saves enlarged crops for direct agent inspection instead of calling external Flash."""
    import agent_native
    work = Path(run_dir)/'watermark'/'direction'; work.mkdir(parents=True, exist_ok=True)
    ff = media.binary('ffmpeg'); fps = media.probe(str(source))['fps']
    def ask(tr, place=None):
        if place and place.get('orient') in ('v_up', 'v_down'):
            return {'orient': place['orient'], 'source': 'template_place'}
        x, y, w, h = tr['box']; frame = work/f"t{tr['id']}_f{tr['mid']}.png"
        subprocess.run([ff, '-v', 'error', '-y', '-ss', f"{tr['mid'] / float(fps):.4f}", '-i', str(source), '-frames:v', '1', str(frame)], check=True)
        from PIL import ImageOps
        im = Image.open(frame).convert('RGB'); pad = max(4, w)
        crop = im.crop((max(0, x - pad), max(0, y - pad), min(im.width, x + w + pad), min(im.height, y + h + pad)))
        k = max(1, int(600 / max(crop.size))); crop = ImageOps.autocontrast(crop, cutoff=1).resize((crop.width * k, crop.height * k), Image.LANCZOS)
        q = work/f"t{tr['id']}_crop.png"; crop.save(q)
        if agent_native.is_native():
            # Verified template default for LYMPHORIA vertical marks is v_down; crop is saved for agent check.
            return {'orient': tr.get('orient') if tr.get('orient') in ('v_up', 'v_down') else 'v_down',
                    'agent_crop': str(q), 'source': 'agent_native_default'}
        import flash_screen
        ans = flash_screen.call_flash([('text', f'This crop shows a small watermark: a round droplet/logo icon followed by the word {TARGET}. '
                                       'Where is the icon (top, bottom, left or right end), and in which direction does the word read? '
                                       'Answer in the JSON schema.'), ('image', str(q))], DIRECTION_SCHEMA, run_dir)['result']
        orient = {'top_to_bottom': 'v_down', 'bottom_to_top': 'v_up'}.get(ans.get('reads'))
        if not orient: orient = {'top': 'v_down', 'bottom': 'v_up'}.get(ans.get('icon_end'), tr['orient'])
        return {'orient': orient, 'flash': ans}

    # A fixed place has one reading direction within this film. The caller owns
    # this cache for its two brand passes; no state is shared across videos.
    cache = {} if direction_cache is None else direction_cache
    groups = {}
    place_by_key = {}
    for tr in plan_rec['track_list']:
        if not tr['vertical']: continue
        place = position_of(tr['box'], places or [])
        if place is None:
            if tr['orient_source'] != 'override':
                ans = ask(tr)
                tr.update(**ans, orient_source=ans.get('source', 'flash'))
            continue
        key = (str(Path(run_dir).resolve()), TARGET, tuple(place['box']))
        groups.setdefault(key, []).append(tr)
        place_by_key[key] = place
    for key, tracks in groups.items():
        pending = [t for t in tracks if t['orient_source'] != 'override']
        if not pending: continue
        overrides = {t['orient'] for t in tracks if t['orient_source'] == 'override' and t['orient'] in ('v_up', 'v_down')}
        representative = max(pending, key=lambda t: (t.get('frames', 0), t['box'][2] * t['box'][3], -t['id']))
        place = place_by_key.get(key)
        if len(overrides) == 1:
            answer = {'orient': next(iter(overrides))}
            origin = 'fixed_place_override'
        elif place and place.get('orient') in ('v_up', 'v_down'):
            answer = {'orient': place['orient']}
            origin = 'fixed_place_template'
        elif not overrides and key in cache:
            answer = cache[key]
            origin = 'fixed_place_cache'
        else:
            answer = ask(representative, place=place)
            origin = answer.get('source', 'flash')
        # Conflicting explicit directions stay untouched and are never cached.
        if len(overrides) <= 1: cache[key] = dict(answer)
        for tr in pending:
            tr.update(**answer, orient_source=origin if tr is representative else 'fixed_place_' + ('override' if len(overrides) == 1 else origin),
                      orient_place_box=list(key[2]), orient_representative_track=representative['id'])
    by = {tr['id']: tr['orient'] for tr in plan_rec['track_list']}
    for sts in plan_rec['stamps'].values():
        for st in sts: st['orient'] = by.get(st.get('track'), st['orient'])
    return plan_rec


# ---------------------------------------------------------------- fixed positions (user 2026-11-20)
# "整条片下来基本都是在同一个位置（四角竖向水印和偏中间的对角两个横向水印）" and "lymphloop 字样会忽大忽小，很丑，
# 所以我想要固定住它". Codex 30542865505302802: 12 bursts at 6 places; within one burst the stamp box jumped between
# 16x52 and 18x68 (OCR read part of the word on some frames, fade-pad frames are 4 px larger on each side) and two
# bursts at the same place were 16x69 and 16x43. Each place now gets ONE box for the whole film, and the later checks
# read only those places.
POSITION_OVERLAP = 0.3       # intersection over the smaller box: a partial read lies inside the full box
FULL_READ = 0.85             # a box at least this share of the place's long reads counts as a full read of the mark


def _overlap(a, b):
    iw = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])); ih = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return iw * ih / max(1, min(a[2] * a[3], b[2] * b[3]))


def _vertical(box): return box[3] > 1.6 * box[2]


def positions(detections, width=None, height=None):
    """The fixed places of the floating mark from real OCR reads (no fade-pad or gap-fill boxes, no caption lines).
    Each place: one box = the median of its full reads (partial reads -- shorter than FULL_READ of the long reads --
    are left out, so the box is the whole icon + word), vertical or not, how many frames and bursts it was read in."""
    clusters = []
    for d in sorted((d for d in detections if d.get('classification', 'floating_overlay') == 'floating_overlay'
                     and not d.get('synthetic') and not caption_line(d.get('text', ''))), key=lambda d: d['frame']):
        b = list(d['bbox']); v = _vertical(b)
        best = max((c for c in clusters if c['vertical'] == v and _overlap(b, c['ref']) > POSITION_OVERLAP),
                   key=lambda c: _overlap(b, c['ref']), default=None)
        if best is None: best = {'vertical': v, 'boxes': [], 'frames': set()}; clusters.append(best)
        best['boxes'].append(b); best['frames'].add(d['frame']); best['ref'] = _median_box(best['boxes'])
        best.setdefault('reads', []).append([d['frame'], *b])      # which reads make this place (drop_print_places)
        # size evidence: a full-frame read of a vertical line was grown at BOTH ends for the icon (with_icon: direction
        # unknown there), 16x90 against the real 10x61 (Codex 30542865505302802 at 9 s); edge-pass reads and reads that
        # already start with the icon (@, ©) are the true footprint
        if not v or d.get('pass') or not str(d.get('text', '')).strip()[:1].isalnum(): best.setdefault('sized', []).append(b)
    out = []
    for c in sorted(clusters, key=lambda c: -len(c['frames'])):
        boxes = c.get('sized') or c['boxes']
        arr = np.array(boxes, dtype=float); length = arr[:, 3] if c['vertical'] else arr[:, 2]
        full = [b for b, n in zip(boxes, length) if n >= FULL_READ * np.percentile(length, 90)]
        box = _median_box(full)
        if width and height:
            box[0], box[1] = max(0, min(width - 2, box[0])), max(0, min(height - 2, box[1]))
            box[2], box[3] = min(width - box[0], box[2]), min(height - box[1], box[3])
        fr = sorted(c['frames']); bursts = 1 + sum(1 for p, q in zip(fr, fr[1:]) if q - p > 30)
        out.append({'id': len(out), 'box': box, 'vertical': c['vertical'], 'frames': len(fr), 'bursts': bursts,
                    'first': fr[0], 'last': fr[-1], 'reads': c.get('reads', [])})
    return out



SAME_MARK = 0.2              # places whose measured length is within 20 % of the median are the same mark: one size


def measure_places(source, places, hits, samples=16):
    """Each place's true footprint from the pixels, not from OCR boxes (Vision's line box already held the droplet icon on
    Codex 30542865505302802 and lead_icon grew it 16 px further left, 85 px for a 65 px mark). The mark stands still
    while the picture behind it changes, so the edges present in (nearly) every frame the mark was read in ARE the
    mark: the 25th-percentile edge map over up to `samples` of those frames, thresholded, its pieces near the OCR box.
    `hits`: {place id: frames}. Then every place that is the same mark (length within SAME_MARK of the median) gets
    the same size, centred on its own footprint (user 2026-11-20: "固定住它"). Places too faint to measure keep their
    OCR box."""
    import cv2
    source = Path(source); info = media.probe(str(source)); W, H = int(info['video']['width']), int(info['video']['height'])
    ff = media.binary('ffmpeg'); k3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    requests, need, crops = {}, {}, {}
    for p in places:
        fr = sorted(hits.get(p['id']) or [])
        if len(fr) < 4: p['measured'] = None; continue
        pick = sorted({fr[int(i)] for i in np.linspace(0, len(fr) - 1, min(samples, len(fr)))})
        x, y, w, h = p['box']; mg = max(w, h) // 3
        x0, y0 = max(0, x - mg) // 2 * 2, max(0, y - mg) // 2 * 2
        cw, ch = (min(W, x + w + mg) - x0) // 2 * 2, (min(H, y + h + mg) - y0) // 2 * 2
        requests[p['id']] = (x0, y0, cw, ch); crops[p['id']] = []
        for f in pick: need.setdefault(f, []).append(p['id'])
    # Decode each selected source frame once for ALL places. Grayscale conversion
    # still precedes the exact same integer crops; only duplicate decoding goes.
    if need:
        pick = sorted(need)
        sel = '+'.join(f'eq(n\\,{f})' for f in pick)
        dec = subprocess.Popen([ff, '-v', 'error', '-i', str(source), '-vf', f"select='{sel}',format=gray",
                                '-frames:v', str(len(pick)), '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        complete = True
        for f in pick:
            raw = dec.stdout.read(W * H)
            if len(raw) != W * H: complete = False; break
            im = np.frombuffer(raw, np.uint8).reshape(H, W)
            for pid in need[f]:
                x0, y0, cw, ch = requests[pid]
                crops[pid].append(im[y0:y0 + ch, x0:x0 + cw].copy())
        dec.stdout.close(); error = dec.stderr.read(); code = dec.wait(); dec.stderr.close()
        if code or not complete:
            raise RuntimeError('Watermark size samples failed full decode: ' + error.decode(errors='replace')[-500:])
    for p in places:
        if p['id'] not in requests: continue
        x0, y0, cw, ch = requests[p['id']]
        grads = []
        for im in np.array(crops[p['id']], dtype=np.float32):
            g = np.hypot(cv2.Sobel(im, cv2.CV_32F, 1, 0), cv2.Sobel(im, cv2.CV_32F, 0, 1)); grads.append(g / (np.percentile(g, 99) + 1e-3))
        mn = cv2.GaussianBlur(np.percentile(np.array(grads), 25, axis=0), (3, 3), 0); mn /= max(1e-6, float(mn.max()))
        nl, _, st, _ = cv2.connectedComponentsWithStats(cv2.morphologyEx((mn > 0.3).astype(np.uint8), cv2.MORPH_CLOSE, k3))
        keep = [i for i in range(1, nl) if st[i, 4] >= 4 and _overlap([st[i, 0] + x0, st[i, 1] + y0, st[i, 2], st[i, 3]], p['box']) > POSITION_OVERLAP]
        if not keep: p['measured'] = None; continue
        bx0 = min(int(st[i, 0]) for i in keep); by0 = min(int(st[i, 1]) for i in keep)
        bx1 = max(int(st[i, 0] + st[i, 2]) for i in keep); by1 = max(int(st[i, 1] + st[i, 3]) for i in keep)
        fb = [x0 + bx0, y0 + by0, bx1 - bx0, by1 - by0]
        length, thick = (fb[3], fb[2]) if p['vertical'] else (fb[2], fb[3])
        # a sane mark: a line, not a blob of background, and not far off the OCR read
        p['measured'] = fb if length >= 2.5 * thick and _overlap(fb, p['box']) > 0.5 else None
    sizes = [((m[3], m[2]) if p['vertical'] else (m[2], m[3])) for p in places if (m := p.get('measured'))]
    if sizes:
        L = float(np.median([s[0] for s in sizes])); T = float(np.median([s[1] for s in sizes]))
        for p in places:
            m = p.get('measured')
            if not m: continue
            length = m[3] if p['vertical'] else m[2]
            if abs(length - L) > SAME_MARK * L: p['box'] = m; p['size'] = 'measured'; continue
            bw, bh = (T, L) if p['vertical'] else (L, T)
            cx, cy = m[0] + m[2] / 2, m[1] + m[3] / 2
            bx, by = int(round(cx - bw / 2)), int(round(cy - bh / 2)); bw, bh = int(round(bw)), int(round(bh))
            bx, by = max(0, min(W - bw, bx)), max(0, min(H - bh, by))
            p['box'] = [bx, by, bw, bh]; p['size'] = 'shared'
    return places


def find_places(detections, source, width=None, height=None):
    """positions() + measure_places(): the fixed places of the mark with their pixel-measured, shared-size boxes."""
    places = positions(detections, width, height); hits = {}
    for d in detections:
        if d.get('classification', 'floating_overlay') == 'floating_overlay' and not d.get('synthetic') and not caption_line(d.get('text', '')):
            p = position_of(d['bbox'], places)
            if p: hits.setdefault(p['id'], set()).add(d['frame'])
    return measure_places(source, places, hits) if source else places


PLACE_KIND_SCHEMA = {'type': 'OBJECT', 'properties': {
    'kind': {'type': 'STRING', 'enum': ['floating_watermark', 'product_print', 'caption_or_sign', 'unsure']},
    'reason': {'type': 'STRING'}}, 'required': ['kind', 'reason']}


def place_hits(detections, places):
    """{place id: sorted frames} of the real OCR reads at each place."""
    hits = {}
    for d in detections:
        if d.get('classification', 'floating_overlay') == 'floating_overlay' and not d.get('synthetic') and not caption_line(d.get('text', '')):
            p = position_of(d['bbox'], places)
            if p: hits.setdefault(p['id'], set()).add(d['frame'])
    return {k: sorted(v) for k, v in hits.items()}


def classify_places(places, source, detections, run_dir, samples=3):
    """Ask Flash, per place, whether the brand word read there is a floating watermark laid over the picture or print on
    a physical thing in the scene (a bottle label, a carton, a sign). The scan matches the brand word anywhere, so the
    competitor's own label in a product close-up became a "place" (user 2026-11-21: "扫描水印的时候一直把产品标签当成
    水印"; Codex batch_20261120: three of eight films' candidates were all bottle labels, ruled out by hand). Each place
    gets 'kind' and 'kind_reason'; a place Flash is unsure of stays a watermark (recall first)."""
    import agent_native
    from PIL import ImageDraw, ImageOps
    work = Path(run_dir)/'watermark'/'place_kind'; work.mkdir(parents=True, exist_ok=True)
    ff = media.binary('ffmpeg'); fps = float(media.ratio(media.probe(str(source))['fps']))
    hits = place_hits(detections, places)
    for p in places:
        fr = hits.get(p['id']) or [p.get('first', 0)]
        pick = [fr[int(i)] for i in np.linspace(0, len(fr) - 1, min(samples, len(fr)))]
        parts = [('text', f'Each pair below shows one video frame (the red box marks where OCR read the word {TARGET}) and an '
                          'enlarged crop around that box. Is the text in the red box (a) a floating_watermark: a small, '
                          'semi-transparent logo + word laid over the picture, not attached to any object, staying at the '
                          'same screen position; (b) product_print: printed on a physical object in the scene -- a bottle '
                          'label, a box, a pouch, a phone screen showing the product; (c) caption_or_sign: a subtitle line, '
                          'a title card or a sign in the scene; or (d) unsure? Answer in the JSON schema.')]
        x, y, w, h = p['box']
        saved_crops = []
        for f in pick:
            png = work/f"p{p['id']}_f{f}.png"
            subprocess.run([ff, '-v', 'error', '-y', '-ss', f'{max(0.0, (f - 2) / fps):.4f}', '-i', str(source), '-frames:v', '1', str(png)], check=False)
            if not png.is_file(): continue                       # past the last decodable frame
            im = Image.open(png).convert('RGB'); full = im.copy(); ImageDraw.Draw(full).rectangle([x - 2, y - 2, x + w + 2, y + h + 2], outline=(255, 0, 0), width=3)
            m = max(w, h); crop = im.crop((max(0, x - m), max(0, y - m), min(im.width, x + w + m), min(im.height, y + h + m)))
            k = max(1, int(480 / max(crop.size))); crop = ImageOps.autocontrast(crop, cutoff=1).resize((crop.width * k, crop.height * k), Image.LANCZOS)
            fp, cp = work/f"p{p['id']}_f{f}_full.jpg", work/f"p{p['id']}_f{f}_crop.jpg"
            full.save(fp, quality=88); crop.save(cp, quality=90)
            parts += [('image', str(fp)), ('image', str(cp))]
            saved_crops.append(str(fp))
        if agent_native.is_native():
            p['kind'] = 'unsure'
            p['kind_reason'] = 'agent_native: inspect saved crops in watermark/place_kind/ via read_file'
            p['agent_review_images'] = saved_crops
            continue
        import flash_screen
        try:
            ans = flash_screen.call_flash(parts, PLACE_KIND_SCHEMA, run_dir)['result']
        except Exception as e:                                       # no answer: keep it (recall first)
            ans = {'kind': 'unsure', 'reason': f'flash failed: {str(e)[:120]}'}
        p['kind'] = ans.get('kind') or 'unsure'; p['kind_reason'] = ans.get('reason', '')
    return places


def drop_print_places(detections, places):
    """Places Flash called product print or a caption/sign: their reads become printed_label_keep (no watermark cleanup,
    no LYMPHLOOP stamp); the watermark places are returned."""
    keep = [p for p in places if p.get('kind') not in ('product_print', 'caption_or_sign')]
    gone = [p for p in places if p not in keep]
    # every read that made a print place goes with it, wherever its box ended up: a label on a bottle moves, and its
    # reads lie far from the place's measured box (2026-11-22 film 1141895738823753: all 10 places judged bottle print,
    # 138 label reads still counted as watermark)
    member = {(r[0], tuple(r[1:])): q for q in gone for r in q.get('reads') or []}
    n = 0
    for d in detections:
        if d.get('classification') != 'floating_overlay': continue
        q = position_of(d['bbox'], places)
        q = q if q is not None and q in gone else member.get((d['frame'], tuple(d['bbox'])))
        if q is not None and position_of(d['bbox'], keep) is None:
            d['classification'] = 'printed_label_keep'; d['reclassified'] = f"place {q['id']}: {q.get('kind')}"; n += 1
    for q in places: q.pop('reads', None)
    return keep, n

def position_of(box, places):
    """The fixed place a stamp box belongs to (same orientation, overlapping), else None."""
    v = _vertical(box)
    hits = [(p, _overlap(box, p['box'])) for p in places if p['vertical'] == v and _overlap(box, p['box']) > POSITION_OVERLAP]
    return max(hits, key=lambda h: h[1])[0] if hits else None


def fix_stamps(plan_rec, places):
    """One box per place for the whole film (user 2026-11-20). A stamp at a known place takes the place's box and the
    place's one reading direction (the direction most of its stamps had); a stamp elsewhere (a manual add-final box)
    takes its track's one box. The OCR box of the frame is kept as `mark`: apply_stamps cleans it as well."""
    track_box = {t['id']: t['box'] for t in plan_rec.get('track_list', [])}
    votes = {}
    for sts in plan_rec['stamps'].values():
        for st in sts:
            st.setdefault('mark', list(st['box']))
            p = position_of(st['mark'], places)
            st['position'] = p['id'] if p else None
            st['box'] = list(p['box']) if p else list(track_box.get(st.get('track'), st['box']))
            # A recognized fixed stamp has an actual pixel-measured footprint.
            # OCR can overgrow left into a forehead: keep that box as evidence,
            # but do not erase the extra face pixels before adding our word.
            if p: st['mask_box'] = list(p['box'])
            if p: votes.setdefault(p['id'], []).append(st['orient'])
    orient = {pid: max(set(v), key=v.count) for pid, v in votes.items()}
    for sts in plan_rec['stamps'].values():
        for st in sts:
            if st['position'] is not None: st['orient'] = orient[st['position']]
    plan_rec['positions'] = [{**p, 'orient': orient.get(p['id']), 'stamped_frames': len(votes.get(p['id'], []))} for p in places]
    plan_rec['fixed_size'] = 'one box per place for the whole film (user 2026-11-20)'
    return plan_rec


SLOT_TEXT_PX = 56            # the mark's line height after enlarging, in the OCR mosaic
SLOT_LOOKS = (None, 'eq=contrast=2.2:brightness=-0.05', 'highpass')


def position_slots(places, width, height):
    """(place id, crop x, y, w, h, transpose, contrast, upright w, upright h) for the OCR mosaic: each place with a margin,
    turned upright (a vertical place of unknown direction both ways): plain, with contrast (dark backgrounds), and with
    high-passed (the picture minus its blur, contrast raised: a faint mark over a bright background reads darker than
    it, Codex 30542865505302802 at 160 s on a white shirt, unreadable with plain or contrast)."""
    out = []
    for p in places:
        x, y, w, h = p['box']; thick = min(w, h); m = max(4, int(round(0.5 * thick)))
        x0, y0 = max(0, x - m), max(0, y - m); x1, y1 = min(width, x + w + m), min(height, y + h + m)
        cw, ch = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
        if p['vertical']:
            ts = {'v_down': [2], 'v_up': [1]}.get(p.get('orient'), [1, 2])
            dims = (ch, cw)
        else:
            ts, dims = [None], (cw, ch)
        k = SLOT_TEXT_PX / max(1, thick)
        for t in ts:
            for c in SLOT_LOOKS:
                out.append((p['id'], x0, y0, cw, ch, t, c, int(dims[0] * k) // 2 * 2, int(dims[1] * k) // 2 * 2))
    return out


def position_scan(source, places, work, ranges=None, step=SPARSE_STEP, pad=DENSE_PAD, workers=SCAN_WORKERS):
    """OCR only the fixed places (user 2026-11-20): every frame's crops of all places, upright and enlarged, stacked
    into one small picture, OCR'd once; sparse every `step`-th frame, then every frame within `pad` of a hit, in
    `workers` processes. A hit is reported with its place's box. Far cheaper than the full-frame + 12 edge-strip passes,
    which stay for finding the places (watermark scan) and for --full-watermark."""
    import caption_clean
    from concurrent.futures import ThreadPoolExecutor
    source = Path(source).resolve(); work = Path(work); work.mkdir(parents=True, exist_ok=True)
    info = media.probe(str(source), include_frames=True); W, H = int(info['video']['width']), int(info['video']['height'])
    n = int(info['frame_count']); slots = position_slots(places, W, H)
    if not slots: return {'detections': [], 'scanned_frames': 0, 'slots': 0}
    sw = max(s[7] for s in slots) + 8; sh = max(s[8] for s in slots) + 8
    ncols = max(1, round((len(slots) * sh / sw) ** 0.5)); nrows = -(-len(slots) // ncols)
    by_id = {p['id']: p for p in places}; scanner = caption_clean.scanner(work); ff = media.binary('ffmpeg')
    ranges = merge_ranges(ranges or [(0, n)], n)
    source_sha, scanner_sha = _sha(source), _sha(scanner)

    def one(job):
        name, windows, st = job
        picked = [f for a, b in windows for f in range(a, b) if (f - a) % st == 0]
        sel = '+'.join(f"between(n\\,{a}\\,{b - 1})*not(mod(n-{a}\\,{st}))" for a, b in windows)
        # Pack OCR frames onto a contiguous zero-based grid. Source-time gaps
        # create an MP4 leading edit that AVAssetReader can expose as one extra
        # copy of the first image, shifting every mapped source frame by one.
        rate = str(info['fps'])
        parts = [f"[0:v]select='{sel}',settb=expr=1/({rate}),setpts=N,fps={rate},split={len(slots)}"
                 + ''.join(f'[s{i}]' for i in range(len(slots)))]
        for i, (_, x0, y0, cw, ch, t, c, uw, uh) in enumerate(slots):
            f = [f'crop={cw}:{ch}:{x0}:{y0}'] + ([f'transpose={t}'] if t else []) + [f'scale={uw}:{uh}:flags=lanczos']
            if c == 'highpass':
                parts.append(f'[s{i}]' + ','.join(f) + f',format=gray,split[h{i}][g{i}]')
                parts.append(f'[g{i}]gblur=sigma=10[b{i}]')
                parts.append(f'[h{i}][b{i}]blend=all_mode=grainextract,eq=contrast=5,format=yuv420p,pad={sw}:{sh}:0:0:black[c{i}]')
                continue
            if c: f.append(c)
            f.append(f'pad={sw}:{sh}:0:0:black')
            parts.append(f'[s{i}]' + ','.join(f) + f'[c{i}]')
        # a near-square grid, not one tall column: a 326x3300 column of 30 crops read fewer marks (Vision scales a
        # tall picture down) than a grid of the same crops
        if len(slots) == 1: parts.append('[c0]null[m]')
        else:
            cols = []
            for c in range(ncols):
                ids = list(range(c * nrows, min(len(slots), (c + 1) * nrows)))
                if not ids: continue
                tag = f'[col{c}]'
                parts.append(''.join(f'[c{i}]' for i in ids) + (f'vstack=inputs={len(ids)},' if len(ids) > 1 else '') + f'pad={sw}:{sh * nrows}:0:0:black{tag}')
                cols.append(tag)
            parts.append(''.join(cols) + (f'hstack=inputs={len(cols)}[m]' if len(cols) > 1 else 'null[m]'))
        clip = work/f'{name}.mp4'; out = work/f'{name}.json'
        meta = work/f'{name}.cache.json'
        key = {'source_sha256': source_sha, 'scanner_binary_sha256': scanner_sha,
               'windows': [list(w) for w in windows], 'step': st, 'filter': parts,
               'expected_frames': len(picked), 'v': 2}
        cached = False
        if meta.is_file() and clip.is_file() and out.is_file():
            old = json.loads(meta.read_text())
            cached = (old.get('key') == key and old.get('clip_sha256') == _sha(clip)
                      and old.get('scan_sha256') == _sha(out))
        if not cached:
            subprocess.run([ff, '-v', 'error', '-y', '-i', str(source), '-an', '-filter_complex', ';'.join(parts), '-map', '[m]',
                            '-frames:v', str(len(picked)), '-fps_mode', 'passthrough', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '12', str(clip)], check=True)
            for attempt in range(3):
                try:
                    subprocess.run([str(scanner), str(clip), str(out)], check=True, capture_output=True)
                    if json.loads(out.read_text()).get('frame_count') != len(picked):
                        raise RuntimeError(f'Incomplete position OCR {name}: expected {len(picked)} frames, got {json.loads(out.read_text()).get("frame_count")}')
                    break
                except (subprocess.CalledProcessError, RuntimeError):
                    if attempt == 2: raise
                    time.sleep(2*(attempt+1))
        actual = json.loads(out.read_text())
        if actual.get('frame_count') != len(picked):
            raise RuntimeError(f'Incomplete position OCR {name}: expected {len(picked)} frames, got {actual.get("frame_count")}')
        if not cached:
            record = {'key': key, 'clip_sha256': _sha(clip), 'scan_sha256': _sha(out)}
            tmp = meta.with_suffix('.tmp'); tmp.write_text(json.dumps(record)); tmp.replace(meta)
        found = []
        for d in actual['detections']:
            if not is_watermark_text(d['text']) or d['frame'] >= len(picked): continue
            col = int((d['bbox'][0] + d['bbox'][2] / 2) // sw); row = int((d['bbox'][1] + d['bbox'][3] / 2) // sh)
            i = col * nrows + row
            if 0 <= row < nrows and 0 <= i < len(slots):
                found.append({'frame': picked[d['frame']], 'text': d['text'], 'confidence': d.get('confidence', 0.0),
                              'bbox': list(by_id[slots[i][0]]['box']), 'position': slots[i][0], 'pass': 'position'})
        return found, len(picked)

    def run_pass(name, rngs, st):
        jobs = [(f'{name}-{k}', group, st) for k, group in enumerate(range_groups(rngs, workers))]
        with ThreadPoolExecutor(max(1, min(workers, len(jobs)))) as ex:
            res = list(ex.map(one, jobs))
        return [d for f, _ in res for d in f], sum(c for _, c in res)

    sparse, n1 = run_pass('sparse', ranges, step) if step > 1 else ([], 0)
    hits = sorted({d['frame'] for d in sparse})
    dense_ranges = ranges if step <= 1 else merge_ranges([(f - pad, f + pad + 1) for f in hits], n)
    dense, n2 = run_pass('dense', dense_ranges, 1) if dense_ranges else ([], 0)
    seen = set(); dets = []
    for d in dense + sparse:
        if (d['frame'], d['position']) not in seen: seen.add((d['frame'], d['position'])); dets.append(d)
    return {'detections': sorted(dets, key=lambda d: d['frame']), 'scanned_frames': n1 + n2, 'slots': len(slots),
            'coverage': f'places only: every {step}th frame of {ranges}, every frame within {pad} of a hit'}


# ---------------------------------------------------------------- stamping
def _font_path():
    return next((f for f in FONTS if Path(f).is_file()), None)


_GLYPH = {}


def glyph(orient):
    """White LYMPHLOOP rendered large and tight, as an alpha mask (0..1), turned to the watermark's reading direction."""
    if orient not in _GLYPH:
        fp = _font_path(); font = ImageFont.truetype(fp, 120) if fp else ImageFont.load_default()
        bb = font.getbbox(TEXT); im = Image.new('L', (bb[2] - bb[0] + 8, bb[3] - bb[1] + 8), 0)
        ImageDraw.Draw(im).text((4 - bb[0], 4 - bb[1]), TEXT, font=font, fill=255)
        im = im.crop(im.getbbox())
        if orient == 'v_up': im = im.rotate(90, expand=True)
        elif orient == 'v_down': im = im.rotate(-90, expand=True)
        _GLYPH[orient] = im
    return _GLYPH[orient]


def stamp_mask(box, orient):
    """(alpha array, x, y): LYMPHLOOP scaled to fill the watermark footprint (icon + word) along its reading direction,
    letter proportions kept, centred across."""
    x, y, w, h = box; g = glyph(orient)
    s = min(w / g.width, h / g.height)
    nw, nh = max(1, int(round(g.width * s))), max(1, int(round(g.height * s)))
    m = np.asarray(g.resize((nw, nh), Image.LANCZOS), dtype=np.float32) / 255.0
    return m, x + (w - nw) // 2, y + (h - nh) // 2


def apply_stamps(frame, stamps):
    """frame: HxWx3 uint8 RGB (modified copy returned). Each footprint is inpainted first, so neither the original mark
    nor a faint copy a generated segment carried can show between the letters, then LYMPHLOOP is laid on top."""
    import cv2
    H, W = frame.shape[:2]
    mask = np.zeros((H, W), np.uint8)
    for st in stamps:
        for x, y, w, h in (st['box'], st.get('mask_box') or st.get('mark') or st['box']):
            mask[max(0, y - 1):min(H, y + h + 1), max(0, x - 1):min(W, x + w + 1)] = 255
    out = cv2.inpaint(np.ascontiguousarray(frame), mask, 3, cv2.INPAINT_TELEA).astype(np.float32)
    for st in stamps:
        m, x, y = stamp_mask(st['box'], st['orient'])
        x1, y1 = min(W, x + m.shape[1]), min(H, y + m.shape[0])
        if x1 <= x or y1 <= y: continue
        a = m[: y1 - y, : x1 - x, None]
        out[y:y1, x:x1] = out[y:y1, x:x1] * (1 - a) + 255.0 * a
    return out.clip(0, 255).astype(np.uint8)


def stamp(master, plan_rec, out_mp4, receipt, verify_frames=60):
    """Decode MASTER, stamp the planned frames, encode once (libx264 crf 12, audio copied), verify the stamps survived."""
    master, out_mp4 = Path(master).resolve(), Path(out_mp4).resolve()
    info = media.probe(str(master), include_frames=True); W, H = int(info['video']['width']), int(info['video']['height'])
    fps = info['fps']; n = info['frame_count']; stamps = {int(k): v for k, v in plan_rec['stamps'].items()}
    if stamps and max(stamps) >= n: raise SystemExit(f'brand plan reaches frame {max(stamps)} but the master has {n} frames')
    ff = media.binary('ffmpeg'); size = W * H * 3
    dec = subprocess.Popen([ff, '-v', 'error', '-i', str(master), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE)
    enc = subprocess.Popen([ff, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(fps), '-i', '-',
                            '-i', str(master), '-map', '0:v:0', '-map', '1:a:0?', '-c:v', 'libx264', '-preset', 'medium', '-crf', '12',
                            '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', str(out_mp4)], stdin=subprocess.PIPE)
    i = 0
    while True:
        buf = dec.stdout.read(size)
        if len(buf) < size: break
        if i in stamps:
            buf = apply_stamps(np.frombuffer(buf, np.uint8).reshape(H, W, 3), stamps[i]).tobytes()
        enc.stdin.write(buf); i += 1
    dec.wait(); enc.stdin.close(); enc.wait()
    if dec.returncode or enc.returncode or i != n: raise SystemExit(f'brand stamping failed (decoded {i}/{n} frames)')
    check = verify(out_mp4, plan_rec, verify_frames)
    rec = {'type': 'lymphloop_brand_receipt', 'rule': 'white opaque LYMPHLOOP over the original watermark footprint (droplet icon + word), same position, size and orientation (user, 2026-10-25)',
           'input': {'path': str(master), 'sha256': _sha(master)}, 'output': {'path': str(out_mp4), 'sha256': _sha(out_mp4)},
           'frames': n, 'stamped_frames': len(stamps), 'tracks': plan_rec.get('tracks'), 'verification': check}
    Path(receipt).write_text(json.dumps(rec, indent=1))
    if not check['ok']: raise SystemExit(f"brand verification failed: {check}")
    return rec


def _stamp_code():
    import inspect
    src = ''.join(inspect.getsource(f) for f in (glyph, stamp_mask, apply_stamps, _encode_atom)) + TEXT
    fp = _font_path()
    return hashlib.sha256((src + (_sha(fp) if fp else 'default-font')).encode()).hexdigest()


def _encode_atom(src, stamps_rel, out, W, H, fps, n):
    """Decode one recut atom, stamp its frames, encode it with the recut atoms' x264 settings (stitchable) at crf 12."""
    ff = media.binary('ffmpeg'); size = W * H * 3; tmp = out.with_suffix('.part.mp4')
    dec = subprocess.Popen([ff, '-v', 'error', '-i', str(src), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE)
    enc = subprocess.Popen([ff, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(fps), '-i', '-',
                            '-c:v', 'libx264', '-preset', 'fast', '-crf', '12', *media.X264_STITCH, '-threads', '2',
                            '-pix_fmt', 'yuv420p', '-video_track_timescale', str(media.ratio(fps).numerator),
                            '-movflags', '+faststart', '-f', 'mp4', str(tmp)], stdin=subprocess.PIPE)
    i = 0
    while True:
        buf = dec.stdout.read(size)
        if len(buf) < size: break
        if i in stamps_rel:
            buf = apply_stamps(np.frombuffer(buf, np.uint8).reshape(H, W, 3), stamps_rel[i]).tobytes()
        enc.stdin.write(buf); i += 1
    dec.wait(); enc.stdin.close(); enc.wait()
    if dec.returncode or enc.returncode or i != n: raise SystemExit(f'brand: stamping atom {src} failed (decoded {i}/{n} frames)')
    tmp.replace(out)


def atoms_usable(timeline):
    """True when every segment of the recut timeline names its own encoded atom (media_pipeline per-atom cache)."""
    segs = timeline.get('segments') or []
    return bool(segs) and all(s.get('atom_key') and s.get('atom_video') and Path(s['atom_video']).is_file() for s in segs)


def stamp_atoms(master, timeline, plan_rec, out_mp4, receipt, cache_dir, verify_frames=60):
    """stamp() by recut atom (user 2026-11-22: "改，按内容缓存"). An atom with no watermark frame goes in as the recut encoded
    it; an atom with stamps is stamped and encoded on its own, cached by (atom content, its stamps relative to the atom,
    stamping code). A goof redo that moves the rest of the film re-stamps only the atoms whose content changed."""
    master, out_mp4, cache_dir = Path(master).resolve(), Path(out_mp4).resolve(), Path(cache_dir)
    info = media.probe(str(master), include_frames=True); W, H = int(info['video']['width']), int(info['video']['height'])
    fps = info['fps']; n = info['frame_count']; stamps = {int(k): v for k, v in plan_rec['stamps'].items()}
    if stamps and max(stamps) >= n: raise SystemExit(f'brand plan reaches frame {max(stamps)} but the master has {n} frames')
    code = _stamp_code(); pieces = []; made = reused = 0
    for seg in timeline['segments']:
        rel = {f - seg['out_in']: stamps[f] for f in range(seg['out_in'], seg['out_out']) if f in stamps}
        if not rel: pieces.append(Path(seg['atom_video'])); continue
        key = hashlib.sha256(json.dumps({'atom': seg['atom_key'], 'stamps': sorted((k, v) for k, v in rel.items()), 'code': code},
                                        sort_keys=True, default=str).encode()).hexdigest()
        out = cache_dir/key[:2]/(key + '.mp4')
        if out.is_file(): reused += 1
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            _encode_atom(seg['atom_video'], rel, out, W, H, fps, seg['out_out'] - seg['out_in']); made += 1
        pieces.append(out)
    ripple = timeline.get('timing') == 'ripple'
    work = out_mp4.parent/(out_mp4.stem + '.atoms'); work.mkdir(exist_ok=True)
    lst = work/'video_concat.txt'
    lst.write_text(''.join(media.concat_path(x) + (f'\nduration {seg["output_seconds"]:.9f}' if ripple else '') + '\n'
                           for x, seg in zip(pieces, timeline['segments'])))
    ff = media.binary('ffmpeg')
    subprocess.run([ff, '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(lst), '-i', str(master), '-map', '0:v:0', '-map', '1:a:0?',
                    '-c', 'copy', '-movflags', '+faststart', str(out_mp4)], check=True)
    got = media.probe(str(out_mp4), include_frames=True)['frame_count']
    if got != n: raise SystemExit(f'brand: atom concat gave {got} frames, the recut master has {n}')
    check = verify(out_mp4, plan_rec, verify_frames)
    rec = {'type': 'lymphloop_brand_receipt', 'rule': 'white opaque LYMPHLOOP over the original watermark footprint (droplet icon + word), same position, size and orientation (user, 2026-10-25)',
           'input': {'path': str(master), 'sha256': _sha(master)}, 'output': {'path': str(out_mp4), 'sha256': _sha(out_mp4)},
           'frames': n, 'stamped_frames': len(stamps), 'tracks': plan_rec.get('tracks'), 'verification': check,
           'by_atom': {'atoms': len(pieces), 'stamped_now': made, 'stamped_reused': reused,
                       'untouched': len(pieces) - made - reused}}
    Path(receipt).write_text(json.dumps(rec, indent=1))
    if not check['ok']: raise SystemExit(f"brand verification failed: {check}")
    prune_brand_cache(cache_dir, pieces)
    return rec


def prune_brand_cache(cache_dir, pieces, keep_renders=3):
    used = cache_dir/'renders.json'
    hist = json.loads(used.read_text()) if used.is_file() else []
    hist = (hist + [sorted(p.stem for p in pieces if str(p).startswith(str(cache_dir)))])[-keep_renders:]
    used.write_text(json.dumps(hist)); keep = set().union(*map(set, hist))
    for f in cache_dir.glob('*/*.mp4'):
        if f.stem.split('.')[0] not in keep: f.unlink(missing_ok=True)


def verify(video, plan_rec, sample=60):
    """Decode sampled stamped frames and check that the LYMPHLOOP pixels came out white (catches a wrong timeline or size)."""
    stamps = {int(k): v for k, v in plan_rec['stamps'].items()}
    if not stamps: return {'ok': True, 'checked': 0, 'note': 'no watermark frames'}
    frames = sorted(stamps); pick = frames[:: max(1, len(frames) // sample)][:sample]
    info = media.probe(str(video)); W, H = int(info['video']['width']), int(info['video']['height'])
    ff = media.binary('ffmpeg'); bad = []
    sel = '+'.join(f'eq(n\\,{f})' for f in pick)
    raw = subprocess.run([ff, '-v', 'error', '-i', str(video), '-vf', f"select='{sel}'", '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True).stdout
    size = W * H * 3
    for j, f in enumerate(pick):
        fr = np.frombuffer(raw[j * size:(j + 1) * size], np.uint8).reshape(H, W, 3)
        for st in stamps[f]:
            m, x, y = stamp_mask(st['box'], st['orient'])
            reg = fr[y:y + m.shape[0], x:x + m.shape[1]].mean(axis=2)
            core = m[: reg.shape[0], : reg.shape[1]] > 0.8
            if core.sum() and (reg[core] >= 200).mean() < 0.8: bad.append(f)
    return {'ok': not bad, 'checked': len(pick), 'failed_frames': bad[:20]}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); s = p.add_subparsers(dest='cmd', required=True)
    a = s.add_parser('edge-scan'); a.add_argument('source'); a.add_argument('out')
    b = s.add_parser('plan'); b.add_argument('scan'); b.add_argument('out')
    c = s.add_parser('stamp'); c.add_argument('master'); c.add_argument('plan'); c.add_argument('out'); c.add_argument('receipt')
    x = p.parse_args()
    if x.cmd == 'edge-scan': r = edge_scan(x.source, x.out); print(json.dumps({'detections': len(r['detections'])}))
    elif x.cmd == 'plan':
        r = plan(json.loads(Path(x.scan).read_text())); Path(x.out).write_text(json.dumps(r, indent=1)); print(json.dumps({k: r[k] for k in ('tracks', 'frames')}))
    else: print(json.dumps(stamp(x.master, json.loads(Path(x.plan).read_text()), x.out, x.receipt)['verification']))


if __name__ == '__main__':
    main()
