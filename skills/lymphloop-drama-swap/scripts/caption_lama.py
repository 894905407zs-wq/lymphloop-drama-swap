#!/usr/bin/env python3
"""Remove burned-in captions from a REFERENCE clip with the LaMa inpainting model, before the clip goes to the generator.

User, 2026-11-11: "送给模型的视频不要带字幕，处理好了再送模型". The official Seedance 2.5 prompt guide says the same:
a reference video that carries captions can defeat "不要字幕". The 2026-11-10 box inpaint (OpenCV TELEA over the whole
OCR box) left smears that the generator amplified; this one masks each caption line (padded over outline and
shadow) and lets LaMa rebuild the texture behind it.

Model: Carve/LaMa-ONNX `lama_fp32.onnx` (208 MB, fixed 512x512 input), cached once per machine in
~/.cache/lymphloop-models and shared by both skill copies; downloaded on first use and checked by sha256.
"""
from __future__ import annotations
import difflib, hashlib, json, os, re, subprocess, sys, threading, urllib.request
from pathlib import Path
import cv2, numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import caption_clean as cc  # noqa: E402

MODEL_URL = 'https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx'
MODEL_SHA256 = '1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6'
MODEL_DIR = Path(os.environ.get('LYMPHLOOP_MODEL_DIR', Path.home()/'.cache'/'lymphloop-models'))
SIDE = 512
HOLD = 3            # frames around an OCR hit that may still carry the caption while it fades
WORKERS = int(os.environ.get('LYMPHLOOP_LAMA_WORKERS', '2'))   # frames inpainted at once (tuned with local_slot=2 for 18-core M5 Pro)
THREADS = int(os.environ.get('LYMPHLOOP_LAMA_THREADS', '4'))   # CPU threads per frame (CoreML was slower than CPU: 1.96 s vs 1.15 s per frame, M5 Pro)
CONTEXT = 64         # px of untouched picture a window keeps beside its masked area along the frame's long side
_session = None
_session_lock = threading.Lock()

# Result cache (user 2026-11-21: "先做缓存"). Reference clips are rebuilt again and again (a rerun of prepare, a goof
# shot's reference, a reused film): three batches held 1066 cleaned clips for 395 references in use, and LaMa is
# ~4.5 CPU-seconds a frame (1.15 s x 4 threads). A model call is keyed by the exact 512x512 crop and mask it gets
# (plus the model's sha), so a hit returns exactly what that call would have produced: same picture, no model run.
# Only the rows and columns of the output that can reach the frame are kept (mask box + MARGIN px; the 2 px feather
# and the cubic upscale reach at most 8 px of a window of 512 px or more), as lossless PNG. Shared by both skill
# copies and every RUN; pruned oldest-first above LYMPHLOOP_LAMA_CACHE_GB (default 48 for 60-film batches). LYMPHLOOP_LAMA_CACHE=0 turns it off.
CACHE_DIR = MODEL_DIR/'lama-cache'
CACHE_MARGIN = 16
CACHE_GB = float(os.environ.get('LYMPHLOOP_LAMA_CACHE_GB', '48'))
_cache_on = os.environ.get('LYMPHLOOP_LAMA_CACHE', '1') != '0'
_cache_stats = {'hit': 0, 'miss': 0}
_pruned = False


def model_path():
    """The cached model; downloaded (208 MB) the first time and verified."""
    p = MODEL_DIR/'lama_fp32.onnx'
    if p.is_file(): return p
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    part = p.with_suffix('.part')
    print(f'downloading LaMa model to {p} (208 MB) ...', file=sys.stderr)
    urllib.request.urlretrieve(MODEL_URL, part)
    got = hashlib.sha256(part.read_bytes()).hexdigest()
    if got != MODEL_SHA256:
        part.unlink(); raise SystemExit(f'LaMa model checksum mismatch: {got}')
    part.rename(p)
    return p


def session():
    global _session
    if _session is None:
        # Several clips and their frame workers reach the first call together.
        # ONNX releases the GIL while loading: without a lock each worker loads
        # another 208 MB graph and starts its own CPU pool before any can finish.
        with _session_lock:
            if _session is None:
                import onnxruntime as ort
                opts = ort.SessionOptions(); opts.log_severity_level = 3; opts.intra_op_num_threads = THREADS
                _session = ort.InferenceSession(str(model_path()), opts, providers=['CPUExecutionProvider'])
    return _session


def line_mask(shape, boxes):
    """The whole text line of every caption box, padded over the outline and drop shadow. A mask that hugs the glyphs
    leaves a halo of outline pixels that LaMa repeats as scribbles (measured 2026-11-11 on Lymphoria G03: glyph mask
    left text-shaped noise, the padded line box came back as clean cloth texture)."""
    H, W = shape[:2]
    k = max(1.0, W / 720)                                   # paddings were tuned on a 720-wide reference
    m = np.zeros((H, W), np.uint8)
    for x, y, w, h in boxes:
        m[max(0, y - int(12 * k)):min(H, y + h + int(14 * k)), max(0, x - int(14 * k)):min(W, x + w + int(14 * k))] = 255
    return m


PLATE_SAT, PLATE_VAL = 140, 70      # a caption backplate: a solid strong colour behind the words (a pink shirt is ~120)
PLATE_TOL = 55                      # max BGR difference from the plate's colour that still counts as plate
PLATE_LEFT = 0.03                   # share of a plate's area still plate-coloured after inpainting that is a leftover


def plates(frame, boxes):
    """The coloured backplates of caption boxes: [(mask, BGR colour)]. Highlight captions sit on a rounded solid box
    that reaches past the OCR text box; the padded line mask left its rim, and LaMa, seeing plate colour all around,
    painted the whole line back as an empty plate (2026-11-22 film 1100729319517519 G08: blue bars on the woman and the
    counter on 47 of 360 frames; OCR read no text there, so frames_left was 0). A plate is a solid block of one strong
    colour inside the text box (white letters on it count as part of the block; glyph strokes of coloured text do not
    make one), that ends within a line height around the box. Every strong hue in the box is tried: a saturated pink
    shirt behind the words can outnumber a small plate under "a" or "It"."""
    H, W = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    strong = (hsv[..., 1] >= PLATE_SAT) & (hsv[..., 2] >= PLATE_VAL)
    out = []
    for x, y, w, h in sorted({tuple(int(v) for v in b) for b in boxes}):    # held boxes repeat the same line
        bx0, by0, bx1, by1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if bx1 - bx0 < 4 or by1 - by0 < 4: continue
        box_strong = strong[by0:by1, bx0:bx1]
        if box_strong.mean() < 0.04: continue
        hue = hsv[by0:by1, bx0:bx1, 0].astype(np.int16)
        counts = np.bincount(hue[box_strong] // 6, minlength=30)
        white = frame[by0:by1, bx0:bx1].min(axis=2) >= 190
        px, py = max(8, h), max(6, int(0.6 * h))
        sx0, sy0, sx1, sy1 = max(0, x - px), max(0, y - py), min(W, x + w + px), min(H, y + h + py)
        for b in np.argsort(counts)[::-1][:3]:
            if counts[b] < 0.03 * box_strong.size: break
            h0 = int(b) * 6 + 3
            inside = box_strong & (np.minimum(np.abs(hue - h0), 180 - np.abs(hue - h0)) <= 9)
            n, lab, st, _ = cv2.connectedComponentsWithStats(inside.astype(np.uint8), 8)
            if n < 2: continue
            big = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
            cx, cy, bw, bh = (int(v) for v in st[big, :4])
            solid = ((lab[cy:cy + bh, cx:cx + bw] == big) | white[cy:cy + bh, cx:cx + bw]).mean()
            if bh < 0.5 * (by1 - by0) or bw < 0.4 * bh or solid < 0.8: continue    # glyph strokes, not a block
            colour = np.median(frame[by0:by1, bx0:bx1][lab == big], axis=0)
            near = (np.abs(frame[sy0:sy1, sx0:sx1].astype(np.int16) - colour.astype(np.int16)).max(axis=2) <= PLATE_TOL).astype(np.uint8)
            n2, lab2, _, _ = cv2.connectedComponentsWithStats(near, 8)
            seed = np.zeros_like(near); seed[by0 - sy0:by1 - sy0, bx0 - sx0:bx1 - sx0] = (lab == big)
            keep = set(np.unique(lab2[(seed > 0) & (near > 0)])) - {0}
            if not keep: continue
            sel = np.isin(lab2, list(keep))
            # a plate ends inside the search area; colour that runs to its edge is a coat, a wall or a carton behind the words
            if sel[0].any() or sel[-1].any() or sel[:, 0].any() or sel[:, -1].any(): continue
            m = np.zeros((H, W), np.uint8)
            m[sy0:sy1, sx0:sx1] = sel.astype(np.uint8) * 255
            ys, xs = np.where(m > 0)                                    # the plate is a box: fill it, words and all
            m[ys.min():ys.max() + 1, xs.min():xs.max() + 1] = 255
            out.append((m, colour))
    return out


def plate_mask(frame, boxes):
    """All backplates of `boxes`, grown over their anti-aliased rim; empty when the captions have none."""
    found = plates(frame, boxes)
    if not found: return None, []
    # the plate's colour bleeds past its edge (4:2:0 chroma, blur): with 4 px LaMa still saw a blue rim and painted a grey-blue
    # box back (G08 frames 212, 238); 8 px left the shirt clean
    r = max(6, round(frame.shape[1] / 720 * 8))
    m = np.zeros(frame.shape[:2], np.uint8)
    for pm, _ in found: m |= pm
    return cv2.dilate(m, np.ones((2 * r + 1, 2 * r + 1), np.uint8)), found


def plate_left(frame, found):
    """True when a plate's colour still covers part of where the plate was (the inpaint painted it back)."""
    for pm, colour in found:
        area = pm > 0
        near = np.abs(frame.astype(np.int16) - colour.astype(np.int16)).max(axis=2) <= PLATE_TOL - 15
        if (near & area).sum() > max(60, PLATE_LEFT * area.sum()): return True
    return False


def white_caption_mask(frame, boxes):
    """Reviewed white-caption exception when a line rectangle overlaps a prop.

    Keep the visible prop pixels between letters. Dilate white glyphs over their
    dark outline; the normal padded-line method remains the default everywhere.
    """
    region = line_mask(frame.shape, boxes)
    lo, hi = frame.min(axis=2), frame.max(axis=2)
    mask = ((region > 0) & (lo >= 220) & ((hi - lo) <= 35)).astype(np.uint8) * 255
    radius = max(8, round(frame.shape[1] / 720 * 8))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    return cv2.bitwise_and(cv2.dilate(mask, kernel), region)


def window(mask):
    """A square crop around the masked area with context, inside the frame: (x, y, side)."""
    H, W = mask.shape
    ys, xs = np.where(mask > 0)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    side = int(min(min(W, H), max(SIDE, (x1 - x0) + 128, (y1 - y0) + 128)))
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    x = int(min(max(0, cx - side // 2), W - side)); y = int(min(max(0, cy - side // 2), H - side))
    return x, y, side


def groups(mask):
    """Masked areas that one window cannot hold together get a window each. A window is at most the frame's short side:
    with a caption low in the frame and the vertical watermark in a top corner in the same mask, the single window used
    until 2026-11-11 sat between the two and the caption below it stayed (Lymphoria G06: captions on 80 frames of the
    reference, found on the upload sheet). Returns sub-masks, each one inside what its own window covers."""
    H, W = mask.shape; short = min(W, H)
    lim_w = short if W == short else short - CONTEXT           # the short side itself may be masked edge to edge
    lim_h = short if H == short else short - CONTEXT
    n, _, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    parts = []
    for x, y, w, h in sorted((tuple(int(v) for v in stats[i][:4]) for i in range(1, n)), key=lambda b: (b[1], b[0])):
        for yy in range(y, y + h, lim_h):                       # an area longer than a window is tiled
            for xx in range(x, x + w, lim_w):
                parts.append([xx, yy, min(x + w, xx + lim_w), min(y + h, yy + lim_h)])
    boxes = []
    for p in parts:
        for b in boxes:
            if max(b[2], p[2]) - min(b[0], p[0]) <= lim_w and max(b[3], p[3]) - min(b[1], p[1]) <= lim_h:
                b[:] = [min(b[0], p[0]), min(b[1], p[1]), max(b[2], p[2]), max(b[3], p[3])]; break
        else:
            boxes.append(list(p))
    out = []
    for x0, y0, x1, y1 in boxes:
        sub = np.zeros_like(mask); sub[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
        if sub.any(): out.append(sub)
    return out


def inpaint(frame, mask):
    """One frame, every masked area: one LaMa pass per group of areas (see groups)."""
    for sub in groups(mask): frame = inpaint_batch([frame], [sub])[0]
    return frame


def _cache_key(crop, cm):
    h = hashlib.sha256(); h.update(MODEL_SHA256.encode()); h.update(f'{SIDE}:{crop.shape}:'.encode())
    h.update(np.ascontiguousarray(crop).tobytes()); h.update(np.ascontiguousarray(cm).tobytes())
    return h.hexdigest()


def _cache_path(key):
    return CACHE_DIR/key[:2]/(key + '.png')


def _cache_get(key):
    """The cached 512x512 BGR output (zeros outside the kept box), or None."""
    if not _cache_on: return None
    p = _cache_path(key)
    if not p.is_file(): return None
    raw = p.read_bytes()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None or len(raw) < 64: return None
    try: os.utime(p)                                              # recently used: pruned last
    except OSError: pass
    return img


def _cache_put(key, bgr, cm):
    """Keep the part of the 512x512 output that can reach the frame (mask box + CACHE_MARGIN), zeros elsewhere."""
    global _pruned
    if not _cache_on: return
    ys, xs = np.where(cm > 0)
    keep = np.zeros_like(bgr)
    if len(xs):
        y0, y1 = max(0, ys.min() - CACHE_MARGIN), min(SIDE, ys.max() + 1 + CACHE_MARGIN)
        x0, x1 = max(0, xs.min() - CACHE_MARGIN), min(SIDE, xs.max() + 1 + CACHE_MARGIN)
        keep[y0:y1, x0:x1] = bgr[y0:y1, x0:x1]
    p = _cache_path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode('.png', keep, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if not ok: return
        tmp = p.with_name(f'.{p.name}.{os.getpid()}.{threading.get_ident()}.part')
        tmp.write_bytes(buf.tobytes()); os.replace(tmp, p)        # atomic: parallel runs never read half a file
    except OSError:
        return
    if not _pruned:
        _pruned = True; prune_cache()


def prune_cache(limit_gb=None):
    """Delete the least recently used results until the cache is under 80% of its limit. Returns bytes freed."""
    limit = (CACHE_GB if limit_gb is None else limit_gb) * 1024 ** 3
    if not CACHE_DIR.is_dir() or limit <= 0: return 0
    files = []
    for p in CACHE_DIR.glob('*/*.png'):
        try: st = p.stat(); files.append((st.st_mtime, st.st_size, p))
        except OSError: pass
    total = sum(f[1] for f in files)
    if total <= limit: return 0
    freed = 0
    for _, size, p in sorted(files):
        if total - freed <= 0.8 * limit: break
        try: p.unlink(); freed += size
        except OSError: pass
    return freed


def cache_stats():
    return dict(_cache_stats)


def inpaint_batch(frames, masks):
    """LaMa on the square window of each frame; only masked pixels (with a 2 px feather) are replaced. A window whose
    exact crop and mask were inpainted before comes from the result cache instead of the model."""
    wins = [window(m) for m in masks]
    crops, cms, keys, outs = [], [], [], []
    for f, m, (x, y, s) in zip(frames, masks, wins):
        crop = cv2.resize(f[y:y + s, x:x + s], (SIDE, SIDE), interpolation=cv2.INTER_AREA if s > SIDE else cv2.INTER_LINEAR)
        cm = cv2.resize(m[y:y + s, x:x + s], (SIDE, SIDE), interpolation=cv2.INTER_NEAREST)
        key = _cache_key(crop, cm)
        crops.append(crop); cms.append(cm); keys.append(key); outs.append(_cache_get(key))
    todo = [i for i, o in enumerate(outs) if o is None]
    _cache_stats['hit'] += len(outs) - len(todo); _cache_stats['miss'] += len(todo)
    if todo:
        img = np.zeros((len(todo), 3, SIDE, SIDE), np.float32); msk = np.zeros((len(todo), 1, SIDE, SIDE), np.float32)
        for j, i in enumerate(todo):
            img[j] = cv2.cvtColor(crops[i], cv2.COLOR_BGR2RGB).transpose(2, 0, 1) / 255.0
            msk[j, 0] = (cms[i] > 0).astype(np.float32)
        s_ = session(); names = [n.name for n in s_.get_inputs()]
        res = s_.run(None, {names[0]: img, names[1]: msk})[0]
        for j, i in enumerate(todo):
            rgb = np.clip(res[j].transpose(1, 2, 0), 0, 255).astype(np.uint8)
            outs[i] = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            _cache_put(keys[i], outs[i], cms[i])
    done = []
    for f, m, (x, y, s), bgr in zip(frames, masks, wins, outs):
        patch = cv2.resize(bgr, (s, s), interpolation=cv2.INTER_CUBIC if s > SIDE else cv2.INTER_AREA)
        alpha = cv2.GaussianBlur((m[y:y + s, x:x + s] > 0).astype(np.float32), (0, 0), 2.0)
        alpha = np.maximum(alpha, (m[y:y + s, x:x + s] > 0).astype(np.float32))[..., None]      # 1 inside the mask, soft just outside
        res = f.copy()
        res[y:y + s, x:x + s] = (patch * alpha + f[y:y + s, x:x + s] * (1 - alpha)).astype(np.uint8)
        done.append(res)
    return done


def shot_starts(clip):
    """First frames of the shots inside the clip (ffmpeg scene score, as swap.hard_cuts)."""
    import re
    from fractions import Fraction
    r = subprocess.run([cc.FFMPEG, '-v', 'error', '-i', str(clip), '-an', '-vf', "select='gt(scene,0.2)',metadata=print:file=-", '-f', 'null', '-'],
                       capture_output=True, text=True)
    fps = float(Fraction(cc.probe(clip)[2]))
    return sorted({int(round(float(m) * fps)) for m in re.findall(r'pts_time:([0-9.]+)', r.stdout)})


TITLE_CARD = re.compile(r'(?i)(day|week|month|night|step|part|phase)\s*(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)'
                        r'|(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(days?|weeks?|months?)\s+later')


def reference_caption(d, width, height, policy=None, spoken=None):
    """Optional reviewed source-caption policy protects readable package print."""
    if d.get('confidence', 1) < 0.5:
        return False
    if policy:
        title = policy.get('title_pattern')
        if title and re.fullmatch(title, d.get('text', '').strip()):
            return True
    if TITLE_CARD.fullmatch(d.get('text', '').strip()):
        # a time card anywhere in the frame ("DAY 1" over the first hand shot, 2026-11-22 film 998 G05): taken off the
        # reference like a caption, put back on the generated clip by captions
        return True
    if not cc.is_caption(d['bbox'], width, height):
        return False
    if not policy:
        return True
    if d['bbox'][3] < policy.get('min_text_height_px', 0):
        return False
    if policy.get('spoken_filter'):
        source_tokens = re.findall(r"[a-z0-9']+", ' '.join(spoken or []).lower().replace('’', "'"))
        words = set(source_tokens)
        words.update(a + b for a, b in zip(source_tokens, source_tokens[1:]) if len(a + b) >= 4)
        numbers = dict(zip(('one two three four five six seven eight nine ten twenty thirty forty fifty sixty seventy eighty ninety hundred').split(),
                           ('1 2 3 4 5 6 7 8 9 10 20 30 40 50 60 70 80 90 100').split()))
        words.update(numbers[w] for w in source_tokens if w in numbers)
        tokens = set(re.findall(r"[a-z0-9']+", d.get('text', '').lower().replace('’', "'")))
        return bool(tokens & words or any(len(w) >= 4 and w in t for w in words for t in tokens))
    return True


def held_boxes(scan, cuts=(), policy=None, spoken=None):
    """Caption boxes per frame, each held HOLD frames before and after its OCR hit, inside its own shot only: held across
    a hard cut, the label lines of a bottle close-up were inpainted over the arm in the first frames of the next shot
    (Lymphoria G09, 2026-11-11)."""
    W, H, n = scan['width'], scan['height'], scan['frame_count']
    per = {}
    for d in scan['detections']:
        # Low-confidence prop print can resemble a caption-sized line: e.g.
        # a soda cup logo read as "40 092)" at confidence .3 erased the logo and
        # softened the driver's thumb. Reliable adjacent subtitle reads still
        # cover fades through HOLD; weak OCR by itself cannot erase the prop.
        if reference_caption(d, W, H, policy, spoken):
            per.setdefault(d['frame'], []).append(d['bbox'])
    held = {}
    for f, boxes in per.items():
        lo = max((c for c in cuts if c <= f), default=0); hi = min((c for c in cuts if c > f), default=n)
        for g in range(max(lo, f - HOLD), min(hi, f + HOLD + 1)): held.setdefault(g, []).extend(boxes)
    return held, per


def survivors(video, held, work):
    """Readable text still sitting inside an area that was masked: {frame: [texts]}. The cleaned clip is read again by
    OCR, so a caption the inpaint missed is reported instead of being sent to the generator."""
    video = Path(video); scan_path = Path(work)/(video.stem + '.after.text.json')
    subprocess.run([str(cc.scanner(work)), str(video), str(scan_path)], check=True, capture_output=True)
    scan = json.loads(scan_path.read_text()); W, H = scan['width'], scan['height']; masks = {}; left = {}
    for d in scan['detections']:
        f = d['frame']; x, y, w, h = d['bbox']
        if f not in held or d.get('confidence', 1) < 0.5 or sum(c.isalnum() for c in d['text']) < 4: continue
        if f not in masks: masks[f] = line_mask((H, W), held[f])
        if masks[f][min(H - 1, y + h // 2), min(W - 1, x + w // 2)]: left.setdefault(f, []).append(d['text'])
    return left


def clean(clip, out, work=None, keep_audio=True, pixels=None, extra=None):
    """OCR `clip` for caption boxes and inpaint the caption lines with LaMa. With `pixels` ({path, ss, t, vf, size, crf,
    maxrate, bufsize, audio_copy, pad_s}) the frames are decoded from that source window instead of from `clip`
    (the same window already scaled to `size`), so the cleaned reference is still one encode from the source.
    `extra` ({frame: [boxes]} in this clip's frames and pixels) adds more regions, e.g. the floating watermark."""
    clip, out = Path(clip).resolve(), Path(out).resolve()
    work = Path(work or out.parent/'caption_work').resolve(); work.mkdir(parents=True, exist_ok=True)
    scan_path = work/(clip.stem + '.text.json')
    # watermark_boxes has already OCR'd this exact, immutable reference into
    # this folder. Reuse the completed native scan through the standard cache.
    scan = cc._scan(clip, work)
    if Path(scan['source']).resolve() != clip or scan.get('frame_count', 0) <= 0:
        raise RuntimeError('Caption OCR cache does not describe the actual reference')
    policy = (pixels or {}).get('caption_policy')
    spoken = (pixels or {}).get('source_spoken')
    held, raw = held_boxes(scan, shot_starts(clip), policy, spoken)
    caption_held = {f: list(boxes) for f, boxes in held.items()}
    for f, boxes in (extra or {}).items(): held.setdefault(int(f), []).extend(boxes)
    W, H, rate = cc.probe(clip)
    if pixels:
        src = str(Path(pixels['path']).resolve()); win = ['-ss', f"{pixels['ss']:.6f}", '-t', f"{pixels['t']:.6f}", '-i', src]
        if tuple(pixels['size']) != (W, H): raise SystemExit(f"pixel source size {pixels['size']} does not match OCR clip {W}x{H}")
        dec_cmd = [cc.FFMPEG, '-v', 'error', *win, '-map', '0:v:0', *(['-vf', pixels['vf']] if pixels.get('vf') else []), '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
        audio = (['-ss', f"{pixels['ss']:.6f}", '-t', f"{pixels['t']:.6f}", '-i', src, '-map', '0:v:0', '-map', '1:a:0?',
                  *(['-c:a', 'copy'] if pixels.get('audio_copy') else ['-c:a', 'aac', '-b:a', '256k']),
                  *(['-af', f"apad=pad_dur={pixels['pad_s']:.6f}"] if pixels.get('pad_s') else [])] if keep_audio else ['-an'])
        crf, rate_ctl = str(pixels.get('crf', 12)), (['-maxrate', pixels['maxrate'], '-bufsize', pixels.get('bufsize', pixels['maxrate'])] if pixels.get('maxrate') else [])
        rate_ctl += list(pixels.get('out_t') or [])
    else:
        dec_cmd = [cc.FFMPEG, '-v', 'error', '-i', str(clip), '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
        audio = ['-i', str(clip), '-map', '0:v:0', '-map', '1:a:0?', '-c:a', 'copy'] if keep_audio else ['-an']
        crf, rate_ctl = '12', []
    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE)
    enc = subprocess.Popen([cc.FFMPEG, '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', rate, '-i', '-', *audio,
                            '-c:v', 'libx264', '-preset', 'fast', '-crf', crf, *rate_ctl, '-profile:v', 'high', '-pix_fmt', 'yuv420p',
                            '-movflags', '+faststart', str(out)], stdin=subprocess.PIPE)
    size = W * H * 3; i = 0; touched = 0; pending = []; stats0 = cache_stats()
    plated, plate_frames_left = {}, []          # frame -> backplates found there; frames whose plate came back

    def flush():
        nonlocal touched
        import agent_native
        from concurrent.futures import ThreadPoolExecutor
        todo = [k for k, (_, m) in enumerate(pending) if m is not None]
        if todo:
            with agent_native.local_slot('lama'):
                with ThreadPoolExecutor(WORKERS) as pool:
                    fixed = dict(zip(todo, pool.map(lambda k: inpaint(pending[k][0], pending[k][1]), todo)))
        else:
            fixed = {}
        touched += len(fixed)
        first = i - len(pending)
        for k, (f, _) in enumerate(pending):
            g = fixed.get(k, f)
            if (first + k) in plated and plate_left(g, plated[first + k]): plate_frames_left.append(first + k)
            enc.stdin.write(g.tobytes())
        pending.clear()

    while True:
        buf = dec.stdout.read(size)
        if len(buf) < size: break
        frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
        mask = line_mask(frame.shape, held[i]) if i in held else None
        if policy and policy.get('white_glyph_prop_protection') and (
                not policy.get('white_glyph_prop_frames') or
                any(a <= i < b for a, b in policy['white_glyph_prop_frames'])):
            mask = white_caption_mask(frame, caption_held.get(i, []))
            if (extra or {}).get(i):
                mask = cv2.bitwise_or(mask, line_mask(frame.shape, extra[i]))
        pm, found = plate_mask(frame, caption_held.get(i, [])) if i in caption_held else (None, [])
        if pm is not None:
            mask = pm if mask is None else cv2.bitwise_or(mask, pm)
            plated[i] = found
        pending.append((frame.copy(), mask if mask is not None and mask.any() else None)); i += 1
        if len(pending) >= WORKERS * 8: flush()
    flush(); enc.stdin.close(); enc.wait(); dec.wait()
    if enc.returncode or dec.returncode: raise SystemExit('caption clean encode failed')
    if i != scan['frame_count']:
        raise SystemExit(f'caption OCR/pixel frame mismatch: OCR {scan["frame_count"]}, pixels {i}')
    texts = sorted({d['text'] for d in scan['detections'] if reference_caption(d, scan['width'], scan['height'], policy, spoken)})
    left = survivors(out, held, work)
    # leftover text that is the caption itself, not a label or sign that sat inside the caption band (2026-11-20 film
    # 1753 G23: five bottles at caption height; their smeared labels read as "Dronage", "bmplinta" on 14 frames and
    # stopped the whole batch although every caption was gone)
    # and only captions of what is said here: shirt print that sat in the caption band ("COVINGTON IRON", 2026-11-22
    # film 998 G04, 41 frames) was masked as a caption and its smeared remains stopped the segment
    said = {w for t in (spoken or []) for w in _fold_words(t)}
    said_texts = [t for t in texts if TITLE_CARD.fullmatch(t.strip()) or not said or any(
        w in said or any(difflib.SequenceMatcher(None, w, x).ratio() >= 0.8 for x in said) for w in _fold_words(t))]
    left_caption = {k: v for k, v in left.items() if any(caption_word(t, said_texts) for t in v)}
    for f in plate_frames_left:                      # an empty plate has no text for OCR to read, but it is the caption
        left_caption.setdefault(f, []).append('[caption backplate]'); left.setdefault(f, []).append('[caption backplate]')
    report = {'type': 'lymphloop_caption_clean', 'method': 'lama_line_mask', 'source': str(clip), 'output': str(out), 'frames': i,
              'frames_cleaned': touched, 'frames_with_ocr_captions': len(raw), 'watermark_frames': len(extra or {}), 'caption_texts': texts[:40],
              'frames_left': len(left), 'texts_left': sorted({t for v in left.values() for t in v})[:20],
              'frames_left_caption': len(left_caption), 'texts_left_caption': sorted({t for v in left_caption.values() for t in v})[:20],
              'frames_with_backplate': len(plated), 'frames_left_backplate': len(plate_frames_left),
              'audio': 'kept' if keep_audio else 'removed', 'pixels_from': pixels['path'] if pixels else str(clip),
              'lama_cache': {k: v - stats0[k] for k, v in cache_stats().items()}}
    if policy and policy.get('white_glyph_prop_protection'):
        report['method'] = 'lama_reviewed_white_glyph_prop_protection'
    out.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return report


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('clip'); p.add_argument('out'); p.add_argument('--work'); p.add_argument('--no-audio', action='store_true')
    a = p.parse_args()
    print(json.dumps(clean(a.clip, a.out, a.work, keep_audio=not a.no_audio), ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()


def _fold_words(text):
    import unicodedata
    t = unicodedata.normalize('NFKD', str(text or '').lower())
    return [w for w in re.sub(r'[^a-z0-9 ]', ' ', ''.join(c for c in t if not unicodedata.combining(c))).split() if len(w) >= 4]


def caption_word(text, caption_texts):
    """True when a leftover OCR text shares a word (4+ letters, fuzzy) with the captions this clip had."""
    import difflib
    cap = {w for t in caption_texts for w in _fold_words(t)}
    return any(w in cap or any(difflib.SequenceMatcher(None, w, c).ratio() >= 0.8 for c in cap) for w in _fold_words(text))
