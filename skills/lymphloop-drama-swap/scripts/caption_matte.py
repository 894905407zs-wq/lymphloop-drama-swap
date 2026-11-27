#!/usr/bin/env python3
"""Captions and title cards put back on a generated segment by copying the film's own caption pixels.

User 2026-11-19: "字幕是不固定的，每一条字幕可能都不一样" -- every film, and often every line, has its own font,
colours, word highlight, outline and place; imitating one measured style never matched (11-19 test: a white caption
with the spoken word in yellow came back pink, in Impact, and the title cards were lost).

The uncleaned reference (`clips/G##.hq.ref.mp4`) and the cleaned one (`clips/G##.hq.clean.ref.mp4`) differ exactly
where LaMa took the captions and title cards off: inside the OCR text boxes that difference is the caption itself, in
the film's own look. It is laid over the generated picture at the moment the same words are said there (the generated
speech can come early or late). Lines that carried a replaced word (the old brand, "gotero" ...) cannot be copied;
they are drawn new in the measured style (caption_burn), only inside their own time window."""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import difflib, json, re, subprocess, sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media  # noqa: E402

PAD = 8            # px around an OCR box at the reference's size: outline, shadow and the highlight block
ALPHA_LO, ALPHA_HI = 18, 60     # colour difference (0-255) between the two references: below = background, above = caption
MAX_SHIFT_S = 1.6  # the generated speech is never further off than this from the source
SMOOTH_S = 0.25    # one line's shift stays this close to the segment's typical shift (OCR noise moves single matches)


def norm_tokens(text):
    return [t for t in re.findall(r"[a-z0-9áéíóúüñ']+", text.lower())]


def fold(t):
    """'mí' and 'mi' are the same word to a caption (OCR and the transcript disagree on accents)."""
    return t.translate(str.maketrans('áéíóúü', 'aeiouu'))


def same_word(a, b):
    """One word as OCR reads it from frame to frame: equal, or 5+ letters spelled nearly alike (CLEAVERS /
    CLEAVERSR, 2026-11-22 film 1801069464466244 G02: the second half of the line was read CLEAVERSR and dropped)."""
    if a == b: return True
    base = lambda w: re.sub(r"(?:'s|s)$", '', w.replace('’', "'"))       # link / link's / links
    if len(a) >= 3 and len(b) >= 3 and base(a) == base(b): return True
    return min(len(a), len(b)) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def says(text, said_long, said_all):
    """The caption line repeats what is said. Lines of only short words ("y si a", "mi no me": 11-19 G16) have no
    3-letter token to match, so they match when every word is in the segment's speech."""
    toks = [fold(t) for t in norm_tokens(text)]
    long = {t for t in toks if len(t) >= 3}
    if long: return bool(long & said_long) or any(same_word(t, w) for t in long for w in said_long)
    return len(toks) >= 2 and set(toks) <= said_all


def uncovered_caps(caps, rendered, line_events, shown_at, slack=0.4):
    """Heard captions no copied caption line shows: drawn in the measured style. The copy only brings back what the
    reference's OCR found and kept; a line it lost (2026-11-22 film 1801069464466244 G02: CLEAVERS) would otherwise
    be missing while it is said. A heard word (3+ letters) counts as shown when a copied line around its time has it
    (OCR spellings like CLEAVERSR count), and a copied line must stay on screen until near the end of the heard line
    (a line the OCR stopped reading part way ends early). Heard starts are not used: Whisper glues a word after a
    pause to the word before (2026-11-22 film 2114607189446748 G04: "then" 5.66-7.72 s, said at about 7.4 s)."""
    out = []
    for c in caps:
        if any(c is r for r in rendered): continue
        near = [(e, w) for e, w in zip(line_events, shown_at) if w and min(c['b'] + slack, w[1]) > max(c['a'] - slack, w[0])]
        keys = {fold(t) for e, _ in near for t in e['key']}
        keys |= {NUMBER_WORDS[k] for k in keys if k in NUMBER_WORDS}
        want = [fold(t) for t in norm_tokens(' '.join(c['lines'])) if len(t) >= 3]
        if not want: continue                    # "BE IN": nothing to judge by; drawn it doubled the copied words around it
        seen = lambda t: t in keys or any(same_word(t, k) or (len(k) >= 4 and difflib.SequenceMatcher(None, t, k).ratio() >= 0.8) for k in keys)
        mine = [w for e, w in near if any(same_word(NUMBER_WORDS.get(fold(k), fold(k)), t) for k in e['key'] for t in want)]
        last = max((w[1] for w in mine), default=-1.0)
        if any(not seen(t) for t in want) or last < c['b'] - 0.3: out.append(c)
    return out


NUMBER_WORDS = {'0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four', '5': 'five', '6': 'six', '7': 'seven', '8': 'eight',
                '9': 'nine', '10': 'ten', '11': 'eleven', '12': 'twelve', '15': 'fifteen', '20': 'twenty', '30': 'thirty'}


def heard_near(event, ref_fps, shift, heard, slack=1.0):
    """Most words of this caption line are heard in the clip around the time it would be shown. A line the clip does
    not say is not copied: 2026-11-22 film 1585905389415123 G10, a muted repeated "Then you" kept its caption; G06, a
    sip with no voice would have shown the source's "I can't believe it"; film 2896957363997251 G06 opened on the
    source's "TASTE IT IT'S" (the last words of the line before). A line left out here that is said after all is drawn
    from what is heard (uncovered_caps)."""
    t0, t1 = event['k0'] / ref_fps + shift - slack, (event['k1'] + 1) / ref_fps + shift + slack
    words = [fold(t) for h in heard if float(h['b']) > t0 and float(h['a']) < t1 for t in norm_tokens(h['w'])]
    keys = [NUMBER_WORDS.get(t, t) for t in (fold(k) for k in event['key'])]
    keys = [k for k in keys if len(k) >= 3] or keys
    heard_k = [k for k in keys if any(same_word(k, w) for w in words)]
    # a tie stays when the line's longest word is heard (Whisper drops "and" of "AND HEADS UP"), not when it is the
    # line's main word that is missing ("TASTE" of "TASTE IT IT'S")
    return len(heard_k) > 0.5 * len(keys) or (len(heard_k) == 0.5 * len(keys) and max(keys, key=len) in heard_k)


def merged_into(gaps, line_events, shown_at):
    """Indexes of copied lines a drawn heard line takes over: their words are in it and they show at its time (the
    copied WEEKS of "GIVE ME 6 / WEEKS" would flash again after the drawn GIVE ME SIX WEEKS)."""
    out = set()
    for c in gaps:
        words = [fold(t) for t in norm_tokens(' '.join(c['lines']))]
        for i, (e, w) in enumerate(zip(line_events, shown_at)):
            longs = [NUMBER_WORDS.get(fold(t), fold(t)) for t in e['key'] if len(t) >= 3 or fold(t) in NUMBER_WORDS]
            if w and longs and min(c['b'], w[1]) > max(c['a'], w[0]) and all(any(same_word(t, x) for x in words) for t in longs):
                out.add(i)
    return out


def show_windows(events, shifts, ref_fps, n_lines):
    """[(start, end)] output seconds each event is shown. Caption lines take turns in one band: a line moved earlier
    cuts the previous line short instead of being laid over it (11-19 G16: "te devuelven el" moved +0.09 s, "dinero"
    moved -0.20 s, both showed for 0.3 s as "te didinero:n el"). Title cards (after the first n_lines) keep their own."""
    win = [None if s is None else [e['k0'] / ref_fps + s, (e['k1'] + 1) / ref_fps + s] for e, s in zip(events, shifts)]
    lines = sorted((i for i in range(n_lines) if win[i]), key=lambda i: win[i][0])
    for a, b in zip(lines, lines[1:]):
        if win[a][1] > win[b][0]: win[a][1] = max(win[a][0], win[b][0])
    return win


from caption_lama import TITLE_CARD as TIME_CARD  # noqa: E402  (DAY 1, WEEK TWO, ONE WEEK LATER)


def is_title(box, width, height):
    """A title card (WEEK 1, SEMANA 5, a closing slogan): horizontal, not tiny print. Anywhere across the frame:
    2026-11-22 film 1585905389415123 puts WEEK 1-6 top left (centre at 0.16 of the width, 0.03 of the height) and
    they were never restored. Product print is not copied anyway: only text the reference cleaning removed has a
    matte (mattes() needs the cleaned reference to differ), and a card has to stay 12 frames."""
    x, y, w, h = box
    cx = (x + w / 2) / width
    return w >= 1.2 * h and 0.05 <= cx <= 0.95 and h >= 0.025 * height


def readable(text, banned, said=frozenset()):
    """Text a caption or a title card can be: Latin letters and digits, not the product's own print (the bottle's
    "lymphatic drainage", OCR gibberish of a carton's microtext) -- copying those would put the competitor's label back.
    `said`: words this segment says; a caption word that is said is never taken for a misread of product print."""
    if re.search(r'[\u0400-\u04ff]', text): return False                  # Cyrillic look-alikes: OCR of tiny print
    toks = norm_tokens(text)
    if not toks or set(toks) & banned: return False
    # OCR misreads of product print ("drair" / "drai" for the bottle's "drainage", 2026-11-20 film 1753 G02: copied
    # as a title card for half a second): a word that starts like a banned word, or is spelled close to one. Not a
    # word that is said: 2026-11-22 film 1801069464466244 G02, the caption CLEAVERS (said, our own ingredient) is
    # spelled like the carton's "cleanse" and was dropped, so the line went missing
    long_banned = [b for b in banned if len(b) >= 5]
    said_long = [w for w in said if len(w) >= 5]
    for t in toks:          # OCR spells the said word its own way on some frames (CLEAVERSR): still the said word
        if fold(t) in said or any(difflib.SequenceMatcher(None, fold(t), w).ratio() >= 0.85 for w in said_long): continue
        if len(t) >= 4 and any(b.startswith(t[:4]) or difflib.SequenceMatcher(None, t, b).ratio() >= 0.75 for b in long_banned):
            return False
    letters = sum(c.isalpha() for c in text); return letters >= 2 and letters >= 0.6 * len(re.sub(r'\s', '', text))


def similar(a, b):
    """Two OCR readings of one caption line. Also when the spaces differ ("IF THE LINK" / "IFTHE LINK", "STILL IN
    STOCK" / "STILDIN STOCK": 2026-11-22 film 2896957363997251 G06, the line split into a dozen short events and
    flickered)."""
    ja, jb = ''.join(a), ''.join(b)
    if ja and jb and (ja == jb or (min(len(ja), len(jb)) >= 6 and difflib.SequenceMatcher(None, ja, jb).ratio() >= 0.85)):
        return True
    a, b = set(a), set(b)
    if not (a and b): return False
    common = sum(1 for x in a if any(same_word(x, y) for y in b))
    return common / (len(a) + len(b) - common) >= 0.5


def text_events(scan, accept):
    """[{'k0','k1','texts','boxes': {frame: [bbox]}}]: consecutive frames showing the same lines merged (OCR reads a
    line a little differently from frame to frame: "cara por mí" / "cara por mi" are one caption)."""
    per = {}
    for d in scan['detections']:
        if not accept(d): continue
        per.setdefault(d['frame'], []).append(d)
    events = []
    for f in sorted(per):
        lines = sorted(per[f], key=lambda d: (d['bbox'][1], d['bbox'][0])); key = tuple(norm_tokens(' '.join(d['text'] for d in lines)))
        if events and f - events[-1]['k1'] <= 3 and (events[-1]['key'] == key or similar(events[-1]['key'], key)):
            events[-1]['k1'] = f
            if len(key) > len(events[-1]['key']): events[-1]['key'] = key; events[-1]['texts'] = [d['text'] for d in lines]
        else:
            events.append({'key': key, 'k0': f, 'k1': f, 'texts': [d['text'] for d in lines], 'boxes': {}})
        events[-1]['boxes'][f] = [d['bbox'] for d in lines]
    for e in events:            # frames OCR missed inside an event keep the nearest frame's boxes
        known = sorted(e['boxes'])
        for f in range(e['k0'], e['k1'] + 1):
            if f not in e['boxes']: e['boxes'][f] = e['boxes'][min(known, key=lambda k: abs(k - f))]
    return events


OWN_WORDS = ('lymphloop', 'yesnap')


def close_to(t, words, need=0.75):
    """A caption word spelled close to one of `words` (5+ letters) and not closer to our own brand."""
    if len(t) < 5: return False
    r = lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()
    best = max((r(t, w) for w in words if len(w) >= 5), default=0)
    return best >= need and best > max(r(t, o) for o in OWN_WORDS)


def changed_event(event, old_words):
    """The caption has a replaced word, exact or spelled its own way: the film's captions spell a brand differently
    from what is said (2026-11-22 film 2896957363997251 G06: "CALLED LYMFORIA" for the spoken "Lymphoria", copied
    back onto the generated clip as it was)."""
    keys = set(event['key'])
    return bool(keys & old_words) or any(close_to(t, old_words) for t in keys)


def event_shift(event, ref_fps, heard, spoken_tokens):
    """Seconds to move this caption so it appears when its words are said in the generated clip; None when its words
    are not heard there (a title card, or speech the generator dropped)."""
    spoken = {fold(t) for t in spoken_tokens}
    toks = [fold(t) for t in event['key'] if fold(t) in spoken]
    if not toks: return None
    t0 = event['k0'] / ref_fps
    seq = [[fold(w) for w in norm_tokens(h['w'])] for h in heard]
    best = None
    for i, ws in enumerate(seq):
        if not ws or ws[0] != toks[0]: continue
        follow = toks[1:3]; got = [w for x in seq[i + 1:i + 1 + len(follow)] for w in x]
        if follow and got[:len(follow)] != follow and len(toks) > 1: continue
        s = heard[i]['a'] - t0
        if abs(s) <= MAX_SHIFT_S and (best is None or abs(s) < abs(best)): best = s
    return best


def _decode(path):
    import cv2
    cap = cv2.VideoCapture(str(path))
    while True:
        ok, frame = cap.read()
        if not ok: break
        yield frame
    cap.release()


GLYPH_TOL = 35      # max channel distance (70 let a cream label background pass as white) from the caption's fill / highlight colour that counts as caption glyph


def glyph_gate(crop, colours, outline_px):
    """1 where a pixel of the ORIGINAL crop is caption ink: near the measured fill or highlight colour, grown by the
    outline width. Product print LaMa also smeared inside a caption box is left out: 2026-11-20 film 1753 G02, a
    bottle's dark-brown "lymphatic drainage" on cream sat inside the caption line's box and was copied onto the new
    picture as a floating "drain" for half a second."""
    import cv2, numpy as np
    m = np.zeros(crop.shape[:2], np.uint8)
    for rgb in colours:
        bgr = np.array(rgb[::-1], np.int16)
        m |= (np.abs(crop.astype(np.int16) - bgr).max(axis=2) <= GLYPH_TOL).astype(np.uint8)
    k = max(1, int(outline_px)) * 2 + 1
    return cv2.dilate(m, np.ones((k, k), np.uint8)).astype(np.float32)


def mattes(orig, clean, events, colours=None, outline_px=3):
    """{(event index, ref frame): [(x, y, crop BGR, alpha)]} at the reference's size. With `colours` (the caption's
    fill and highlight, RGB) only caption-coloured pixels and their outline are copied."""
    import cv2, numpy as np
    want = {}
    for i, e in enumerate(events):
        for f, boxes in e['boxes'].items(): want.setdefault(f, []).append((i, boxes))
    out = {}
    for f, (a, b) in enumerate(zip(_decode(orig), _decode(clean))):
        if f not in want: continue
        if b.shape != a.shape: b = cv2.resize(b, (a.shape[1], a.shape[0]))
        H, W = a.shape[:2]
        for i, boxes in want[f]:
            pieces = []
            for x, y, w, h in boxes:
                x0, y0 = max(0, int(x) - PAD), max(0, int(y) - PAD); x1, y1 = min(W, int(x + w) + PAD), min(H, int(y + h) + PAD)
                if x1 <= x0 or y1 <= y0: continue
                d = np.abs(a[y0:y1, x0:x1].astype(np.int16) - b[y0:y1, x0:x1].astype(np.int16)).max(axis=2).astype(np.float32)
                alpha = np.clip((d - ALPHA_LO) / (ALPHA_HI - ALPHA_LO), 0, 1)
                alpha = cv2.GaussianBlur(cv2.dilate(alpha, np.ones((2, 2), np.uint8)), (3, 3), 0)
                if alpha.max() < 0.5: continue                    # LaMa did not touch it: not a caption
                if colours: alpha = alpha * glyph_gate(a[y0:y1, x0:x1], colours, outline_px)
                if alpha.max() < 0.5: continue
                pieces.append((x0, y0, a[y0:y1, x0:x1].copy(), alpha))
            if pieces: out[(i, f)] = pieces
    return out


def composite(base, out, events, shifts, ref_fps, ref_size, cut, rendered=None, style=None, centre_y=None, windows=None):
    """Lay every copied caption over `base` at its shifted time; then draw `rendered` captions (lines with a replaced
    word) on top in their windows. One encode (libx264 crf 12), sound copied."""
    import cv2, numpy as np
    import caption_burn as cb
    info = media.probe(str(base)); v = info['video']; W, H = int(v['width']), int(v['height']); fps = str(info['fps'])
    rate = float(Fraction(fps)); kx, ky = W / ref_size[0], H / ref_size[1]
    cmd = [media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', fps, '-i', '-',
           '-i', str(base), '-map', '0:v:0', '-map', '1:a?', '-c:v', 'libx264', '-crf', '12', '-preset', 'medium', '-pix_fmt', 'yuv420p',
           '-c:a', 'copy', '-movflags', '+faststart', str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE); n = copied = drawn = 0
    for frame in _decode(base):
        t = n / rate
        r = next((c for c in (rendered or []) if c['a'] <= t < c['b']), None)
        if r is not None:
            frame = cb.draw(frame, r['lines'], style, centre_y=centre_y, highlight_words=cb.active_highlight(r, t)); drawn += 1
        else:
            for i, e in enumerate(events):
                s = shifts[i]
                if s is None: continue
                if windows and windows[i] and not windows[i][0] <= t < windows[i][1]: continue
                k = int(round((t - s) * ref_fps))
                if not e['k0'] <= k <= e['k1']: continue
                for x0, y0, crop, alpha in cut.get((i, k), []):
                    X0, Y0 = int(round(x0 * kx)), int(round(y0 * ky))
                    cw, ch = max(1, int(round(crop.shape[1] * kx))), max(1, int(round(crop.shape[0] * ky)))
                    if X0 >= W or Y0 >= H: continue
                    cw, ch = min(cw, W - X0), min(ch, H - Y0)
                    c = cv2.resize(crop, (cw, ch), interpolation=cv2.INTER_AREA).astype(np.float32)
                    al = cv2.resize(alpha, (cw, ch), interpolation=cv2.INTER_AREA)[..., None]
                    roi = frame[Y0:Y0 + ch, X0:X0 + cw].astype(np.float32)
                    frame[Y0:Y0 + ch, X0:X0 + cw] = (roi * (1 - al) + c * al).astype(np.uint8)
                copied += 1
        proc.stdin.write(frame.tobytes()); n += 1
    proc.stdin.close()
    if proc.wait() != 0: raise SystemExit(f'captions: encode failed for {base}')
    return {'frames': n, 'frames_copied': copied, 'frames_drawn': drawn}


def apply(run, seg, sel_entry, base, out, style, spoken_lines, old_words, drop_text, render, frames_match=True, banned=frozenset()):
    """Copy the film's captions onto `base` (the selected generated clip). Returns a report, or None when this segment
    cannot be matted (no uncleaned/cleaned reference pair, or a local splice whose frames are not the reference's)."""
    run = Path(run); gid = seg['id']
    orig, clean = run/'clips'/f'{gid}.hq.ref.mp4', run/'clips'/f'{gid}.hq.clean.ref.mp4'
    scan_path = run/'clips'/'caption_work'/f'{gid}.hq.ref.text.json'
    if not frames_match or not (orig.is_file() and clean.is_file() and scan_path.is_file()): return None
    scan = json.loads(scan_path.read_text()); RW, RH = scan['width'], scan['height']
    import caption_clean as cc
    title_banned = set(banned) | {t for w in cc.PRODUCT_WORDS for t in norm_tokens(w)}

    said_all = {fold(t) for x in seg.get('speech', []) for t in norm_tokens(x['text'])}
    said = {t for t in said_all if len(t) >= 3}
    cy = lambda b: (b[1] + b[3] / 2) / RH
    spoken_boxes = [d for d in scan['detections'] if not drop_text(d['text']) and cc.is_caption(d['bbox'], RW, RH)
                    and says(d['text'], said, said_all)]
    band = sorted(cy(d['bbox']) for d in spoken_boxes)[len(spoken_boxes) // 2] if spoken_boxes else None

    def is_line(d):
        """A caption line: in the film's caption band and repeating what is said. A bottle's "lymphatic drainage" is as
        big and as centred as a caption (11-19 G08: it was copied onto our packet); a place card on the table repeats
        the spoken words but sits outside the band."""
        if drop_text(d['text']) or band is None or abs(cy(d['bbox']) - band) > 0.07: return False
        if not (cc.is_caption(d['bbox'], RW, RH) and says(d['text'], said, said_all)): return False
        return readable(d['text'], frozenset(title_banned - said_all), said=frozenset(said_all))

    def is_card(d):
        """A title card: big, centred, not product print, outside the caption band."""
        if drop_text(d['text']) or (band is not None and abs(cy(d['bbox']) - band) <= 0.07): return False
        # a time card ("DAY 1", 29 px in a 1280 frame, 2026-11-22 film 998 G05) is a title at any height
        return (is_title(d['bbox'], RW, RH) or bool(TIME_CARD.fullmatch(d['text'].strip()))) and readable(d['text'], title_banned)
    # captions and title cards are grouped apart; a title card stays put for a while, print on a product passing by does not
    line_events = text_events(scan, is_line)
    events = line_events + [e for e in text_events(scan, is_card) if e['k1'] - e['k0'] >= 12]
    if not events: return None
    ref_fps = float(Fraction(str(media.probe(str(orig))['fps'])))
    spoken_tokens = set(norm_tokens(' '.join(spoken_lines)))
    heard = render['heard']
    raw = [None if changed_event(e, old_words) else event_shift(e, ref_fps, heard, spoken_tokens) for e in events]
    found = [s for s in raw if s is not None]
    typical = sorted(found)[len(found) // 2] if found else 0.0
    raw = [None if s is None else min(typical + SMOOTH_S, max(typical - SMOOTH_S, s)) for s in raw]
    shifts, kinds = [], []
    for e, s in zip(events, raw):
        if changed_event(e, old_words): shifts.append(None); kinds.append('drawn new (replaced word)')
        elif s is not None and sel_entry.get('sound') != 'source' and not heard_near(e, ref_fps, s, heard):
            shifts.append(None); kinds.append('left out (not said in this clip)')
        elif s is not None: shifts.append(s); kinds.append(f'copied, moved {s:+.2f} s')
        elif not (set(e['key']) & spoken_tokens): shifts.append(0.0); kinds.append('copied (title card)')
        elif sel_entry.get('sound') == 'source': shifts.append(0.0); kinds.append('copied (source sound)')
        elif not heard_near(e, ref_fps, typical, heard): shifts.append(None); kinds.append('left out (not said in this clip)')
        else: shifts.append(typical); kinds.append(f'copied, moved {typical:+.2f} s (typical shift)')
    left_out = {i for i, k in enumerate(kinds) if k.startswith('left out')}
    windows = [((e['k0'] / ref_fps) + typical, (e['k1'] + 1) / ref_fps + typical) for i, (e, s) in enumerate(zip(events, shifts))
               if s is None and i not in left_out]
    # A standalone old brand caption is filtered with the old watermark text above, so it has no event/window.
    # Still draw its HEARD replacement (305 G01: LymphLoop); never restore the filtered old pixels.
    rw = seg.get('rewrites') or {}
    redraw_words = set()
    for change in (rw.get('dialogue') or []) + (rw.get('vo') or []):
        redraw_words.update(set(norm_tokens(change['rewrite'])) - set(norm_tokens(change['original'])))
    rendered = [c for c in render['caps'] if set(norm_tokens(' '.join(c['lines']))) & redraw_words
                or any(min(c['b'], b) - max(c['a'], a) > 0.15 * (c['b'] - c['a']) for a, b in windows)]
    shown_at = show_windows(events, shifts, ref_fps, len(line_events))
    gaps = uncovered_caps(render['caps'], rendered, events[:len(line_events)], shown_at[:len(line_events)])
    for i in merged_into(gaps, events[:len(line_events)], shown_at[:len(line_events)]):
        shifts[i] = None; kinds[i] = 'drawn new (part of a said line the copy missed)'
    rendered = sorted(rendered + gaps, key=lambda c: c['a'])
    colours = [c for c in (style.get('fill'), style.get('highlight')) if c] if style else None
    cut = mattes(orig, clean, events, colours, outline_px=max(2, round(float(style.get('stroke_width') or 0) * RH) + 2) if style else 3)
    rec = composite(base, out, events, shifts, ref_fps, (RW, RH), cut, rendered, style,
                    centre_y=(style.get('centre_y_by_segment') or {}).get(gid),
                    windows=shown_at)
    return {**rec, 'method': 'copied from the film', 'events': [{'t': round(e['k0'] / ref_fps, 2), 'text': ' / '.join(e['texts']), 'how': k}
                                                                 for e, k in zip(events, kinds)],
            'drawn': [' / '.join(c['lines']) for c in rendered],
            **({'drawn_missing': [' / '.join(c['lines']) for c in gaps]} if gaps else {})}
