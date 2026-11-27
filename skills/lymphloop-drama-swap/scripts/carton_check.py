#!/usr/bin/env python3
"""Measured carton check: the carton's proportions in a generated clip, by numbers, not by eye.

Why (2026-11-24, user: "盒装产品比例变形的问题还是被放过了"): an agent that watched every clip of a 62-film batch passed
cartons whose LYMPHATIC front was 1.9-2.0 times taller than wide (real 1.3), hero shots that stood the carton on its
narrow logo side, and a carton squeezed into a car cup holder like a bottle -- all as "carton clean front face & scale".
The review rule allowed "about a quarter" off the true ratio and "a tilted carton may look a little short or tall", with
nothing to measure against, so every judgement leaned to pass; and `review` (the Flash carton check) was never run.

What it measures (every 0.25 s, frames scaled to 360x640):
  - the teal carton: connected teal regions bigger than 1.2 % of the frame, their outline ratio (long / short side of
    the smallest rotated rectangle) and size;
  - the front: OCR finds the word LYMPHATIC; on the real carton it spans 0.81 of the front's width (calibrated on
    assets/product/carton-hand-scale.jpg), so the front width is word width / 0.81 and the front's height:width is the
    carton height over that.
Real numbers: front 13:10 = 1.3 (1.23 measured this way on the real photo), side face 13:7 = 1.86, top 10:7 = 1.43.
A carton tilted back shows its top and stays under about 1.5. The user's rule (2026-11-20: "只要比例差不多正确，不要大幅度
偏差都不算瑕疵") is kept as numbers: a quarter off is the line.

Flags (each needs two samples in a row, i.e. half a second):
  front_stretched  LYMPHATIC visible and front height:width > 1.65 (1.3 + a quarter)      -> goof `shape`
  too_slim         outline ratio > 2.3 (beyond the side face's 1.86 + a quarter), carton-only segment -> `shape`
  side_as_front    an upright carton outline 1.6-2.3, at least 4 % of the frame, with no LYMPHATIC on it: the
                   narrow logo side stands as the front (a display shot)                  -> goof `side`
In a segment that also shows packets (`mixed`), outlines over 2.8 are taken for packets and not flagged. Regions that
touch the frame edge (a box cut off by the picture) and regions that do not fill their rectangle (teal clothes) are not
measured. Batch 62 (2026-11-24, 312 carton segments): 27 flagged, about 70 % real goofs (fronts 1.8-2.5 tall, side as
front, a box in a cup holder); the false ones were a carton with its lid flipped up (up to 2.0 tall: lid 7 cm on 13
cm), a packet in hand, a phone screen and an out-of-focus box whose word OCR could not read -- the agent looks at
the sheet and passes those with --carton-ok.

    carton_check.py clip VIDEO [--from S --to S] [--mixed] [--out JSON]
    carton_check.py run RUN [--segment G05]          # every selected generated segment whose jobs sent carton images
    carton_check.py batch ROOT [--films A,B]         # a finished batch: ROOT/carton_audit.md lists every flagged segment
`swap.py select` runs it on carton segments and refuses a flagged clip until the agent either puts the goof in the
round (goofs.json) or passes --carton-ok "看过 sheet：<why this is not a goof>" (lid up, a packet, a mis-segmented
background object). The sheet with every flagged frame, box and number is written next to the JSON.
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media  # noqa: E402

W, H = 360, 640
FPS = 4
TEAL_LO, TEAL_HI = (78, 80, 50), (102, 255, 255)     # OpenCV HSV: the carton and packet teal
MIN_AREA = 0.012
WORD_FRONT = 0.81          # LYMPHATIC width / front width on the real carton
FRONT_TRUE, FRONT_MAX, FRONT_MIN = 1.3, 1.65, 0.85
SIDE_TRUE, SLIM_MAX = 1.86, 2.3
PACKET_MIN = 2.8
SIDE_FRONT_MIN, SIDE_FRONT_AREA = 1.6, 0.04
RUN_MIN = 2                # samples in a row (0.5 s)
RECT_MIN = 0.7             # region area / its rotated rectangle: a box face is close to 1
EDGE = 3                   # pixels from the frame edge: a region touching it is cut off, not measured
WORD_RE = re.compile(r'YMPHAT|LYMPHA|MPHATIC|PHATIC')
GOOF_OF = {'front_stretched': 'shape', 'front_squat': 'shape', 'too_slim': 'shape', 'side_as_front': 'side'}
WHAT = {'front_stretched': '正面（印 LYMPHATIC 的面）被拉高：高:宽 {v:.2f}，真盒 1.3，超过 1.65 算比例失调',
        'front_squat': '正面被压扁：高:宽 {v:.2f}，真盒 1.3，低于 0.85 算比例失调',
        'too_slim': '纸盒外形细长如瓶：长:短 {v:.2f}，真盒任何一面都不超过 1.86，超过 2.3 算比例失调',
        'side_as_front': '纸盒竖着站、看不到 LYMPHATIC 正面，外形 {v:.2f}（窄侧面 1.86）：窄侧面当正面'}


def ff(): return media.binary('ffmpeg')


def samples(video, t0=0.0, t1=None, work=None):
    """[(t, frame BGR)], and the small clip the frames came from (for OCR)."""
    import numpy as np
    work = Path(work or tempfile.mkdtemp(prefix='carton-check-')); work.mkdir(parents=True, exist_ok=True)
    clip = work/'sample.mp4'
    cmd = [ff(), '-v', 'error', '-y', '-ss', f'{t0:.3f}', '-i', str(video)]
    if t1 is not None: cmd += ['-t', f'{max(0.05, t1 - t0):.3f}']
    cmd += ['-vf', f'fps={FPS},scale={W}:{H}', '-an', '-c:v', 'libx264', '-crf', '14', '-pix_fmt', 'yuv420p', str(clip)]
    subprocess.run(cmd, check=True)
    raw = subprocess.run([ff(), '-v', 'error', '-i', str(clip), '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], capture_output=True, check=True).stdout
    n = len(raw) // (W * H * 3)
    frames = [np.frombuffer(raw[k * W * H * 3:(k + 1) * W * H * 3], np.uint8).reshape(H, W, 3) for k in range(n)]
    return [(t0 + k / FPS, f) for k, f in enumerate(frames)], clip


def words(clip, work):
    """{sample index: [LYMPHATIC boxes]} by OCR (the skill's text scanner, see vision_scan.py)."""
    import caption_clean
    out = Path(work)/'ocr.json'
    # one compiled scanner for every check on this machine (swiftc takes seconds; parallel checks share the lock)
    subprocess.run([*caption_clean.argv(caption_clean.scanner(Path(tempfile.gettempdir())/'lymphloop-carton-ocr')), str(clip), str(out)], check=True, capture_output=True)
    by = {}
    for d in json.loads(out.read_text()).get('detections') or []:
        if WORD_RE.search(str(d.get('text', '')).upper()):
            x, y, w, h = d['bbox']
            if w > 1.8 * h: by.setdefault(int(d['frame']), []).append([x, y, w, h])
    return by


def blobs(img):
    import cv2, numpy as np
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = cv2.morphologyEx(cv2.inRange(hsv, TEAL_LO, TEAL_HI), cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    out = []
    for c in cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        a = cv2.contourArea(c)
        if a < MIN_AREA * W * H: continue
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
        hull = cv2.contourArea(cv2.convexHull(c))
        if a / max(1.0, hull) < 0.75: continue                # ragged regions: clothes, water, background, not a box
        # a box face fills its rotated rectangle; a teal shirt or a box merged with a teal blouse does not (2026-11-24
        # batch 62: 5 of 40 too_slim flags were clothes)
        if a / max(1.0, rw * rh) < RECT_MIN: continue
        x, y, w, h = cv2.boundingRect(c)
        # a box cut by the frame edge cannot be measured: its visible part is shorter or narrower than the box (batch 62:
        # most front_squat and many too_slim flags were cartons running off the bottom or side of the picture)
        if x <= EDGE or y <= EDGE or x + w >= W - EDGE or y + h >= H - EDGE: continue
        out.append({'box': [x, y, w, h], 'outline': round(max(rw, rh) / max(1.0, min(rw, rh)), 2), 'area': round(a / (W * H), 3)})
    return out


def judge(blob, ws, mixed):
    """(flag, value) for one carton region in one sample, or (None, value)."""
    x, y, w, h = blob['box']
    inside = [b for b in ws if x - 4 <= b[0] + b[2] / 2 <= x + w + 4 and y - 4 <= b[1] + b[3] / 2 <= y + h + 4]
    if inside:
        front_w = max(b[2] for b in inside) / WORD_FRONT
        if front_w > 1.15 * w: return None, None              # the word is wider than the region: an occluded box
        r = round(h / front_w, 2)
        if r > FRONT_MAX: return 'front_stretched', r
        # a squat front is not flagged: on batch 62 every front under 0.85 was a box whose bottom was hidden by a hand or
        # the counter edge (FRONT_MIN stays documented; Flash's too_squat and the agent's eye cover the real case)
        return None, r
    o = blob['outline']
    if o >= PACKET_MIN and mixed: return None, o                  # a packet
    if o > SLIM_MAX: return 'too_slim', o
    if o >= SIDE_FRONT_MIN and h > w and blob['area'] >= SIDE_FRONT_AREA: return 'side_as_front', o
    return None, o


def check(video, t0=0.0, t1=None, mixed=False, out=None, work=None):
    """Measure a clip. Returns {flags: [{kind, goof, from, to, worst, what, box}], samples, sheet}."""
    work = Path(work or tempfile.mkdtemp(prefix='carton-check-'))
    smp, clip = samples(video, t0, t1, work)
    ws = words(clip, work)
    rows = []
    for k, (t, img) in enumerate(smp):
        hits = []
        for b in blobs(img):
            f, v = judge(b, ws.get(k, []), mixed)
            if f: hits.append({'kind': f, 'value': v, 'box': b['box']})
        rows.append((t, hits))
    flags, cur = [], {}
    for k, (t, hits) in enumerate(rows + [(None, [])]):
        kinds = {h['kind']: h for h in hits}
        for kind in list(cur):
            if kind not in kinds:
                r = cur.pop(kind)
                if r['n'] >= RUN_MIN: flags.append(r)
        for kind, h in kinds.items():
            r = cur.setdefault(kind, {'kind': kind, 'from': t, 'to': t, 'n': 0, 'worst': h['value'], 'at': t, 'box': h['box'], 'k': k})
            r['to'] = t; r['n'] += 1
            worse = h['value'] > r['worst'] if kind != 'front_squat' else h['value'] < r['worst']
            if worse: r.update(worst=h['value'], at=t, box=h['box'], k=k)
    for f in flags:
        f.update(goof=GOOF_OF[f['kind']], what=WHAT[f['kind']].format(v=f['worst']), to=round(f['to'] + 1 / FPS, 2), **{'from': round(f['from'], 2)})
    res = {'type': 'lymphloop_carton_check', 'video': str(video), 'from_s': t0, 'to_s': t1, 'mixed': mixed, 'samples': len(smp),
           'thresholds': {'front_true': FRONT_TRUE, 'front_max': FRONT_MAX, 'front_min': FRONT_MIN, 'side_true': SIDE_TRUE,
                          'slim_max': SLIM_MAX, 'packet_min': PACKET_MIN},
           'flags': [{k: f[k] for k in ('kind', 'goof', 'from', 'to', 'worst', 'at', 'what', 'box')} for f in flags]}
    if flags:
        res['sheet'] = str(sheet(smp, flags, Path(out).with_suffix('.png') if out else work/'flags.png', video))
    if out: Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res


CROP_FPS = 3               # product crops for the agent's review: three a second
CROP_MIN_AREA = 0.002      # any teal region this big (0.2 % of the frame) is cropped: small, hidden and moving boxes too
CROPS_PER_SHEET = 40


def product_crops(video, t0, t1, out_dir, label_t0=None, name='crops'):
    """Every appearance of the product in [t0, t1) of a clip, cut out at FULL resolution, three a second, on sheets for
    the agent's own eyes (user 2026-11-24, after a redo round: "可是我看着还是有产品穿帮镜头"). The measurement only flags
    what it can measure; boxes that were small, half hidden by a hand or moving (a thin card into a cup holder, a tall
    box taken from a cupboard, a stretched box under a hand) passed it, and a 180 px thumbnail sheet hid them from the
    reviewer as well. Cut out large, the same boxes are plain to see. Returns the sheet paths."""
    import cv2, numpy as np
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    info = media.probe(str(video)); vw, vh = int(info['video']['width']), int(info['video']['height'])
    raw = subprocess.run([ff(), '-v', 'error', '-ss', f'{t0:.3f}', '-i', str(video), '-t', f'{max(0.05, t1 - t0):.3f}', '-vf', f'fps={CROP_FPS}',
                          '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], capture_output=True, check=True).stdout
    n = len(raw) // (vw * vh * 3); cells = []
    lab0 = t0 if label_t0 is None else label_t0
    for k in range(n):
        im = np.frombuffer(raw[k * vw * vh * 3:(k + 1) * vw * vh * 3], np.uint8).reshape(vh, vw, 3)
        m = cv2.morphologyEx(cv2.inRange(cv2.cvtColor(im, cv2.COLOR_BGR2HSV), TEAL_LO, TEAL_HI), cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        cs = sorted((c for c in cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0] if cv2.contourArea(c) > CROP_MIN_AREA * vw * vh),
                    key=cv2.contourArea, reverse=True)[:2]
        for c in cs:
            x, y, w, h = cv2.boundingRect(c); mg = int(max(w, h) * 0.5) + 20
            crop = Image.fromarray(cv2.cvtColor(im[max(0, y - mg):y + h + mg, max(0, x - mg):x + w + mg], cv2.COLOR_BGR2RGB)); crop.thumbnail((260, 260))
            cell = Image.new('RGB', (260, 280), (20, 20, 20)); cell.paste(crop, ((260 - crop.width) // 2, 20))
            ImageDraw.Draw(cell).text((4, 3), f'{lab0 + k / CROP_FPS:.2f}s', fill='yellow'); cells.append(cell)
    sheets = []
    for i in range(0, len(cells), CROPS_PER_SHEET):
        ch = cells[i:i + CROPS_PER_SHEET]; cols = 8
        S = Image.new('RGB', (260 * cols, 280 * ((len(ch) + cols - 1) // cols)), (20, 20, 20))
        for j, c in enumerate(ch): S.paste(c, ((j % cols) * 260, (j // cols) * 280))
        path = out_dir/f'{name}_{i // CROPS_PER_SHEET + 1:02d}.jpg'; S.save(path, quality=85); sheets.append(str(path))
    return sheets


REF_DIR = HERE.parent/'assets'/'product'
_REFS = {}


def reference_face(kind):
    """The real carton cut out of the product photos: the front (carton-hand-scale.jpg) for front flags, the narrow
    logo side (top-right view of carton-views.jpg) for slim / side flags. PIL image, cached."""
    from PIL import Image
    import cv2, numpy as np
    key = 'front' if kind.startswith('front') else 'side'
    if key in _REFS: return _REFS[key]
    if key == 'front':
        im = Image.open(REF_DIR/'carton-hand-scale.jpg').convert('RGB')
    else:
        v = Image.open(REF_DIR/'carton-views.jpg').convert('RGB'); w, h = v.size
        im = v.crop((2 * w // 3, 0, w, h // 3))
    a = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2HSV)
    m = cv2.morphologyEx(cv2.inRange(a, TEAL_LO, TEAL_HI), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    c = max(cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0], key=cv2.contourArea)
    x, y, bw, bh = cv2.boundingRect(c)
    _REFS[key] = im.crop((x, y, x + bw, y + bh)); return _REFS[key]


def full_frame(video, t):
    """The frame at t seconds at the clip's own resolution (the measuring frames are 360x640)."""
    from PIL import Image
    import io
    png = subprocess.run([ff(), '-v', 'error', '-ss', f'{max(0.0, t):.3f}', '-i', str(video), '-frames:v', '1', '-f', 'image2pipe',
                          '-vcodec', 'png', '-'], capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(png)).convert('RGB')


def sheet(smp, flags, path, video=None):
    """One row per flag, for the agent's own eyes (user 2026-11-24: "我想要的是让agent自己能检查出来"): the whole frame
    with the measured box, the box cut out at full resolution, and the REAL carton face scaled to the SAME HEIGHT next to
    it -- a stretched or slim box is then plain to see, without judging a ratio from a small picture."""
    from PIL import Image, ImageDraw
    import cv2
    BOX_H = 420; rows = []
    for f in flags:
        small = Image.fromarray(cv2.cvtColor(smp[f['k']][1], cv2.COLOR_BGR2RGB)); d = ImageDraw.Draw(small)
        x, y, w, h = f['box']; d.rectangle((x, y, x + w, y + h), outline='red', width=3)
        ctx = small.resize((int(W * BOX_H / H * 1.2), int(BOX_H * 1.2)))
        try:
            big = full_frame(video, f['at']) if video else small
        except Exception:
            big = small
        k = big.width / W; mx, my = int(w * k * 0.08), int(h * k * 0.08)
        crop = big.crop((max(0, int(x * k) - mx), max(0, int(y * k) - my), min(big.width, int((x + w) * k) + mx), min(big.height, int((y + h) * k) + my)))
        s = BOX_H / (h * k); crop = crop.resize((max(1, int(crop.width * s)), max(1, int(crop.height * s))))
        ref = reference_face(f['kind']); rs = BOX_H / ref.height; ref = ref.resize((max(1, int(ref.width * rs)), BOX_H))
        true = FRONT_TRUE if f['kind'].startswith('front') else SIDE_TRUE
        Wr = ctx.width + crop.width + ref.width + 60; Hr = max(ctx.height, crop.height, BOX_H) + 70
        row = Image.new('RGB', (Wr, Hr), (24, 24, 24)); d = ImageDraw.Draw(row)
        row.paste(ctx, (10, 60)); row.paste(crop, (ctx.width + 30, 60)); row.paste(ref, (ctx.width + crop.width + 50, 60 + (crop.height - BOX_H) // 2))
        d.text((10, 8), f"{f['kind']}  {f['from']:.2f}-{f['to']:.2f}s (worst @{f['at']:.2f}s)", fill='yellow')
        d.text((10, 26), f"GENERATED {'front h:w' if f['kind'].startswith('front') else 'outline'} {f['worst']}   |   REAL carton {true}   |   line {FRONT_MAX if f['kind'].startswith('front') else SLIM_MAX}", fill='yellow')
        d.text((ctx.width + 30, 44), 'generated box (cut out)', fill='white')
        d.text((ctx.width + crop.width + 50, 44), 'REAL carton, same height', fill='white')
        rows.append(row)
    S = Image.new('RGB', (max(r.width for r in rows), sum(r.height for r in rows)), (24, 24, 24)); yy = 0
    for r in rows: S.paste(r, (0, yy)); yy += r.height
    S.save(path); return path


CARTON_NAMES = ('carton-views', 'carton-hand', 'carton_views', 'carton_hand')
PACKET_NAMES = ('stick-handheld', 'opened-packet', 'handheld_photo', 'opened_packet')


def segment_kind(run, seg_id):
    """'carton' (only carton images were sent for this segment), 'mixed' (cartons and packets), or None (no carton)."""
    run = Path(run); names = []
    for p in list((run/'jobs').glob(f'{seg_id}-*.json')) + list((run/'generation'/'jobs').glob(f'{seg_id}-*.json')):
        try: d = json.loads(p.read_text())
        except Exception: continue
        job = d.get('job') if d.get('type') == 'apimart_job_ledger' else d
        for r in (job or {}).get('references') or []:
            names.append(Path(str(r.get('path', ''))).name + ' ' + str(r.get('view', '')))
    carton = any(any(n in x for n in CARTON_NAMES) for x in names)
    if not carton: return None
    return 'mixed' if any(any(n in x for n in PACKET_NAMES) for x in names) else 'carton'


def run_check(run, segment=None):
    """Every selected generated segment of a RUN whose jobs sent carton images: [{segment, kind, flags, sheet, carton_ok}].
    Writes RUN/qa/<job>-<cand>.carton.json and .png (the side-by-side sheet)."""
    run = Path(run); sel = json.loads((run/'plan/selections.json').read_text()); report = []
    for seg_id, e in sorted(sel.items()):
        if segment and seg_id != segment: continue
        if not isinstance(e, dict) or e.get('picture') == 'source' or e.get('reused_from'): continue
        kind = segment_kind(run, seg_id)
        if not kind: continue
        video = (e.get('media_uncaptioned') or e.get('media') or {}).get('path')
        if not video or not Path(video).is_file(): continue
        out = run/'qa'/f"{e['job']}-{e.get('candidate') or 'c1'}.carton.json"; out.parent.mkdir(exist_ok=True)
        r = check(video, float(e.get('clip_in') or 0), float(e['clip_out']) if e.get('clip_out') else None, kind == 'mixed', out,
                  work=run/'qa'/'carton_work'/f"{e['job']}-{e.get('candidate') or 'c1'}")
        report.append({'segment': seg_id, 'job': e['job'], 'kind': kind, 'flags': r['flags'], **({'sheet': r['sheet']} if r.get('sheet') else {}),
                       **({'carton_ok': e['carton_check'].get('ok')} if (e.get('carton_check') or {}).get('ok') else {})})
    return report


def batch(root, films=None, workers=3):
    """A finished batch (a folder of RUNs): every film checked, one summary for the agent to work through
    (ROOT/carton_audit.json + .md: each flagged segment with its sheet). Nothing is changed in the films."""
    from concurrent.futures import ThreadPoolExecutor
    root = Path(root)
    runs = [p.parent.parent for p in sorted(root.glob('*/plan/selections.json'))]
    if films: runs = [r for r in runs if r.name in films]
    def one(r):
        try: return r.name, run_check(r), None
        except Exception as e: return r.name, [], f'{type(e).__name__}: {e}'[:300]
    with ThreadPoolExecutor(workers) as ex: res = list(ex.map(one, runs))
    summary = {'type': 'lymphloop_carton_audit', 'root': str(root), 'films': len(res), 'checked_segments': sum(len(r) for _, r, _ in res),
               'errors': {n: e for n, _, e in res if e},
               'flagged': [{'film': n, **x} for n, rep, _ in res for x in rep if x['flags'] and not x.get('carton_ok')]}
    (root/'carton_audit.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    md = [f"# 纸盒比例自检（{summary['films']} 条片，{summary['checked_segments']} 个纸盒段，标出 {len(summary['flagged'])} 段）", '',
          '每一行先用 read_file 打开 sheet 亲自看（左：整帧和量的框；中：生成的盒子；右：真盒同高）。是穿帮就写进这条片的 goofs.json'
          '（why 用 goof 列，at 用秒数列）走人物卡补做；是量错了（开盖盖子立起、条包、手机屏、虚焦）就记下理由放行。', '',
          '| 片子 | 段 | 类型 | 补做原因 | 生成片内秒数 | 量出 | sheet |', '|---|---|---|---|---|---|---|']
    for x in summary['flagged']:
        for f in x['flags']:
            md.append(f"| {x['film']} | {x['segment']} | {f['kind']} | {f['goof']} | {f['from']:.2f}-{f['to']:.2f} | {f['worst']} | {x.get('sheet', '')} |")
    (root/'carton_audit.md').write_text('\n'.join(md) + '\n')
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest='cmd', required=True)
    q = s.add_parser('clip'); q.add_argument('video'); q.add_argument('--from', dest='t0', type=float, default=0.0)
    q.add_argument('--to', dest='t1', type=float); q.add_argument('--mixed', action='store_true'); q.add_argument('--out')
    q = s.add_parser('run'); q.add_argument('run'); q.add_argument('--segment')
    q = s.add_parser('batch', help='every RUN under a batch folder; writes ROOT/carton_audit.json and .md')
    q.add_argument('root'); q.add_argument('--films', help='FILM,FILM'); q.add_argument('--workers', type=int, default=3)
    a = p.parse_args()
    if a.cmd == 'clip':
        print(json.dumps(check(a.video, a.t0, a.t1, a.mixed, a.out), ensure_ascii=False, indent=1)); return
    if a.cmd == 'run':
        rep = run_check(a.run, a.segment)
        print(json.dumps({'run': str(a.run), 'segments': rep, 'flagged': [x['segment'] for x in rep if x['flags'] and not x.get('carton_ok')]},
                         ensure_ascii=False, indent=1)); return
    sm = batch(a.root, set(a.films.split(',')) if a.films else None, a.workers)
    print(json.dumps({k: sm[k] for k in ('films', 'checked_segments', 'errors')} | {'flagged': len(sm['flagged']),
                      'summary': str(Path(a.root)/'carton_audit.md')}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
