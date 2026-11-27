#!/usr/bin/env python3
"""Text and face detection for the skill on any OS.

Two engines write the same JSON, so the callers do not care which one ran:

  vision    macOS Vision through the Swift scanners (scan_text / scan_watermark / scan_people), compiled once.
            The default on a Mac that has swiftc.
  rapidocr  RapidOCR (PP-OCR ONNX models on onnxruntime) for text and OpenCV YuNet for faces. The default on
            Windows and Linux; LYMPHLOOP_OCR_ENGINE=rapidocr selects it on a Mac too.

Callers ask for a command prefix and append the scanner's arguments, exactly as they did with the Swift binary:

    subprocess.run([*vision_scan.text_cmd(work), CLIP, OUT_JSON], check=True)
    subprocess.run([*vision_scan.watermark_cmd(work), SOURCE, OUT_JSON, SHA256, PATTERN, TARGET, STEP, RANGES], check=True)
    subprocess.check_output([*vision_scan.people_cmd(work), IMG, ...])       # JSON lines

Run as a program this file is the rapidocr engine:

    vision_scan.py text SOURCE OUTPUT_JSON
    vision_scan.py watermark SOURCE OUTPUT_JSON SOURCE_SHA256 [PATTERN TARGET [STEP RANGES]]
    vision_scan.py people IMG...

The face model (232 KB, OpenCV Zoo YuNet, MIT) is downloaded and checked the first time it is needed, next to the
LaMa model. Only faces are found by this engine; "people" stays empty (the callers decide on faces).
"""
from __future__ import annotations
import oscompat  # noqa: F401  (first: UTF-8 mode on Windows)
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL_DIR = Path(os.environ.get('LYMPHLOOP_MODEL_DIR', Path.home()/'.cache'/'lymphloop-models'))
YUNET_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
YUNET_SHA256 = '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'
DEFAULT_PATTERN, DEFAULT_TARGET = '(PHOR|HORI|RHOR|MPHO(?!O))', 'LYMPHORIA'
ENGINE_NOTE = 'RapidOCR (PP-OCR ONNX on onnxruntime)'


def engine():
    """'vision' or 'rapidocr' for this machine (LYMPHLOOP_OCR_ENGINE overrides)."""
    want = os.environ.get('LYMPHLOOP_OCR_ENGINE', '').strip().lower()
    if want in ('vision', 'rapidocr'): return want
    return 'vision' if oscompat.MACOS and shutil.which('swiftc') else 'rapidocr'


# ---------------------------------------------------------------- command prefixes for the callers
def _compiled(work, name):
    """A Swift scanner, (re)built atomically under a lock. Threads that OCR in parallel (review, select, captions run
    4-12 at once) used to rebuild it together whenever the .swift was newer -- after every skill sync -- and one ran
    the half-written file: "scan_text ... returned non-zero exit status 1" (2026-11-20 film 1753, four times, never
    reproducible by hand)."""
    work = Path(work); binary = work/name; src = HERE/f'{name}.swift'
    if binary.is_file() and binary.stat().st_mtime >= src.stat().st_mtime: return binary
    work.mkdir(parents=True, exist_ok=True)
    with oscompat.locked(work/f'.{name}.lock'):
        if not binary.is_file() or binary.stat().st_mtime < src.stat().st_mtime:
            fd, tmp = tempfile.mkstemp(dir=work, prefix=f'.{name}.'); os.close(fd)
            subprocess.run(['swiftc', '-O', str(src), '-o', tmp], check=True, capture_output=True)
            os.replace(tmp, binary)
    return binary


def _cmd(work, name, mode):
    if engine() == 'vision': return [str(_compiled(work, name))]
    return [sys.executable, str(HERE/'vision_scan.py'), mode]


def text_cmd(work): return _cmd(work, 'scan_text', 'text')
def watermark_cmd(work): return _cmd(work, 'scan_watermark', 'watermark')
def people_cmd(work): return _cmd(work, 'scan_people', 'people')


def argv(cmd):
    """A scanner as a command list: text_cmd() and friends return a list, older callers hold one program path."""
    return [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]


def fingerprint(cmd):
    """Identity of a scanner command for result caches: the compiled binary, or this engine's code and version."""
    h = hashlib.sha256(); cmd = argv(cmd)
    if len(cmd) == 1: h.update(Path(cmd[0]).read_bytes())
    else:
        h.update(Path(__file__).read_bytes())
        try:
            from importlib.metadata import version
            h.update(version('rapidocr').encode())
        except Exception: pass
    return h.hexdigest()


# ---------------------------------------------------------------- rapidocr engine
_ocr = None


def fail(text):
    print(text, file=sys.stderr); raise SystemExit(1)


def reader():
    global _ocr
    if _ocr is None:
        try: from rapidocr import RapidOCR
        except ImportError:
            fail(f'Text recognition on this OS needs RapidOCR: "{sys.executable}" -m pip install rapidocr')
        # Box size matched to Vision, which the callers' thresholds were tuned on (caption line height, masks, the
        # stamp that covers a watermark). Measured 2026-11-26 on 103 lines of 6 frames, same-text lines only: the
        # stock detector (dilation, unclip 1.6) drew boxes 1.23x as tall as Vision's (up to 1.74x); without dilation
        # and unclip 1.2 / 1.4 the median is 0.99x / 1.03x with the same lines read.
        params = {'Global.log_level': 'error', 'Det.use_dilation': False, 'Det.unclip_ratio': 1.3}
        threads = os.environ.get('LYMPHLOOP_OCR_THREADS', '').strip()
        if threads.isdigit() and int(threads) > 0: params['EngineConfig.onnxruntime.intra_op_num_threads'] = int(threads)
        _ocr = RapidOCR(params=params)
    return _ocr


def read_lines(img):
    """[(text, confidence, [x, y, w, h])] for one BGR frame: whole text lines, boxes upright and inside the frame."""
    import numpy as np
    r = reader()(img)
    if r is None or r.boxes is None or r.txts is None: return []
    H, W = img.shape[:2]; out = []
    for quad, text, score in zip(r.boxes, r.txts, r.scores if r.scores is not None else [0.0] * len(r.txts)):
        q = np.asarray(quad, dtype=float).reshape(-1, 2)
        x0, y0 = max(0, int(np.floor(q[:, 0].min()))), max(0, int(np.floor(q[:, 1].min())))
        x1, y1 = min(W, int(np.ceil(q[:, 0].max()))), min(H, int(np.ceil(q[:, 1].max())))
        if x1 > x0 and y1 > y0 and str(text).strip(): out.append((str(text), float(score), [x0, y0, x1 - x0, y1 - y0]))
    return out


def write_json(path, record, pretty=False):
    path = Path(path); tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2 if pretty else None), encoding='utf-8')
    os.replace(tmp, path)


def scan_text(source, out_json):
    import cv2
    source = Path(source).resolve(); cap = cv2.VideoCapture(str(source))
    if not cap.isOpened(): fail('Cannot open asset')
    dets, frame, W, H = [], 0, 0, 0
    while True:
        ok, img = cap.read()
        if not ok: break
        H, W = img.shape[:2]
        for text, conf, box in read_lines(img):
            dets.append({'frame': frame, 'text': text, 'confidence': conf, 'bbox': box})
        frame += 1
    cap.release()
    if frame == 0: fail('Cannot decode input')
    write_json(out_json, {'cpu_fallback_frames': 0, 'type': 'lymphloop_text_scan', 'source': str(source), 'frame_count': frame,
                          'width': W, 'height': H, 'detections': dets, 'engine': ENGINE_NOTE + ', every frame'})


def parse_ranges(text):
    out = []
    for part in (text or '').split(','):
        ab = part.split('-')
        if len(ab) == 2 and ab[0].strip().isdigit() and ab[1].strip().isdigit(): out.append((int(ab[0]), int(ab[1])))
    return out


def scan_watermark(source, out_json, sha256, pattern=DEFAULT_PATTERN, target=DEFAULT_TARGET, step=1, ranges=None):
    """Fuzzy watermark OCR: keep the lines whose letters match `pattern`. `ranges` are frames [a, b); every `step`-th
    frame of each range is read."""
    import cv2
    source = Path(source).resolve(); step = max(1, int(step)); ranges = list(ranges or [])
    rx = re.compile(pattern); own = re.compile('L[yу]')      # the cursive LymphLoop logo reads as "LyMPHOR"
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened(): fail('Cannot open asset')

    def wanted(f):
        if not ranges: return f % step == 0
        base = next((a for a, b in ranges if a <= f < b), None)
        return base is not None and (f - base) % step == 0

    frame = 0; last = max((b for _, b in ranges), default=None)
    if ranges:
        lo = max(0, min(a for a, _ in ranges))
        if lo > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, lo)
            if int(round(cap.get(cv2.CAP_PROP_POS_FRAMES))) == lo: frame = lo
            else: cap.release(); cap = cv2.VideoCapture(str(source))    # this container does not seek by frame: read from 0
    dets, scanned, decoded = [], 0, 0
    while last is None or frame < last:
        if not cap.grab(): break
        decoded += 1
        if wanted(frame):
            ok, img = cap.retrieve()
            if not ok: fail(f'Cannot decode frame {frame}')
            scanned += 1; H, W = img.shape[:2]
            for text, conf, (x, y, w, h) in read_lines(img):
                letters = ''.join(c for c in text.upper() if c.isalpha())
                if not rx.search(letters) or own.search(text): continue
                # whole line box plus 2 px: the droplet icon in front of the word is part of the watermark
                x0, y0 = max(0, x - 2), max(0, y - 2); x1, y1 = min(W, x + w + 2), min(H, y + h + 2)
                dets.append({'frame': frame, 'text': text, 'confidence': conf, 'bbox': [x0, y0, x1 - x0, y1 - y0], 'classification': 'unreviewed'})
        frame += 1
    cap.release()
    if decoded == 0: fail('Cannot decode input')
    write_json(out_json, {'cpu_fallback_frames': 0, 'type': 'lymphloop_watermark_scan', 'target': target,
                          'source': {'path': str(source), 'sha256': sha256}, 'frame_count': frame, 'scanned_frames': scanned,
                          'decoded_frames': decoded, 'step': step, 'coverage': 'every_frame' if step == 1 and not ranges else 'partial',
                          'detections': dets, 'overlay_classification_reviewed': False,
                          'engine': f'{ENGINE_NOTE}, fuzzy {target} match {pattern}, whole-line boxes; detections are candidates, not proof of semantic absence'},
               pretty=True)


def yunet_model():
    p = MODEL_DIR/'face_detection_yunet_2023mar.onnx'
    if p.is_file(): return p
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    with oscompat.locked(MODEL_DIR/'.yunet.lock'):
        if p.is_file(): return p
        part = p.with_name(f'{p.name}.{os.getpid()}.part')
        print(f'downloading the face model to {p} (232 KB) ...', file=sys.stderr)
        urllib.request.urlretrieve(YUNET_URL, part)
        got = hashlib.sha256(part.read_bytes()).hexdigest()
        if got != YUNET_SHA256:
            part.unlink(); fail(f'face model checksum mismatch: {got}')
        os.replace(part, p)
    return p


def scan_people(paths, min_conf=0.6):
    """One JSON line per image: {"path", "faces": [[x, y, w, h]], "people": []}; boxes normalised, origin top-left."""
    import cv2, numpy as np
    model = str(yunet_model())
    for path in paths:
        try: img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)    # fromfile: non-ASCII paths on Windows
        except OSError: img = None
        if img is None:
            print(json.dumps({'path': str(path), 'error': 'unreadable'}, ensure_ascii=False)); continue
        H, W = img.shape[:2]
        _, found = cv2.FaceDetectorYN.create(model, '', (W, H), min_conf, 0.3, 5000).detect(img)
        faces = [[float(max(0.0, f[0]) / W), float(max(0.0, f[1]) / H), float(f[2] / W), float(f[3] / H)] for f in (found if found is not None else [])]
        print(json.dumps({'path': str(path), 'faces': faces, 'people': []}, ensure_ascii=False))
    sys.stdout.flush()


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if a and a[0] == 'text' and len(a) == 3: return scan_text(a[1], a[2])
    if a and a[0] == 'watermark' and len(a) in (4, 6, 8):
        pattern, target = (a[4], a[5]) if len(a) >= 6 else (DEFAULT_PATTERN, DEFAULT_TARGET)
        step, ranges = (int(a[6]) if a[6].isdigit() else 1, parse_ranges(a[7])) if len(a) == 8 else (1, [])
        return scan_watermark(a[1], a[2], a[3], pattern, target, step, ranges)
    if a and a[0] == 'people' and len(a) >= 2: return scan_people(a[1:])
    fail(__doc__)


if __name__ == '__main__':
    main()
