#!/usr/bin/env python3
"""Put captions back on the generated segments, in the film's own caption style (user, 2026-11-13: "仿照整条视频原本的
字幕，把生成段的字幕补上，就补在原本字幕所属的位置，字幕内容就是生成段的台词").

Generated segments carry no captions (the reference is cleaned before it is sent), while the untouched shots around them
keep theirs, so captions came and went inside one film. Two steps, both local and free:

  measure   the film's caption style from the uncleaned references of its own segments (RUN/clips/caption_work/
            G##.hq.ref.text.json + G##.hq.ref.mp4): where the block sits, the letter size, the widest line, the fill and
            outline colours, the outline width, the closest installed font, how sentences are cased and punctuated.
            -> RUN/captions/style.json (the agent may edit it) + RUN/captions/style-preview.jpg (original | imitation)
  burn      for one selected clip: the words actually spoken in it (faster-whisper timings), spelled as the prompt wrote
            them, grouped the way the film groups them, drawn at the measured place. -> <clip>.captioned.mp4

`swap.py captions --run RUN` runs both for every selected generated segment.
"""
from __future__ import annotations
import argparse, difflib, json, re, statistics, subprocess
from pathlib import Path
import media_pipeline as media

FONT_FILES = [  # sans faces that ship with macOS; every face in a collection is tried, plus whatever the user installed
    '/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf', '/System/Library/Fonts/Supplemental/Arial Black.ttf',
    '/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf', '/System/Library/Fonts/Helvetica.ttc', '/System/Library/Fonts/HelveticaNeue.ttc',
    '/System/Library/Fonts/Avenir.ttc', '/System/Library/Fonts/Avenir Next.ttc', '/System/Library/Fonts/Supplemental/Futura.ttc',
    '/System/Library/Fonts/Supplemental/GillSans.ttc', '/System/Library/Fonts/Supplemental/Verdana.ttf', '/System/Library/Fonts/Supplemental/Verdana Bold.ttf',
    '/System/Library/Fonts/Supplemental/Tahoma.ttf', '/System/Library/Fonts/Supplemental/Tahoma Bold.ttf', '/System/Library/Fonts/Supplemental/Trebuchet MS.ttf',
    '/System/Library/Fonts/Supplemental/Trebuchet MS Bold.ttf', '/System/Library/Fonts/Supplemental/Impact.ttf', '/System/Library/Fonts/SFNSRounded.ttf',
    # narrow faces: many ads caption in a condensed sans (NV0183, 2026-11-13: DIN Condensed-like captions fell back to Impact)
    '/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf', '/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf',
    '/System/Library/Fonts/Supplemental/Arial Narrow.ttf', '/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf',
]
FONT_DIRS = [Path.home() / 'Library/Fonts', Path('/Library/Fonts')]
SKIP_FACE = re.compile(r'italic|oblique|light|thin', re.I)


def font_candidates():
    """[(name, file, index)] of the upright, not-too-light faces found on this machine."""
    from PIL import ImageFont
    files = [f for f in FONT_FILES if Path(f).is_file()]
    files += [str(f) for d in FONT_DIRS if d.is_dir() for f in sorted(d.iterdir()) if f.suffix.lower() in ('.ttf', '.otf', '.ttc')]
    out, seen = [], set()
    for f in files:
        for i in range(24):
            try: name = ' '.join(ImageFont.truetype(f, 20, index=i).getname())
            except Exception: break
            if name not in seen and not SKIP_FACE.search(name): seen.add(name); out.append((name, f, i))
            if not f.lower().endswith('.ttc'): break
    return out


SS = 3                 # supersampling when drawing
LEAD, HOLD = 0.06, 0.30   # s a caption comes up before its first word / stays after its last word
PAUSE = 0.70           # s of silence between two words that starts a new caption


def font(style, px):
    from PIL import ImageFont
    return ImageFont.truetype(style['font_file'], max(4, int(round(px))), index=int(style.get('font_index', 0)))


# ---------------------------------------------------------------- measure
def caption_events(scan, spoken=None):
    """The original captions of one scanned clip: [{'first','last','lines': [{'text','bbox'}]}], consecutive frames
    showing the same lines merged. Only confident, caption-placed text; with `spoken`, only text that repeats the lines."""
    import caption_clean as cc
    words = cc.spoken_words(spoken) if spoken else None
    per = {}
    for d in scan['detections']:
        if d.get('confidence', 1) >= 1 and cc.is_caption(d['bbox'], scan['width'], scan['height']) and (words is None or cc.spoken_caption(d['text'], words)):
            per.setdefault(d['frame'], []).append(d)
    events = []
    for f in sorted(per):
        lines = sorted(per[f], key=lambda d: d['bbox'][1]); key = tuple(d['text'] for d in lines)
        if events and events[-1]['key'] == key and f - events[-1]['last'] <= 3: events[-1]['last'] = f
        else: events.append({'key': key, 'first': f, 'last': f, 'lines': [{'text': d['text'], 'bbox': d['bbox']} for d in lines]})
    return [{k: v for k, v in e.items() if k != 'key'} for e in events]


def _frame(clip, n):
    import cv2
    cap = cv2.VideoCapture(str(clip)); cap.set(cv2.CAP_PROP_POS_FRAMES, n); ok, img = cap.read(); cap.release()
    return img if ok else None


def _ink(crop, color):
    """Pixels of the caption fill: close to the fill colour."""
    import numpy as np
    return (np.abs(crop.astype(np.int16) - np.array(color, np.int16)).max(axis=2) < 60)


def _render_mask(text, fnt):
    """The glyphs of `text` as a tight boolean array."""
    import numpy as np
    from PIL import Image, ImageDraw
    l, t, r, b = fnt.getbbox(text)
    img = Image.new('L', (max(1, r - l) + 4, max(1, b - t) + 4), 0)
    ImageDraw.Draw(img).text((2 - l, 2 - t), text, font=fnt, fill=255)
    a = np.array(img) > 127; ys, xs = np.where(a)
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1] if len(ys) else a


def _fit(sample, name, file, index):
    """The size at which this face draws the sample's text as wide as the film does, and how well the shapes agree.
    None when the face cannot draw this text (2026-11-22 film 2896957363997251: one installed face raised "division
    by zero" inside PIL getbbox and stopped the whole style measurement)."""
    try: return _fit_face(sample, name, file, index)
    except (OSError, ValueError, ZeroDivisionError, MemoryError): return None


def _fit_face(sample, name, file, index):
    import cv2, numpy as np
    from PIL import ImageFont
    obs = sample['ink']; target = obs.shape[1]
    try: ImageFont.truetype(file, 20, index=index)
    except OSError: return None
    lo, hi = 6, 200
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _render_mask(sample['text'], ImageFont.truetype(file, mid, index=index)).shape[1] < target: lo = mid
        else: hi = mid
    best = (0.0, lo)
    soft = lambda m, shape: cv2.GaussianBlur(cv2.copyMakeBorder(m.astype(np.float32), 4, shape[0] + 4 - m.shape[0], 3, 3, cv2.BORDER_CONSTANT), (0, 0), 1.2)
    for size in (lo, hi):
        m = _render_mask(sample['text'], ImageFont.truetype(file, size, index=index)).astype(np.uint8)
        m = cv2.resize(m, (obs.shape[1], max(1, int(round(m.shape[0] * obs.shape[1] / m.shape[1])))), interpolation=cv2.INTER_AREA)
        shape = (max(m.shape[0], obs.shape[0]), obs.shape[1]); A, B = soft(obs, shape), soft(m, shape)
        # blurred shapes compared at small shifts: letter forms and weight count, a one-pixel offset does not
        score = max(float((np.roll(np.roll(B, dy, 0), dx, 1) * A).sum() / np.sqrt((A * A).sum() * (B * B).sum() + 1e-9))
                    for dy in range(-3, 4) for dx in range(-2, 3))
        if score > best[0]: best = (score, size)
    return best


def _thinning(sample, file, index, size):
    """How many px the film's fill is thinner than the plain glyphs at the fitted size (its outline is drawn over the
    edge of the letters): (glyph area - fill area) / glyph perimeter, in the sample's pixels."""
    import cv2, numpy as np
    from PIL import ImageFont
    obs = sample['ink']
    m = _render_mask(sample['text'], ImageFont.truetype(file, size, index=index)).astype(np.uint8)
    m = cv2.resize(m, (obs.shape[1], max(1, int(round(m.shape[0] * obs.shape[1] / m.shape[1])))), interpolation=cv2.INTER_NEAREST)
    edge = m.astype(bool) & ~cv2.erode(m, np.ones((3, 3), np.uint8)).astype(bool)
    return (int(m.sum()) - int(obs.sum())) / max(1, int(edge.sum()))       # negative: the film's letters are heavier than this face


def _fill_colours(crop):
    """(fill BGR, highlight BGR or None) of one caption crop. Captions that colour the word being spoken (white letters,
    the current word yellow) were averaged into one pink-beige fill, and only the white words counted as letters, so a
    condensed face fitted best (2026-11-19, Spanish Lymphoria film: Impact, pink). Bright pixels are split into
    near-grey (the fill) and saturated (the highlight)."""
    import numpy as np
    px = crop.reshape(-1, 3).astype(np.int16)
    cand = px[px.max(axis=1) >= 150]
    if not len(cand): return [int(v) for v in np.median(px, axis=0)], None
    sat = cand.max(axis=1) - cand.min(axis=1)
    grey, colour = cand[sat < 60], cand[sat >= 60]
    top = lambda a: a[a.max(axis=1) >= np.percentile(a.max(axis=1), 80)]    # the letters' cores, not their anti-aliased edges
    if len(grey) >= 0.3 * len(cand):
        fill = [int(v) for v in np.median(top(grey), axis=0)]
        hi = [int(v) for v in np.median(top(colour), axis=0)] if len(colour) >= 0.08 * len(cand) else None
        return fill, hi
    return [int(v) for v in np.median(top(cand), axis=0)], None


def caption_colours(samples):
    """(fill RGB, highlight RGB or None) of the film. White captions with the spoken word in yellow: on short lines the
    yellow word and lamp-lit background outweigh the white letters, so most samples read as one yellow fill; the copy
    then kept only the yellow pixels and dropped every white word (2026-11-22 film 1659373095741717: 5 of 14 samples
    white + yellow, the median said yellow, captions came out as lone yellow words). A film where at least a quarter of
    the samples clearly show a white fill with a coloured word is a highlight film."""
    import numpy as np
    med = lambda xs: [int(v) for v in np.median(xs, axis=0)][::-1]
    # a real highlight is a strong colour (yellow 245,255,16; orange; red; magenta); skin or a lamp behind plain white
    # captions reads 228,165,159 (2026-11-22, films 1409 / 2836 / 1144) and is not one
    lit = [s for s in samples if s.get('highlight') and min(s['color']) >= 200 and max(s['highlight']) - min(s['highlight']) >= 120]
    if len(lit) >= max(2, 0.25 * len(samples)):
        return med([s['color'] for s in lit]), med([s['highlight'] for s in lit])
    hi = [s['highlight'] for s in samples if s.get('highlight')]
    return med([s['color'] for s in samples]), (med(hi) if len(hi) >= 0.5 * len(samples) else None)


def _backplate(crop, fill, highlight):
    """A coloured rectangle behind white glyphs, distinct from coloured glyphs."""
    import cv2, numpy as np
    if highlight is None: return None
    colour = _ink(crop, highlight).astype(np.uint8)
    white = _ink(crop, fill)
    count, _, stats, _ = cv2.connectedComponentsWithStats(colour)
    candidates = []
    for x, y, w, h, area in stats[1:]:
        if w < 20 or h < 10 or w < 1.1 * h or area < 0.3 * w * h: continue
        ys, xs = np.where(white[y:y+h, x:x+w])
        if len(xs) < max(20, 0.04 * w * h): continue
        # White letters must lie inside the coloured component's bounding box.
        candidates.append((int(area), {'box': [int(x), int(y), int(w), int(h)],
            'pad_x': max(1., float(np.median([xs.min(), w - 1 - xs.max()]))),
            'pad_y': max(1., float(np.median([ys.min(), h - 1 - ys.max()]))),
            'radius': float(h) * 0.23}))
    return max(candidates, key=lambda x: x[0])[1] if candidates else None


def _samples(clips, limit=14):
    """Single-line captions cut out of the original frames: {'text', 'ink' (tight fill mask), 'crop', 'height'}."""
    import numpy as np
    out = []
    for clip, scan, events in clips:
        for e in events:
            if len(out) >= limit: return out
            if len(e['lines']) != 1 or e['last'] - e['first'] < 6 or not 8 <= len(e['lines'][0]['text']) <= 26: continue
            text = e['lines'][0]['text']
            if re.search(r"[^A-Za-z' ?,]", text): continue
            img = _frame(clip, (e['first'] + e['last']) // 2)
            if img is None: continue
            x, y, w, h = [int(round(v)) for v in e['lines'][0]['bbox']]; p = int(h * 0.45)
            top, left = max(0, y - p), max(0, x - p)
            crop = img[top:y + h + p, left:x + w + p]
            color, highlight = _fill_colours(crop)
            backplate = _backplate(crop, color, highlight)
            ink = _ink(crop, color) | (_ink(crop, highlight) if highlight and not backplate else False)
            # OCR can miss a neighboring line. Its white pixels in the context
            # crop must not make this line look taller and select a condensed face.
            region = np.zeros_like(ink)
            region[max(0, y - top - 1):y - top + h + 1,
                   max(0, x - left - 1):x - left + w + 1] = True
            ink &= region
            ys, xs = np.where(ink)
            if len(ys) < 50: continue
            out.append({'text': text, 'crop': crop, 'color': color, 'highlight': highlight, 'backplate': backplate, 'height': scan['height'], 'width': scan['width'],
                        'ink': ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1], 'box': (xs.min(), ys.min(), xs.max(), ys.max())})
    return out


def _outline(sample):
    """(colour, width in px) of the outline around the fill, from one sample; None when the surroundings are too dark to tell."""
    import cv2, numpy as np
    crop = sample['crop']; ink = _ink(crop, sample['color']).astype(np.uint8)
    far = cv2.dilate(ink, np.ones((25, 25), np.uint8)).astype(bool) & ~cv2.dilate(ink, np.ones((17, 17), np.uint8)).astype(bool)
    if not far.any() or crop[far].max(axis=1).mean() < 95: return None
    # Shell by shell outwards from the fill: the first shell is the grey anti-aliased edge of the letters, the outline
    # is every shell that is mostly dark.
    width, prev, ring_px = 0, ink.astype(bool), []
    for w in range(1, 11):
        cur = cv2.dilate(ink, np.ones((2 * w + 1, 2 * w + 1), np.uint8)).astype(bool); shell = cur & ~prev; prev = cur
        if shell.any() and (crop[shell].max(axis=1) < 90).mean() >= 0.5: width += 1; ring_px.append(crop[shell])
        elif w > 2 and width: break
    if not width: return ([0, 0, 0], 0)
    dark = np.concatenate(ring_px); dark = dark[dark.max(axis=1) < 90]
    return ([int(v) for v in np.median(dark, axis=0)], width)


def names_in(texts):
    """Words written with a capital and never without one in these texts: names (Danny, Norway), not sentence starts."""
    caps = {w for t in texts for w in re.findall(r"\b[A-Z][a-z]{2,}\b", t)}
    lower = {w for t in texts for w in re.findall(r"\b[a-z]{3,}\b", t)}
    return sorted(w for w in caps if w.lower() not in lower)


def measure(run, out_dir=None, font_name=None):
    """style.json for this film. Raises SystemExit when the references show no captions (then there is nothing to imitate)."""
    import numpy as np
    run = Path(run); out_dir = Path(out_dir or run / 'captions'); out_dir.mkdir(parents=True, exist_ok=True)
    segs = {s['id']: s for s in json.loads((run / 'plan/segments.json').read_text())['segments']}
    clips = []
    for f in sorted((run / 'clips/caption_work').glob('G*.hq.ref.text.json')):
        gid = f.name[:3]; clip = run / 'clips' / f'{gid}.hq.ref.mp4'
        if gid not in segs or not clip.is_file(): continue
        scan = json.loads(f.read_text())
        clips.append((clip, scan, caption_events(scan, [x['text'] for x in segs[gid].get('speech', [])] or None), gid))
    events = [(e, scan, gid) for _, scan, evs, gid in clips for e in evs]
    if len(events) < 3: raise SystemExit('captions: the references of this film show no captions to imitate')
    rel_h = [l['bbox'][3] / scan['height'] for e, scan, _ in events for l in e['lines']]
    rel_w = sorted(l['bbox'][2] / scan['width'] for e, scan, _ in events for l in e['lines'])
    centre = lambda e, scan: (min(l['bbox'][1] for l in e['lines']) + max(l['bbox'][1] + l['bbox'][3] for l in e['lines'])) / 2 / scan['height']
    cx = [(l['bbox'][0] + l['bbox'][2] / 2) / scan['width'] for e, scan, _ in events for l in e['lines']]
    line_h = statistics.median(rel_h)
    # two text blocks far apart (a caption and a title card) are not two lines of one caption (the 11-19 film measured a
    # 0.16 pitch from them)
    two = [g for g in ((e['lines'][1]['bbox'][1] - e['lines'][0]['bbox'][1]) / scan['height'] for e, scan, _ in events if len(e['lines']) == 2)
           if 0 < g < 2.2 * line_h]
    per_seg = {}
    for e, scan, gid in events: per_seg.setdefault(gid, []).append(centre(e, scan))
    texts = sorted({l['text'] for e, _, _ in events for l in e['lines']})
    firsts = [e['lines'][0]['text'] for e, _, _ in events]
    samples = _samples([(c, s, ev) for c, s, ev, _ in clips])
    if not samples: raise SystemExit('captions: no clean single-line caption found to measure the letters from')
    fill_rgb, highlight_rgb = caption_colours(samples)
    fits = []
    faces = font_candidates()
    if font_name:                                    # the agent looked at the preview and names the face
        faces = [f for f in faces if f[0].lower() == font_name.lower()] or [f for f in faces if font_name.lower() in f[0].lower()]
        if not faces: raise SystemExit(f'captions: no installed face is called {font_name!r}')
    for name, file, index in faces:
        scores = [r for r in (_fit(s, name, file, index) for s in samples) if r]
        if scores: fits.append((statistics.mean(r[0] for r in scores), name, file, index, statistics.median(r[1] / s['height'] for r, s in zip(scores, samples))))
    if not fits: raise SystemExit('captions: none of the candidate fonts is installed')
    fits.sort(reverse=True); score, name, file, index, size = fits[0]
    outlines = [o for o in (_outline(s) for s in samples) if o]
    thin_px = statistics.median(_thinning(s, file, index, f[1]) for s in samples for f in [_fit(s, name, file, index)] if f)
    if abs(thin_px) < 0.0008 * samples[0]['height']: thin_px = 0.0      # under a pixel at 720p: compression blur, not a different weight
    # the dark band seen around the fill = the part of the outline over the letters' edge (thin_px) + the part outside
    ring_px = (statistics.median(o[1] for o in outlines) + 0.5) if outlines else 0.1 * size * samples[0]['height']
    stroke_px = max(0.0, ring_px - max(0.0, thin_px))
    style = {
        'type': 'lymphloop_caption_style', 'font_chosen_by': 'agent' if font_name else 'measure', 'font': name, 'font_file': file, 'font_index': index, 'font_fit': round(score, 3),
        'font_alternatives': [{'font': n, 'fit': round(s, 3)} for s, n, *_ in fits[1:4]],
        'size': round(size, 5),                                   # font size / frame height
        'line_pitch': round(statistics.median(two), 5) if two else round(1.15 * size, 5),
        'centre_y': round(statistics.median(centre(e, scan) for e, scan, _ in events), 4),
        'centre_y_by_segment': {g: round(statistics.median(v), 4) for g, v in per_seg.items()},
        'centre_x': round(statistics.median(cx), 4),
        'max_line_width': round(rel_w[int(0.95 * (len(rel_w) - 1))], 4),       # of the frame width
        'max_lines': 2 if len(two) >= 0.1 * len(events) else 1,     # single-line captions stay single-line
        'fill': fill_rgb,             # RGB
        # the colour of the word being spoken, when the film highlights it
        'highlight': highlight_rgb,
        'stroke': ([int(v) for v in np.median([o[0] for o in outlines], axis=0)][::-1] if outlines else [0, 0, 0]),
        'stroke_width': round(stroke_px / samples[0]['height'], 5),      # of the frame height
        'fill_thinning': round(thin_px / samples[0]['height'], 5),       # of the frame height: the fill is this much thinner than the plain glyphs
        'stroke_blur': 0.35,                                          # px at the output size: the film's outline is slightly soft
        'lowercase_sentence_start': sum(t[:1].islower() for t in firsts) > 0.6 * len(firsts),
        'uppercase': sum(t.isupper() for t in texts) > 0.6 * len(texts),
        'trailing_period': sum(t.rstrip().endswith('.') for t in texts) > 0.15 * len(texts),
        'commas': sum(',' in t for t in texts) > 0,
        'hyphens': sum('-' in t for t in texts) > 0,
        # words the film's captions write with a capital (names): kept when sentence starts are lower-cased
        'proper_nouns': names_in(texts),
        'measured_from': {'segments': sorted(per_seg), 'caption_events': len(events), 'letter_samples': len(samples), 'line_height': round(statistics.median(rel_h), 5)},
    }
    plates = [s for s in samples if s.get('backplate')]
    if len(plates) >= 0.5 * len(samples):
        style['highlight_background'] = {
            'color': style['highlight'],
            **{k: float(np.median([s['backplate'][k] / s['height'] for s in plates])) for k in ('pad_x', 'pad_y', 'radius')}}
    (out_dir / 'style.json').write_text(json.dumps(style, ensure_ascii=False, indent=2))
    _preview(style, samples, out_dir / 'style-preview.jpg')
    return style


def _preview(style, samples, out):
    """Original caption crops on the left, the same words drawn in the measured style over the same crop on the right."""
    import cv2, numpy as np
    rows = []
    for s in samples[:6]:
        crop = s['crop']; h, w = crop.shape[:2]
        st = {**style, 'centre_y': 0.5, 'centre_x': 0.5}
        base = cv2.blur(crop, (31, 31))                       # the same colours without the original letters
        scale = s['height']                                   # sizes are relative to the full frame height
        highlight = None
        if s.get('backplate'):
            bp = s['backplate']['box']; target = (bp[0] + bp[2] / 2 - s['box'][0]) / max(1, s['box'][2] - s['box'][0])
            fnt = font(style, style['size'] * scale); tokens = s['text'].split(); total = fnt.getlength(s['text'])
            centres = [(fnt.getlength(' '.join(tokens[:i])) + (fnt.getlength(' ') if i else 0) + fnt.getlength(t) / 2) / max(1, total) for i, t in enumerate(tokens)]
            j = min(range(len(tokens)), key=lambda i: abs(centres[i] - target)); highlight = (j, j + 1)
        drawn = draw(base, [s['text']], st, frame_height=scale, frame_width=s['width'], highlight_words=highlight)
        rows.append(np.hstack([crop, drawn]))
    width = max(r.shape[1] for r in rows)
    rows = [cv2.copyMakeBorder(r, 2, 2, 0, width - r.shape[1], cv2.BORDER_CONSTANT, value=(40, 40, 40)) for r in rows]
    cv2.imwrite(str(out), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 92])


# ---------------------------------------------------------------- text
QUOTE = re.compile(r'台词[^“\n]{0,24}[:：]\s*“([^”]+)”')


LINE_BRACES = re.compile(r'\{([^{}]+)\}')


def prompt_lines(prompt_text):
    """The spoken lines a prompt asks for, in order: {原词} in the five-section layout (2026-11-20), 称呼台词：“…” before.
    2026-11-20 film 2210: braces were not read, every rewritten segment fell back to the source transcript and drew
    the old words ("unas gotas") under the new speech ("unos sobres"). Comment lines (#) are skipped."""
    body = '\n'.join(l for l in prompt_text.splitlines() if not l.lstrip().startswith('#'))
    braces = [m.group(1).strip() for m in LINE_BRACES.finditer(body)]
    return braces or [m.group(1).strip() for m in QUOTE.finditer(body)]


def _norm(w): return re.sub(r'[^a-z0-9]', '', w.lower())


def spell(heard, lines):
    """What the caption shows, word by word: only what is actually said in the clip (user, 2026-11-13: "按照替换段的实际
    口播内容补字幕"). heard: [{w,a,b}] from the clip; lines: what the prompt asked for, used for spelling only.
    - a word heard as written takes the prompt's spelling and punctuation;
    - a stretch heard differently (LymphLoop -> "Limp Flute") takes the prompt's words that sound like it, over the
      stretch's time; a prompt word with nothing like it in the stretch was not said and is left out
      ("One packet" heard as "Once" -> "One");
    - a prompt word nobody said is left out, and listed in `unsaid` so the missing word is known;
    - a word heard next to the lines but not written in them stays as heard; a stray one (whisper inventing words over
      music) is dropped.
    -> ([{'w','a','b','end': a sentence ends after this word}], unsaid words)"""
    want, ends = [], set()
    for line in lines:
        toks = [t for t in line.split() if _norm(t)]
        for t in toks:
            if re.search(r'[.?!]["\']?$', t): ends.add(len(want))
            want.append(t)
        if toks: ends.add(len(want) - 1)
    # whisper splits "70%" into "70" + "%" (2026-11-22 film 1144433884582573 G04: the caption read "70 off right now"):
    # a symbol-only token goes back onto the word before it instead of being dropped with the punctuation
    glued = []
    for h in heard:
        if not _norm(h['w']) and h['w'].strip() in ('%', '$', '€', '£', '+') and glued:
            sym = h['w'].strip()
            glued[-1] = {**glued[-1], 'w': (sym + glued[-1]['w']) if sym in '$€£' else glued[-1]['w'] + sym, 'b': h['b']}
        else: glued.append(h)
    heard = [h for h in glued if _norm(h['w'])]
    if not want: return [{'w': h['w'], 'a': h['a'], 'b': h['b'], 'end': bool(re.search(r'[.?!]$', h['w']))} for h in heard], []
    ops = difflib.SequenceMatcher(None, [_norm(h['w']) for h in heard], [_norm(t) for t in want], autojunk=False).get_opcodes()
    out, unsaid = [], []
    def close(j):                                 # the sentence end moves back to the last word that was said
        if j in ends and out: out[-1]['end'] = True
    for op, i1, i2, j1, j2 in ops:
        if op == 'equal':
            # the prompt's spelling, unless only the heard word carries the sign (prompt "70", heard "70%")
            out += [{'w': heard[i1 + k]['w'] if re.search(r'[%$€£]', heard[i1 + k]['w']) and not re.search(r'[%$€£]', want[j1 + k]) else want[j1 + k],
                     'a': heard[i1 + k]['a'], 'b': heard[i1 + k]['b'], 'end': (j1 + k) in ends} for k in range(i2 - i1)]
        elif op == 'replace':
            got = [_norm(h['w']) for h in heard[i1:i2]]; got.append(''.join(got))
            alike = lambda t: max(difflib.SequenceMatcher(None, _norm(t), g).ratio() for g in got) >= 0.5
            keep = [j for j in range(j1, j2) if j2 - j1 <= i2 - i1 or alike(want[j])]
            t0, t1 = heard[i1]['a'], heard[i2 - 1]['b']; n = max(1, len(keep))
            for k, j in enumerate(keep):
                out.append({'w': want[j], 'a': round(t0 + (t1 - t0) * k / n, 2), 'b': round(t0 + (t1 - t0) * (k + 1) / n, 2), 'end': j in ends})
            for j in range(j1, j2):
                if j not in keep: unsaid.append(want[j]); close(j)
            if not keep:
                out += [{'w': h['w'], 'a': h['a'], 'b': h['b'], 'end': False} for h in heard[i1:i2]]
        elif op == 'insert':
            for j in range(j1, j2): unsaid.append(want[j]); close(j)
        elif op == 'delete':
            near = lambda h: any(abs(h['a'] - o['b']) <= 0.6 for o in out[-1:]) or (i2 < len(heard) and heard[i2]['a'] - h['b'] <= 0.6)
            out += [{'w': h['w'], 'a': h['a'], 'b': h['b'], 'end': False} for h in heard[i1:i2] if near(h)]
    return out, unsaid


def dress(word, style, sentence_start=False):
    """One word the way the film writes it."""
    w = re.sub(r'[;:"“”]', '', word.strip())
    w = re.sub(r'[.!…]+$', '.' if style.get('trailing_period') else '', w)
    if not style.get('commas', True): w = w.replace(',', '')
    if not style.get('hyphens', True): w = w.replace('-', ' ').strip()
    if style.get('uppercase'): return w.upper()
    core = re.sub(r"[^A-Za-z']", '', w)
    plain = re.fullmatch(r"[A-Z][a-z']*", core) and not re.match(r"I($|')", core) and core.split("'")[0] not in set(style.get('proper_nouns') or [])
    if plain and (style.get('lowercase_sentence_start') or not sentence_start): w = w.replace(core, core[0].lower() + core[1:], 1)
    return w


def group(words, style, width_of):
    """Captions from timed words. A sentence (or what a pause sets apart) is one caption when it fits on the film's
    number of lines; a longer one is cut at the comma nearest its middle, else at the word nearest its middle.
    width_of(text) -> width as a share of the frame width. -> [{'a','b','lines': [str, ...]}]"""
    limit = style['max_line_width']; most = style.get('max_lines', 2)
    def lines_of(ws):
        lines = [[]]
        for w in ws:
            if lines[-1] and width_of(' '.join(lines[-1] + [w])) > limit: lines.append([])
            lines[-1].append(w)
        return lines
    def fit(ws):
        if len(ws) < 2 or len(lines_of([w['text'] for w in ws])) <= most: return [ws]
        mid = (len(ws) - 1) / 2
        commas = [i for i, w in enumerate(ws[:-1]) if w['text'].endswith(',') and 1 <= i < len(ws) - 2]
        cut = min(commas, key=lambda i: abs(i - mid)) if commas else int(mid)
        return fit(ws[:cut + 1]) + fit(ws[cut + 1:])
    sentences, cur, start = [], [], True
    for w in words:
        text = dress(w['w'], style, start)
        if not text: continue
        if cur and w['a'] - cur[-1]['b'] > PAUSE: sentences.append(cur); cur = []
        cur.append({'text': text, 'a': w['a'], 'b': w['b']}); start = bool(w.get('end'))
        if w.get('end'): sentences.append(cur); cur = []
    if cur: sentences.append(cur)
    chunks = [c for sent in sentences for c in fit(sent)]
    out = []
    for i, c in enumerate(chunks):
        a = max(0.0, c[0]['a'] - LEAD); b = c[-1]['b'] + HOLD
        if i + 1 < len(chunks): b = min(b, max(c[-1]['b'], chunks[i + 1][0]['a'] - LEAD))
        if out: a = max(a, out[-1]['b'])
        c[-1]['text'] = c[-1]['text'].rstrip(',')
        if b - a < 0.05: continue
        out.append({'a': round(a, 3), 'b': round(b, 3), 'lines': [' '.join(l) for l in lines_of([x['text'] for x in c])],
                    'words': [dict(x) for x in c]})
    return out


# ---------------------------------------------------------------- draw
def active_highlight(caption, t):
    words = caption.get('words') or []
    if not words: return None
    j = max([i for i, word in enumerate(words) if word['a'] <= t] or [0])
    start = sum(len(word['text'].split()) for word in words[:j])
    return start, start + len(words[j]['text'].split())


def draw(frame, lines, style, frame_height=None, frame_width=None, centre_y=None, highlight_words=None):
    """`lines` on a BGR frame in the measured style. frame_height / frame_width: the full frame the relative sizes refer
    to when `frame` is only a crop."""
    import cv2, numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    h, w = frame.shape[:2]; H = frame_height or h
    fnt = font(style, style['size'] * H * SS); sw = int(round(style['stroke_width'] * H * SS))
    layer = Image.new('RGBA', (w * SS, h * SS), (0, 0, 0, 0)); edge = Image.new('RGBA', (w * SS, h * SS), (0, 0, 0, 0))
    back = Image.new('RGBA', layer.size, (0, 0, 0, 0)); db = ImageDraw.Draw(back); word_index = 0
    d, de = ImageDraw.Draw(layer), ImageDraw.Draw(edge)
    pitch = style['line_pitch'] * H * SS; cy = (centre_y if centre_y is not None else style['centre_y']) * h * SS
    asc, desc = fnt.getmetrics(); x_h = fnt.getbbox('x')[3] - fnt.getbbox('x')[1]
    for i, text in enumerate(lines):
        mid = cy + (i - (len(lines) - 1) / 2) * pitch              # the middle of this line's x-height band
        y = mid - (asc - x_h / 2)                                  # PIL draws from the ascender line
        x = style.get('centre_x', 0.5) * w * SS - d.textlength(text, font=fnt) / 2
        plate = style.get('highlight_background')
        if plate and highlight_words:
            parts = text.split()
            for j, word in enumerate(parts):
                if highlight_words[0] <= word_index + j < highlight_words[1]:
                    prefix = ' '.join(parts[:j]) + (' ' if j else '')
                    left = x + d.textlength(prefix, font=fnt); l, top, r, bottom = fnt.getbbox(word)
                    px, py = plate['pad_x'] * H * SS, plate['pad_y'] * H * SS
                    db.rounded_rectangle((left+l-px, y+top-py, left+r+px, y+bottom+py),
                        radius=plate['radius']*H*SS, fill=tuple(plate['color'])+(255,))
        word_index += len(text.split())
        de.text((x, y), text, font=fnt, fill=tuple(style['stroke']) + (255,), stroke_width=sw, stroke_fill=tuple(style['stroke']) + (255,))
        d.text((x, y), text, font=fnt, fill=tuple(style['fill']) + (255,))
    thin = int(round(style.get('fill_thinning', 0) * H * SS))
    if thin: layer.putalpha(layer.getchannel('A').filter((ImageFilter.MinFilter if thin > 0 else ImageFilter.MaxFilter)(2 * abs(thin) + 1)))
    if style.get('stroke_blur'): edge = edge.filter(ImageFilter.GaussianBlur(style['stroke_blur'] * SS))
    over = Image.alpha_composite(Image.alpha_composite(back, edge), layer).resize((w, h), Image.LANCZOS)
    rgba = np.array(over).astype(np.float32); alpha = rgba[:, :, 3:4] / 255.0
    out = frame.astype(np.float32) * (1 - alpha) + rgba[:, :, 2::-1] * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def width_fn(style, frame_width, frame_height):
    fnt = font(style, style['size'] * frame_height * SS)
    return lambda text: fnt.getlength(text) / SS / frame_width


def burn(clip, out, captions, style, centre_y=None):
    """Draw the captions on every frame of `clip` they cover; one encode (libx264 crf 12), sound copied."""
    import cv2
    info = media.probe(str(clip)); v = info['video']; w, h = int(v['width']), int(v['height']); fps = str(info['fps'])
    from fractions import Fraction
    rate = float(Fraction(fps))
    cap = cv2.VideoCapture(str(clip))
    cmd = [media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{w}x{h}', '-r', fps, '-i', '-',
           '-i', str(clip), '-map', '0:v:0', '-map', '1:a?', '-c:v', 'libx264', '-crf', '12', '-preset', 'medium', '-pix_fmt', 'yuv420p',
           '-c:a', 'copy', '-movflags', '+faststart', str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE); n = 0; drawn = 0
    while True:
        ok, frame = cap.read()
        if not ok: break
        t = n / rate
        c = next((c for c in captions if c['a'] <= t < c['b']), None)
        if c: frame = draw(frame, c['lines'], style, centre_y=centre_y, highlight_words=active_highlight(c, t)); drawn += 1
        proc.stdin.write(frame.tobytes()); n += 1
    cap.release(); proc.stdin.close()
    if proc.wait() != 0: raise SystemExit(f'captions: encode failed for {clip}')
    return {'frames': n, 'frames_captioned': drawn}


def tighten(heard, audio, rate=16000, offset=0.0):
    """Whisper lets a word start in the silence before it and end in the silence after it (a 1.0 s line came back as
    2.3 s): pull each word in to where the sound is clearly above the quiet parts of the clip (20 ms steps)."""
    import numpy as np
    step = rate // 50
    lv = np.array([20 * np.log10(max(1e-6, float(np.sqrt(np.mean(audio[i:i + step] ** 2))))) for i in range(0, max(0, len(audio) - step), step)])
    if not len(lv): return heard
    lv = np.array([np.median(lv[max(0, i - 1):i + 2]) for i in range(len(lv))])            # a lone click is not speech
    loud = lv > max(float(np.percentile(lv, 10)) + 10, float(np.percentile(lv, 95)) - 14)      # speech stands well above room tone and music
    out = []
    for h in heard:
        i0, i1 = max(0, int((h['a'] - offset) * 50)), min(len(loud), int((h['b'] - offset) * 50) + 1)
        idx = np.where(loud[i0:i1])[0]
        if len(idx): out.append({**h, 'a': round(offset + (i0 + idx[0]) / 50, 2), 'b': round(offset + (i0 + idx[-1] + 1) / 50, 2)})
        else: out.append(h)
    return out


def captions_for(clip, lines, style, clip_in=0.0, clip_out=None, heard=None):
    """Captions for the selected part of a clip, in the clip's own seconds."""
    import words as words_mod
    info = media.probe(str(clip)); v = info['video']
    if heard is None and words_mod.RUN is not None:
        overrides = Path(words_mod.RUN)/'captions/timing_overrides.json'
        if overrides.is_file():
            import google_api as api
            reviewed = (json.loads(overrides.read_text()).get('clips') or {}).get(api.file_sha(clip))
            if reviewed and reviewed.get('reviewed'):
                heard = reviewed['words']
                end = float(info['video_duration'])
                if not reviewed.get('evidence') or any(not 0 <= w['a'] <= w['b'] <= end + 0.01 for w in heard):
                    raise SystemExit('captions: invalid reviewed timing override')
                heard = [dict(w) for w in heard if w['b'] > clip_in and (clip_out is None or w['a'] < clip_out)]
    if heard is None:
        heard = words_mod.transcribe(words_mod.load_model(), clip, clip_in, (clip_out - clip_in) if clip_out else None)
        heard = [{**h, 'a': round(h['a'] + clip_in, 2), 'b': round(h['b'] + clip_in, 2)} for h in heard]
        heard = tighten(heard, words_mod.pcm(clip, clip_in, (clip_out - clip_in) if clip_out else None), offset=clip_in)
    spoken, unsaid = spell(heard, lines)
    return group(spoken, style, width_fn(style, int(v['width']), int(v['height']))), heard, unsaid


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__); s = p.add_subparsers(dest='cmd', required=True)
    q = s.add_parser('measure'); q.add_argument('run')
    a = p.parse_args(argv)
    if a.cmd == 'measure':
        st = measure(a.run); print(json.dumps({k: v for k, v in st.items() if k != 'centre_y_by_segment'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
