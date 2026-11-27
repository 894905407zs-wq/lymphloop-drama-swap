#!/usr/bin/env python3
"""Caption OCR and the caption-cleaned Wan reference (G##.hq.clean.mp4), plus the review's caption and
leftover-overlay gates.

Burned-in captions in a reference clip are copied by the video model (it reads them as part of the look). Today
the cleaned reference is used for segments with conflict words (from a1, together with --mute) or after a caption
gate fail; swap.reference_clip_hq calls clean() with keep_audio=True and the source pixels, so it stays ONE encode
from the original source at a 720 short edge, CRF 4, with the original audio copied:
  1. macOS Vision OCR on every frame of the uncleaned reference (scan_text.swift) proposes text boxes;
  2. caption boxes are kept (horizontal text, centred, in the middle/lower part of the frame, caption-sized);
  3. boxes are padded and held a few frames before/after (OCR misses fades and motion blur);
  4. OpenCV TELEA inpaint per frame on pixels re-read from the source, then the single encode.
Only the reference sent to the generator is cleaned; the source master is untouched.

  caption_clean.py CLIP OUT.mp4 [--work DIR] [--short-edge 480] [--keep-audio]
The CLI defaults (480 short edge, silent output) are legacy; the pipeline does not use them.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys
from pathlib import Path
import numpy as np, cv2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as _media  # noqa: E402
FFMPEG = _media.binary('ffmpeg'); FFPROBE = _media.binary('ffprobe')   # the one tool lookup: media_pipeline.binary
PAD_X, PAD_Y, HOLD = 10, 8, 6


def probe(path):
    out = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate',
                          '-of', 'json', str(path)], check=True, capture_output=True, text=True).stdout
    s = json.loads(out)['streams'][0]
    return int(s['width']), int(s['height']), s['r_frame_rate']


def scanner(work):
    """The compiled OCR binary, (re)built atomically under a lock. Threads that OCR in parallel (review, select,
    captions run 4-12 at once) used to rebuild it together whenever the .swift was newer -- after every skill sync --
    and one ran the half-written file: "scan_text ... returned non-zero exit status 1" (2026-11-20 film 1753, four
    times, never reproducible by hand)."""
    import fcntl, os, tempfile
    binary = Path(work)/'scan_text'
    src = HERE/'scan_text.swift'
    if binary.is_file() and binary.stat().st_mtime >= src.stat().st_mtime: return binary
    Path(work).mkdir(parents=True, exist_ok=True)
    with open(Path(work)/'.scan_text.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if not binary.is_file() or binary.stat().st_mtime < src.stat().st_mtime:
            tmp = Path(tempfile.mkstemp(dir=work, prefix='.scan_text.')[1])
            subprocess.run(['swiftc', '-O', str(src), '-o', str(tmp)], check=True, capture_output=True)
            os.replace(tmp, binary)
    return binary


def is_caption(box, width, height):
    """Captions: horizontal, centred, middle-to-lower part of the frame, caption-sized. Product labels and signs
    near the edges or at the top are left alone (labels on the old bottle are replaced by the generator anyway)."""
    x, y, w, h = box
    if w <= 0 or h <= 0 or w < h * 1.2: return False          # vertical text (e.g. the rotated watermark)
    cx, cy = (x + w / 2) / width, (y + h / 2) / height
    if not 0.2 <= cx <= 0.8: return False                      # captions are centred
    if not 0.3 <= cy <= 0.93: return False                     # not the top band
    if not 0.012 * height <= h <= 0.09 * height: return False  # caption line height
    return w <= 0.95 * width


def caption_boxes(scan):
    width, height, n = scan['width'], scan['height'], scan['frame_count']
    per_frame = {}
    for d in scan['detections']:
        if is_caption(d['bbox'], width, height):
            per_frame.setdefault(d['frame'], []).append(d['bbox'])
    held = {}
    for f, boxes in per_frame.items():
        for g in range(max(0, f - HOLD), min(n, f + HOLD + 1)):
            held.setdefault(g, []).extend(boxes)
    return held, per_frame


def _tokens(text):
    import re
    return {w for w in re.findall(r"[a-z']{3,}", text.lower())}


def spoken_caption(text, words):
    """Does this text box repeat what is said? Two shared words, or a one-word box that is itself a spoken word of four
    letters or more. One short shared word is not enough: on 2026-11-11 the garbled microtext of a generated carton
    ("NOT HE DIDIE 10tel") shared "not" with the narration and 36 frames of the carton were inpainted. A box whose
    word swallowed a neighbour ("LYMPCALLED" where a caption crosses the carton) counts through the spoken word of
    five letters or more inside it."""
    toks = _tokens(text)
    shared = {t for t in toks if t in words or any(len(w) >= 5 and w in t for w in words)}
    return len(shared) >= 2 or (len(shared) == 1 and len(toks) == 1 and len(next(iter(shared))) >= 4)


def spoken_words(spoken):
    return _tokens(' '.join(spoken)) - PRODUCT_WORDS


def caption_frames(video, work, spoken=None):
    """Leftover-caption check for a generated candidate: (frames with a caption, total frames, sample texts).
    With `spoken` (the segment's transcript), a text box counts only if it shares a word with what is said, so the
    packet print ("yesnap", "6785mg") on a close-up is not mistaken for a caption."""
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    scan_path = work/(Path(video).stem + '.text.json')
    subprocess.run([str(scanner(work)), str(Path(video).resolve()), str(scan_path)], check=True, capture_output=True)
    scan = json.loads(scan_path.read_text())
    words = spoken_words(spoken) if spoken else None
    hits = [d for d in scan['detections'] if is_caption(d['bbox'], scan['width'], scan['height']) and (words is None or spoken_caption(d['text'], words))]
    frames = {d['frame'] for d in hits}
    return len(frames), scan['frame_count'], sorted({d['text'] for d in hits})[:10]


# Words printed on the yesnap packet or carton: never evidence of a leftover overlay.
PRODUCT_WORDS = {'yesnap', 'lymphloop', 'lymph', 'loop', 'herbal', 'blend', 'supplement', 'dietary', 'serving', 'lymphatic', 'drainage',
                 'support', 'cleavers', 'burdock', 'root', 'dandelion', 'bromelain', 'echinacea', 'calendula', 'clover', 'orange', 'mango',
                 'flavor', 'packets', 'function', 'botanical', 'detox', 'cleanse'}


def _scan(video, work):
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    scan_path = work/(Path(video).stem + '.text.json')
    if not scan_path.is_file() or scan_path.stat().st_mtime < Path(video).stat().st_mtime:
        subprocess.run([str(scanner(work)), str(Path(video).resolve()), str(scan_path)], check=True, capture_output=True)
    return json.loads(scan_path.read_text())


def leftover_overlay_frames(video, source_clip, work, pattern=None):
    """Text that was on the SOURCE reference (watermark, title cards like MONDAY / WEEK 1, the competitor label) and is
    still on the candidate, anywhere in the frame. The caption gate only counts spoken words in caption position, so a
    kept corner watermark passed it (v025 long-paragraph test, S019 c2 kept "© LYMPHORIA" in 43/60 frames).
    `pattern`: the competitor's fuzzy watermark pattern on upper-cased letters (competitor.fuzzy_pattern); default Lymphoria.
    Returns (frames with a leftover, total frames, sample texts)."""
    import re
    src = _scan(source_clip, work); cand = _scan(video, work)
    src_words = {w for d in src['detections'] for w in re.findall(r"[a-z]{4,}", d['text'].lower())} - PRODUCT_WORDS
    competitor = re.compile(pattern or r'PHORIA|MPHOR|HORIA')
    hits = [d for d in cand['detections'] if competitor.search(''.join(c for c in d['text'].upper() if c.isalpha()))
            or (set(re.findall(r"[a-z]{4,}", d['text'].lower())) - PRODUCT_WORDS) & src_words]
    return len({d['frame'] for d in hits}), cand['frame_count'], sorted({d['text'] for d in hits})[:10]


def clean(clip, out, work=None, short_edge=480, keep_audio=False, pixels=None, spoken=None):
    """OCR `clip` for caption boxes and inpaint them. With `pixels` ({path, ss, t, vf, size, crf, audio_copy}) the frames
    are decoded from that source window instead of from `clip` (which must be the same window already scaled to
    `size`), so the cleaned reference is still a single encode from the original source.
    `spoken` (the segment's lines) keeps only caption boxes that share a word with what is said: cleaning a GENERATED clip
    (2026-11-11, references are no longer inpainted) must not erase the packet print on a close-up."""
    clip, out = Path(clip).resolve(), Path(out).resolve()
    work = Path(work or out.parent/'caption_work').resolve(); work.mkdir(parents=True, exist_ok=True)
    scan_path = work/(clip.stem + '.text.json')
    subprocess.run([str(scanner(work)), str(clip), str(scan_path)], check=True, capture_output=True)
    scan = json.loads(scan_path.read_text())
    if spoken:
        words = spoken_words(spoken)
        scan = {**scan, 'detections': [d for d in scan['detections'] if spoken_caption(d['text'], words)]}
    held, raw = caption_boxes(scan)
    W, H, rate = probe(clip)
    if pixels:
        src = str(Path(pixels['path']).resolve()); window = ['-ss', f"{pixels['ss']:.6f}", '-t', f"{pixels['t']:.6f}", '-i', src]
        sw, sh = pixels['size']
        if (sw, sh) != (W, H): raise SystemExit(f'pixel source size {sw}x{sh} does not match OCR clip {W}x{H}')
        dec = subprocess.Popen([FFMPEG, '-v', 'error', *window, '-map', '0:v:0', *(['-vf', pixels['vf']] if pixels.get('vf') else []),
                                '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], stdout=subprocess.PIPE)
        audio = (['-ss', f"{pixels['ss']:.6f}", '-t', f"{pixels['t']:.6f}", '-i', src, '-map', '0:v:0', '-map', '1:a:0?',
                  *(['-c:a', 'copy'] if pixels.get('audio_copy') else ['-c:a', 'aac', '-b:a', '256k']),
                  *(['-af', f"apad=pad_dur={pixels['pad_s']:.6f}"] if pixels.get('pad_s') else [])] if keep_audio else ['-an'])
        vf, crf = [], str(pixels.get('crf', 12))
    else:
        if W <= H: sw, sh = short_edge, int(round(H * short_edge / W / 2)) * 2
        else: sh, sw = short_edge, int(round(W * short_edge / H / 2)) * 2
        dec = subprocess.Popen([FFMPEG, '-v', 'error', '-i', str(clip), '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], stdout=subprocess.PIPE)
        audio = ['-i', str(clip), '-map', '0:v:0', '-map', '1:a:0?', '-c:a', 'aac', '-b:a', '192k'] if keep_audio else ['-an']
        vf, crf = ['-vf', f'scale={sw}:{sh}:flags=lanczos'], '14' if keep_audio else '18'
    enc = subprocess.Popen([FFMPEG, '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', rate, '-i', '-', *audio,
                            *vf, '-c:v', 'libx264', '-preset', 'slow' if pixels else 'medium', '-crf', crf,
                            *(['-maxrate', pixels['maxrate'], '-bufsize', pixels.get('bufsize', pixels['maxrate'])] if pixels and pixels.get('maxrate') else []),
                            '-pix_fmt', 'yuv420p', '-shortest', '-movflags', '+faststart', str(out)], stdin=subprocess.PIPE)
    size = W * H * 3; i = 0; touched = 0
    while True:
        buf = dec.stdout.read(size)
        if len(buf) < size: break
        frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
        boxes = held.get(i)
        if boxes:
            mask = np.zeros((H, W), np.uint8)
            for x, y, w, h in boxes:
                mask[max(0, y - PAD_Y):min(H, y + h + PAD_Y), max(0, x - PAD_X):min(W, x + w + PAD_X)] = 255
            frame = cv2.inpaint(frame, mask, 4, cv2.INPAINT_TELEA); touched += 1
        enc.stdin.write(frame.tobytes()); i += 1
    enc.stdin.close(); enc.wait(); dec.wait()
    if enc.returncode or dec.returncode: raise SystemExit('caption clean encode failed')
    texts = sorted({d['text'] for d in scan['detections'] if is_caption(d['bbox'], scan['width'], scan['height'])})
    report = {'type': 'lymphloop_caption_clean', 'source': str(clip), 'output': str(out), 'frames': i, 'frames_cleaned': touched,
              'frames_with_ocr_captions': len(raw), 'caption_texts': texts[:40], 'output_size': [sw, sh], 'audio': 'kept' if keep_audio else 'removed',
              'pixels_from': pixels['path'] if pixels else str(clip)}
    out.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('clip'); p.add_argument('out'); p.add_argument('--work'); p.add_argument('--short-edge', type=int, default=480); p.add_argument('--keep-audio', action='store_true')
    a = p.parse_args()
    print(json.dumps(clean(a.clip, a.out, a.work, a.short_edge, a.keep_audio), ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
