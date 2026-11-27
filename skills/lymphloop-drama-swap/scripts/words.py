#!/usr/bin/env python3
"""Word-level timings, offline (faster-whisper): the agent cannot listen, so this is how a mute window is placed on
the exact conflict word and how a generated clip's lines are checked word by word.

  words.py RUN [G01,G02]              source words of each planned segment, in segment seconds -> RUN/plan/words/G##.json
  words.py RUN [G01,G02] --generated  words of each generated clip, next to the source words, with the largest shift

Mute windows: take the conflict word's start and end from the source list, leave the neighbours whole, then listen
check by transcribing the muted reference again. Needs `pip install faster-whisper` in the skill's .venv (small.en,
about 480 MB, is downloaded once into the Hugging Face cache).
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, json, re, subprocess, sys
from fractions import Fraction
from pathlib import Path
import media_pipeline as media

MODEL = 'small'          # multilingual (2026-11-19: small.en with language='en' heard a Spanish film as English gibberish)
LANGUAGE = None          # the film's language code; set_run() finds it once per RUN (cached in RUN/plan/language.json)
RUN = None


def set_run(run):
    """Tell the transcriber which RUN it works for: the film's language is detected once and reused."""
    global RUN, LANGUAGE
    RUN = Path(run) if run else None; LANGUAGE = None


def run_language():
    """The film's spoken language (whisper code), detected on a minute of its sound and cached per RUN."""
    global LANGUAGE
    if LANGUAGE or RUN is None: return LANGUAGE
    cache = RUN/'plan'/'language.json'
    if cache.is_file():
        LANGUAGE = json.loads(cache.read_text()).get('language'); return LANGUAGE
    state = RUN/'state.json'
    if not state.is_file(): return None
    st = json.loads(state.read_text()); master = ((st.get('stages') or {}).get('prepare') or {}).get('working_master', {}).get('path') or st.get('source')
    if not master or not Path(master).is_file(): return None
    dur = float(media.probe(str(master)).get('video_duration') or 0)
    audio = pcm(master, max(0.0, dur * 0.3), 60.0)
    if len(audio) < 16000: return None
    _, info = load_model(MODEL).transcribe(audio, vad_filter=True, without_timestamps=True)
    LANGUAGE = info.language
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({'language': LANGUAGE, 'probability': round(float(info.language_probability), 3), 'from': str(master)}))
    return LANGUAGE


import threading
_MODELS = {}
_MODELS_LOCK = threading.Lock()


def load_model(name=None):
    """One model per process, shared by the threads that transcribe segments at once (captions run 4 at a time)."""
    name = name or ('small.en' if LANGUAGE == 'en' else MODEL)
    with _MODELS_LOCK:
        if name not in _MODELS:
            try: from faster_whisper import WhisperModel
            except ImportError: sys.exit('words.py needs faster-whisper: "$SKILL/.venv/bin/python" -m pip install faster-whisper')
            _MODELS[name] = WhisperModel(name, device='cpu', compute_type='int8', num_workers=4)
            _MODELS[name]._lymphloop_name = name
        return _MODELS[name]


def transcribe(model, path, start_s=0.0, dur_s=None, language=None):
    """[{w, a, b}] with times in seconds from start_s. ffmpeg decodes the sound (faster-whisper's own PyAV decoder
    breaks across PyAV versions)."""
    import numpy as np
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-ss', f'{start_s:.4f}', *(['-t', f'{dur_s:.4f}'] if dur_s else []), '-i', str(path),
                          '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', '-'], check=True, capture_output=True).stdout
    audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    if len(audio) < 1600: return []
    lang = language or run_language()
    if lang and lang != 'en' and getattr(model, '_lymphloop_name', '').endswith('.en'): model = load_model(MODEL)
    segs, _ = model.transcribe(audio, word_timestamps=True, vad_filter=False, language=lang)
    return [{'w': w.word.strip(), 'a': round(w.start, 2), 'b': round(w.end, 2)} for sg in segs if not hallucinated(sg) for w in sg.words]


# Whisper's stock lines on a track with no one speaking (2026-11-22: 180 G08 a silent bathroom sip read "Thank you so
# much for watching, see you next time." no_speech 0.55, logprob -0.98; 110 G06 "It's not that bad." 0.49 / -1.42;
# 110 G04 laughs read "I am" 0.58 / -1.01). Real lines in the same films: no_speech 0.00-0.04, logprob -0.17 to -0.40.
# The prepare gate then refused the segments for words the prompt did not have.
NO_SPEECH_MIN, LOGPROB_MAX = 0.4, -0.8


def hallucinated(seg):
    return float(getattr(seg, 'no_speech_prob', 0) or 0) >= NO_SPEECH_MIN and float(getattr(seg, 'avg_logprob', 0) or 0) <= LOGPROB_MAX


def source_words(run, seg, fps, source, model=None, refresh=True):
    """Source words of a planned segment from RUN/plan/words/G##.json, transcribed again when the plan moved the
    segment (2026-11-22 film 998: re-planned from 16 to 15 segments, the cached G14/G15 still held the old G14/G15
    words and the prepare gate refused six segments for lines that are not in them). The range sits beside the words
    in G##.range.json; a file without it is from before and is transcribed again. None when there is no cache and
    refresh is off."""
    out = Path(run)/'plan/words'; wfile = out/f"{seg['id']}.json"; rfile = out/f"{seg['id']}.range.json"
    key = {'in_frame': int(seg['in_frame']), 'out_frame': int(seg['out_frame']), 'source': str(source), 'filter': [NO_SPEECH_MIN, LOGPROB_MAX]}
    if wfile.is_file() and rfile.is_file():
        try:
            if json.loads(rfile.read_text()) == key: return json.loads(wfile.read_text())
        except ValueError: pass
    if not refresh: return None
    words = transcribe(model or load_model(), source, seg['in_frame'] / fps, (seg['out_frame'] - seg['in_frame']) / fps)
    out.mkdir(parents=True, exist_ok=True)
    wfile.write_text(json.dumps(words)); rfile.write_text(json.dumps(key))
    return words


def norm(word): return re.sub(r"[^a-z0-9']", '', word.lower())


def pcm(path, start_s=0.0, dur_s=None, rate=16000):
    """Mono float samples of a stretch of a file's sound."""
    import numpy as np
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-ss', f'{start_s:.4f}', *(['-t', f'{dur_s:.4f}'] if dur_s else []), '-i', str(path),
                          '-vn', '-ac', '1', '-ar', str(rate), '-f', 's16le', '-'], check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def cut_words(audio, words, windows, rate=16000, min_s=0.05, margin_db=8.0):
    """Words a mute window cuts through: at least half of the word is inside the window, and a piece of 0.05 s or more
    stays outside it and is clearly louder than the quiet parts of the clip. The model voices such leftovers (Nuflos,
    2026-11-12: 0.06 s of the old name's first and last sound stayed in the reference and "LymphLoop" came back as
    "Lymph-loo-naps"). Neighbouring words that a window only touches are not reported.
    -> [{'window', 'word', 'side', 'left', 'widen_to'}], times in the clip's seconds."""
    import numpy as np
    def db(x): return 20 * float(np.log10(max(1e-6, float(np.sqrt(np.mean(x ** 2)))))) if len(x) else -120.0
    step = rate // 20
    frames = [db(audio[i:i + step]) for i in range(0, max(0, len(audio) - step), step)]
    floor = float(np.percentile(frames, 10)) if frames else -60.0
    out = []
    for a, b in windows:
        for w in words:
            inside = min(b, w['b']) - max(a, w['a'])
            if inside <= 0 or inside < 0.5 * (w['b'] - w['a']) or w['b'] - w['a'] < 0.2: continue      # under 0.2 s: 'a', 'in' next to the window
            for lo, hi, side in ((w['a'], a, 'start'), (b, w['b'], 'end')):
                if hi - lo >= min_s and db(audio[int(lo * rate):int(hi * rate)]) > floor + margin_db:
                    out.append({'window': [a, b], 'word': w['w'], 'side': side, 'left': [round(lo, 2), round(hi, 2)],
                                'widen_to': [round(min(a, w['a']), 2), round(max(b, w['b']), 2)]})
    return out


def largest_shift(source, generated):
    """The biggest start-time difference between a source word and the same word in the generated clip (words matched
    in order), in seconds; negative = the generated clip says it earlier. None when nothing matches."""
    j = 0; worst = None
    for w in generated:
        for k in range(j, len(source)):
            if norm(source[k]['w']) == norm(w['w']) and norm(w['w']):
                d = round(w['a'] - source[k]['a'], 2); j = k + 1
                if abs(d) < 3 and (worst is None or abs(d) > abs(worst)): worst = d
                break
    return worst


def show(words): return ' '.join(f"{w['w']}[{w['a']:.2f}-{w['b']:.2f}]" for w in words)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('run'); p.add_argument('ids', nargs='?'); p.add_argument('--generated', action='store_true'); p.add_argument('--model')
    a = p.parse_args(argv)
    set_run(a.run); run_language()
    run = Path(a.run); only = set(a.ids.split(',')) if a.ids else None
    state = json.loads((run/'state.json').read_text()); plan = json.loads((run/'plan/segments.json').read_text())
    fps = float(Fraction(str(plan['fps']))); out = run/'plan/words'; out.mkdir(parents=True, exist_ok=True)
    sel = json.loads((run/'plan/selections.json').read_text()) if (run/'plan/selections.json').is_file() else {}
    model = load_model(a.model)
    for s in plan['segments']:
        if only and s['id'] not in only: continue
        source = source_words(run, s, fps, state['source'], model)
        print(f"{s['id']} source    {show(source)}")
        if not a.generated: continue
        clip = Path(sel[s['id']]['media']['path']) if (sel.get(s['id']) or {}).get('media') else run/'generation/results'/f"{s['id']}-a1-c1.mp4"
        if not clip.is_file(): print(f"{s['id']} generated (no clip yet)"); continue
        gen = transcribe(model, clip)
        (out/f"{s['id']}.generated.json").write_text(json.dumps({'clip': str(clip), 'words': gen}))
        shift = largest_shift(source, gen)
        print(f"{s['id']} generated {show(gen)}\n{s['id']} largest shift of a shared word: {shift if shift is not None else 'n/a'} s ({clip.name})")


if __name__ == '__main__':
    main()
