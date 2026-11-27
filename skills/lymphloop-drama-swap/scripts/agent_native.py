#!/usr/bin/env python3
"""Antigravity native multimodal (see + hear) helpers and zero-external-Gemini mode.

In Antigravity's plugin copy of lymphloop-drama-swap, the executing agent itself has
native video, image, and audio understanding via `read_file`. External Gemini API calls
(`google_api.py` / `flash_screen.py`) are disabled by default; set
`LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1` only if you explicitly want to call the external API.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

_WHISPER_LOCK = threading.Lock()
_WHISPER_MODEL = None

AUDIT_FIELDS = (
    'competitor_clean',   # 1. 无竞品容器/滴管/瓶贴/四图标残留
    'front_face_ok',      # 2. 展示/近景镜头为宽正面（LYMPHATIC）朝镜头；竖着站却半秒以上看不到正面 = false（carton_check 量）
    'scale_ok',           # 3. 纸盒正面高:宽 ≤ 1.65（真盒 1.3）、任何一面长:短 ≤ 2.3；carton_check 量出超线时不能填 true
    'physics_ok',         # 4. 物理合理：条包不自站立、不撕包、服用时手中有条包
    'speech_lip_ok',      # 5. 已听生成音轨：台词完整准确、无竞品词/乱码、口型对得上
    'text_clean',         # 6. 无多余字幕、竞品水印或错乱叠加文字
)


def is_native() -> bool:
    """True when running in Antigravity native see/hear mode (zero external Gemini API).

    Enabled by default in this plugin; set LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1 to allow
    external Gemini API calls, or LYMPHLOOP_NO_EXTERNAL_GEMINI=0 to opt out.
    """
    if os.environ.get('LYMPHLOOP_ALLOW_EXTERNAL_GEMINI', '').strip().lower() in ('1', 'true', 'yes'):
        return False
    if os.environ.get('LYMPHLOOP_NO_EXTERNAL_GEMINI', '').strip().lower() in ('0', 'false', 'no'):
        return False
    return True


def gen_channel() -> str:
    """Return active video/card generation channel ('x100' or 'apimart').

    Live-tunable via ~/.cache/lymphloop-models/local-slots/channel.txt so running batch
    workers switch channels cleanly without restart. Falls back to LYMPHLOOP_GEN_CHANNEL
    and defaults to 'x100' (100x-minimax-h3 / standard / 768P / 9:16; user 2026-11-26).
    """
    slot_dir = Path(os.environ.get('LYMPHLOOP_SLOT_DIR', Path.home() / '.cache' / 'lymphloop-models' / 'local-slots'))
    ch_file = slot_dir / 'channel.txt'
    raw = ''
    if ch_file.is_file():
        try:
            raw = ch_file.read_text(encoding='utf-8').strip().lower()
        except OSError:
            raw = ''
    if not raw:
        raw = os.environ.get('LYMPHLOOP_GEN_CHANNEL', 'x100').strip().lower()
    if raw in ('apimart',):
        return 'apimart'
    return 'x100'


def _whisper():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        with _WHISPER_LOCK:
            if _WHISPER_MODEL is None:
                from faster_whisper import WhisperModel
                model_name = os.environ.get('LYMPHLOOP_WHISPER_MODEL', 'small')
                pref_dev = os.environ.get('LYMPHLOOP_WHISPER_DEVICE', 'mps' if sys.platform == 'darwin' else 'cpu').strip().lower()
                if pref_dev == 'mps':
                    try:
                        _WHISPER_MODEL = WhisperModel(model_name, device='mps', compute_type='float16')
                    except Exception:
                        _WHISPER_MODEL = WhisperModel(model_name, device='cpu', compute_type='int8')
                else:
                    _WHISPER_MODEL = WhisperModel(model_name, device='cpu', compute_type='int8')
    return _WHISPER_MODEL


def decode_audio_f32(media_path, start_s=0.0, duration_s=None):
    """Decode mono 16kHz float32 PCM via ffmpeg (avoids PyAV metadata_errors compatibility bug)."""
    import numpy as np
    import media_pipeline as media
    cmd = [media.binary('ffmpeg'), '-v', 'error']
    if start_s and start_s > 0:
        cmd += ['-ss', f'{float(start_s):.3f}']
    cmd += ['-i', str(media_path)]
    if duration_s is not None and duration_s > 0:
        cmd += ['-t', f'{float(duration_s):.3f}']
    cmd += ['-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', '-']
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    if not raw:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw, dtype=np.float32).copy()


def split_words_by_pause(words, offset_s=0.0, gap_s=0.7):
    """Group word dicts [{start, end, word}] into sentences, always splitting on pauses >= gap_s
    or strong sentence-ending punctuation so long silences never merge distant lines."""
    if not words:
        return []
    groups = []
    cur = []
    for w in words:
        text = str(w.get('word') or '').strip()
        if not text:
            continue
        ws = float(w['start']) + offset_s
        we = float(w['end']) + offset_s
        if cur and (ws - cur[-1]['end'] >= gap_s or re.search(r'[.!?]["\']?$', cur[-1]['word'])):
            groups.append(cur)
            cur = []
        cur.append({'start': ws, 'end': we, 'word': text})
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        line = ' '.join(x['word'] for x in g).strip()
        line = re.sub(r'\s+([,.!?;:])', r'\1', line)
        if not line:
            continue
        out.append({
            'start': round(g[0]['start'], 3),
            'end': round(max(g[-1]['end'], g[0]['start'] + 0.05), 3),
            'speaker': 'speaker',
            'kind': 'on_screen_dialogue',
            'text': line,
        })
    return out


def whisper_sentences(master, start_s=0.0, end_s=None, gap_s=0.7):
    """Local faster-whisper transcription with word timestamps and pause-gap splitting."""
    dur = (end_s - start_s) if end_s is not None else None
    audio = decode_audio_f32(master, start_s=start_s, duration_s=dur)
    if audio.size < 160:
        return []
    model = _whisper()
    segs, _ = model.transcribe(audio, word_timestamps=True, vad_filter=False)
    words = []
    for seg in segs:
        if seg.words:
            for w in seg.words:
                words.append({'start': float(w.start), 'end': float(w.end), 'word': w.word})
        else:
            words.append({'start': float(seg.start), 'end': float(seg.end), 'word': seg.text})
    out = split_words_by_pause(words, offset_s=float(start_s), gap_s=gap_s)
    if end_s is not None:
        out = [s for s in out if s['end'] > start_s and s['start'] < end_s]
    return out


def cached_full_transcript(run, master, gap_s=0.7):
    """Full-film local Whisper transcript cached at RUN/agent_review/transcript.json."""
    import google_api as api
    run = Path(run)
    out_dir = run / 'agent_review'
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / 'transcript.json'
    msha = api.file_sha(master)
    if p.is_file():
        old = json.loads(p.read_text(encoding='utf-8'))
        if old.get('master_sha256') == msha and old.get('gap_s') == gap_s:
            return old['sentences'], p
    sents = whisper_sentences(master, start_s=0.0, end_s=None, gap_s=gap_s)
    p.write_text(json.dumps({
        'master': str(master),
        'master_sha256': msha,
        'gap_s': gap_s,
        'sentences': sents,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return sents, p


def build_overview_grid(master, out_path, count=24, cols=6):
    """Build a crisp evenly-spaced overview contact sheet of the whole master for Step 1b."""
    import media_pipeline as media
    from PIL import Image, ImageDraw
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    info = media.probe(str(master))
    dur = float(info.get('video_duration') or 1.0)
    ff = media.binary('ffmpeg')
    tmp_dir = out_path.parent / '.overview_frames'
    tmp_dir.mkdir(parents=True, exist_ok=True)
    thumbs = []
    for i in range(count):
        t = dur * (i + 0.5) / count
        fp = tmp_dir / f'f_{i:02d}.jpg'
        subprocess.run([ff, '-v', 'error', '-y', '-ss', f'{t:.3f}', '-i', str(master),
                        '-frames:v', '1', '-vf', 'scale=240:-2', str(fp)], check=False)
        if fp.is_file():
            thumbs.append((t, Image.open(fp).convert('RGB')))
    if not thumbs:
        return None
    w, h = thumbs[0][1].size
    rows = (len(thumbs) + cols - 1) // cols
    canvas = Image.new('RGB', (cols * (w + 4), rows * (h + 24)), (18, 18, 18))
    draw = ImageDraw.Draw(canvas)
    for idx, (t, im) in enumerate(thumbs):
        r, c = divmod(idx, cols)
        x0, y0 = c * (w + 4), r * (h + 24)
        mm, ss = divmod(int(t), 60)
        draw.text((x0 + 6, y0 + 4), f'{mm:02d}:{ss:02d} ({t:.1f}s)', fill=(255, 230, 80))
        canvas.paste(im, (x0, y0 + 20))
    canvas.save(out_path, quality=88)
    return out_path


def export_voice_samples(run, master, sentences, max_samples=3, clip_s=6.0):
    """Export short MP4 voice/video clips from early/mid/late speech so Antigravity can
    directly watch & listen via read_file to note speaker age/gender/accent/tone/BGM."""
    import media_pipeline as media
    out_dir = Path(run) / 'agent_review' / 'voice_samples'
    out_dir.mkdir(parents=True, exist_ok=True)
    if not sentences:
        return []
    ff = media.binary('ffmpeg')
    picks = []
    n = len(sentences)
    indices = sorted({0, n // 2, max(0, n - 2)})[:max_samples]
    for rank, idx in enumerate(indices, 1):
        t0 = max(0.0, float(sentences[idx]['start']) - 0.2)
        out = out_dir / f'sample_{rank:02d}_{int(t0)}s.mp4'
        if not out.is_file():
            subprocess.run([
                ff, '-v', 'error', '-y', '-ss', f'{t0:.3f}', '-i', str(master),
                '-t', f'{clip_s:.2f}', '-vf', 'scale=360:-2',
                *hw_encode_args(62),
                '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(out)
            ], check=False)
        if out.is_file():
            picks.append(str(out))
    return picks


def tile_sheets(sheet_paths, out_dir, prefix='grid', cols=2, per_grid=12, thumb_max_w=720):
    """Pack many shot contact sheets into compact labeled grids for Antigravity read_file."""
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    valid = [Path(p) for p in sheet_paths if Path(p).is_file()]
    written = []
    for gi in range(0, len(valid), per_grid):
        chunk = valid[gi:gi + per_grid]
        ims = []
        for p in chunk:
            im = Image.open(p).convert('RGB')
            if im.width > thumb_max_w:
                ratio = thumb_max_w / im.width
                im = im.resize((thumb_max_w, max(1, int(im.height * ratio))), Image.LANCZOS)
            ims.append((p.stem, im))
        cw = max(im.width for _, im in ims)
        ch = max(im.height for _, im in ims)
        rows = (len(ims) + cols - 1) // cols
        canvas = Image.new('RGB', (cols * (cw + 8), rows * (ch + 28)), (20, 20, 20))
        draw = ImageDraw.Draw(canvas)
        for idx, (label, im) in enumerate(ims):
            r, c = divmod(idx, cols)
            x0, y0 = c * (cw + 8), r * (ch + 28)
            draw.text((x0 + 6, y0 + 5), label, fill=(80, 240, 240))
            canvas.paste(im, (x0, y0 + 24))
        dst = out_dir / f'{prefix}_{gi // per_grid + 1:02d}.jpg'
        canvas.save(dst, quality=86)
        written.append(str(dst))
    return written


def stack_flashback_sheets(sheet_paths, out_dir, prefix='flashback', per_grid=4):
    """Stack wide flashback seam/join sheets vertically into compact grids for read_file."""
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    valid = [Path(p) for p in sheet_paths if Path(p).is_file()]
    written = []
    for gi in range(0, len(valid), per_grid):
        chunk = valid[gi:gi + per_grid]
        ims = [(p.name, Image.open(p).convert('RGB')) for p in chunk]
        w = max(im.width for _, im in ims)
        h = sum(im.height + 28 for _, im in ims)
        canvas = Image.new('RGB', (w, h), (18, 18, 18))
        draw = ImageDraw.Draw(canvas)
        y = 0
        for label, im in ims:
            draw.text((8, y + 5), label, fill=(255, 230, 70))
            canvas.paste(im, (0, y + 26))
            y += im.height + 28
        dst = out_dir / f'{prefix}_{gi // per_grid + 1:02d}.jpg'
        canvas.save(dst, quality=88)
        written.append(str(dst))
    return written


def scan_highlight_backplates(video_path, y_band=(0.55, 0.88)):
    """Detect residual bright yellow caption highlight pills left after LaMa caption cleaning.

    Returns {'frames': int, 'y_min': int|None, 'y_max': int|None, 'sample_frames': [...]}.
    """
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(video_path))
    hits = []
    y_lo, y_hi = None, None
    idx = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        h, w = fr.shape[:2]
        y0, y1 = int(h * y_band[0]), int(h * y_band[1])
        roi = fr[y0:y1]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = (
            (hsv[:, :, 0] >= 20) & (hsv[:, :, 0] <= 38)
            & (hsv[:, :, 1] >= 145) & (hsv[:, :, 2] >= 180)
            & (roi[:, :, 0] <= 190) & (roi[:, :, 1] >= 200) & (roi[:, :, 2] >= 220)
        )
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        found = False
        for s in stats[1:]:
            if s[cv2.CC_STAT_WIDTH] >= 28 and s[cv2.CC_STAT_HEIGHT] >= 6 and s[cv2.CC_STAT_AREA] >= 180:
                found = True
                top = int(y0 + s[cv2.CC_STAT_TOP])
                bot = int(top + s[cv2.CC_STAT_HEIGHT])
                y_lo = top if y_lo is None else min(y_lo, top)
                y_hi = bot if y_hi is None else max(y_hi, bot)
        if found:
            hits.append(idx)
        idx += 1
    cap.release()
    return {
        'frames': len(hits),
        'y_min': y_lo,
        'y_max': y_hi,
        'sample_frames': hits[:8],
    }


_KNOWN_SYLLABLES = {
    'lymphoria': 4, 'lymphloop': 2, 'yesnap': 2, 'stillingia': 4, 'burdock': 2,
    'dandelion': 4, 'cleavers': 2, 'bromelain': 3, 'echinacea': 4, 'calendula': 4,
    'clover': 2, 'prickly': 2, 'dropper': 2, 'packet': 2, 'packets': 2,
    'liquid': 2, 'capsule': 2, 'capsules': 2, 'softgel': 2, 'softgels': 2,
    'gummy': 2, 'gummies': 2, 'bottle': 2, 'bottles': 2, 'box': 1, 'boxes': 2,
    'tongue': 1, 'every': 2, 'morning': 2, 'twice': 1, 'straight': 1,
}


def word_syllables(word: str) -> int:
    w = re.sub(r'[^a-z]', '', word.lower())
    if not w:
        return 0
    if w in _KNOWN_SYLLABLES:
        return _KNOWN_SYLLABLES[w]
    if len(w) <= 3:
        return 1
    w2 = re.sub(r'(?:[^laeiouy]es|ed|[^laeiouy]e)$', '', w)
    w2 = re.sub(r'^y', '', w2)
    groups = re.findall(r'[aeiouy]{1,2}', w2)
    return max(1, len(groups))


def syllable_count(text: str) -> int:
    return sum(word_syllables(tok) for tok in re.findall(r"[A-Za-z']+", text or ''))


def check_dialogue_syllables(original: str, rewrite: str):
    """Compare syllable counts of an on-screen dialogue rewrite against the original line.

    Returns a dict with counts and an optional advisory warning when syllable mismatch is
    large enough to risk Seedance lip-sync stretching/mangling (e.g. 4-syllable 'Lymphoria'
    replaced by 2-syllable 'LymphLoop' on visible lips).
    """
    old_n = syllable_count(original)
    new_n = syllable_count(rewrite)
    diff = new_n - old_n
    warn = None
    if old_n > 0 and abs(diff) >= 2 and (abs(diff) >= 3 or abs(diff) / old_n >= 0.28):
        hint = ''
        if re.search(r'\blymphoria\b', original, re.I) and re.search(r'\blymphloop\b', rewrite, re.I) and not re.search(r'\byesnap\b', rewrite, re.I):
            hint = '（例如把 2 音节 LymphLoop 改为 4 音节 Yesnap LymphLoop 以贴合 4 音节原口型）'
        warn = (
            f'对白音节差 {diff:+d}（原词 {old_n} 音节 -> 新词 {new_n} 音节）：'
            f'画内可见嘴部改词建议尽量对齐音节数{hint}，否则首次生成口型易拉扯变形；'
            f'若首次生成仍因旧口型念错，第二次补做直接走人物卡模式 + # voice 脱离原口型。'
        )
    return {'original_syllables': old_n, 'rewrite_syllables': new_n, 'diff': diff, 'warning': warn}


def parse_audit(spec) -> dict:
    """Parse --audit JSON string, file path, or comma-separated k=v flags into validated dict."""
    if not spec:
        return {}
    raw = str(spec).strip()
    if raw.startswith('{'):
        data = json.loads(raw)
    elif Path(raw).is_file():
        data = json.loads(Path(raw).read_text(encoding='utf-8'))
    else:
        data = {}
        for part in raw.split(','):
            if '=' not in part:
                continue
            k, v = (x.strip() for x in part.split('=', 1))
            lv = v.lower()
            if lv in ('1', 'true', 'yes', 'ok', 'pass'):
                data[k] = True
            elif lv in ('0', 'false', 'no', 'fail'):
                data[k] = False
            else:
                data[k] = v
    return data


def hw_encode_enabled() -> bool:
    """Use Apple VideoToolbox hardware H.264 encoder on macOS for preview/intermediate clips."""
    if sys.platform != 'darwin':
        return False
    return os.environ.get('LYMPHLOOP_HW_ENCODE', '1').strip().lower() not in ('0', 'false', 'no')


def hw_encode_args(quality=65) -> list[str]:
    if hw_encode_enabled():
        return ['-c:v', 'h264_videotoolbox', '-q:v', str(int(quality)), '-pix_fmt', 'yuv420p']
    return ['-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p']


@contextlib.contextmanager
def local_slot(kind: str = 'heavy', slots: int | None = None):
    """Machine-wide semaphore limiting heavy local compute (LaMa/recut) across parallel RUNs.

    Prevents 60-film batches from thrashing CPU threads while keeping APIMART cloud jobs
    running in full parallel. Controlled by LYMPHLOOP_LOCAL_SLOTS (default 2; 0 disables).
    """
    slot_dir = Path(os.environ.get('LYMPHLOOP_SLOT_DIR', Path.home() / '.cache' / 'lymphloop-models' / 'local-slots'))
    slot_dir.mkdir(parents=True, exist_ok=True)
    if slots is None:
        override_file = slot_dir / 'slots.txt'
        if override_file.is_file():
            try:
                n = int(override_file.read_text(encoding='utf-8').strip())
            except ValueError:
                n = int(os.environ.get('LYMPHLOOP_LOCAL_SLOTS', '4'))
        else:
            n = int(os.environ.get('LYMPHLOOP_LOCAL_SLOTS', '4'))
    else:
        n = int(slots)
    if n <= 0:
        yield
        return
    acquired = None
    while acquired is None:
        for i in range(n):
            fh = open(slot_dir / f'{kind}-{i}.lock', 'a+b')
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = fh
                break
            except OSError:
                fh.close()
        if acquired is None:
            time.sleep(0.4)
    try:
        yield
    finally:
        try:
            fcntl.flock(acquired.fileno(), fcntl.LOCK_UN)
        finally:
            acquired.close()


def audit_failures(audit: dict) -> list[str]:
    """Return names of any required goof-audit fields explicitly marked False."""
    return [k for k in AUDIT_FIELDS if audit.get(k) is False]
