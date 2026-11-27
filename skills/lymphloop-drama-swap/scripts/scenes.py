#!/usr/bin/env python3
"""Lock levels for every hard cut (user rule, 2026-10-24).

  paragraph lock  the next shot is in a completely different environment (florist shop -> living room -> hair salon).
                  A paragraph never crosses it; exploring a segment's edges forward or backward stops here.
  shot lock       same environment, but the next shot has no continuity with the previous one (a time jump, other
                  clothes, an unrelated new action). Lower level than a paragraph lock; a segment edge also locks here.
  none            an ordinary hard cut inside one continuous moment (reverse angle, close-up, insert of the same
                  conversation). The planner only cuts here when a locked block is longer than the 15 s ceiling.

Gemini Flash looks at one strip per shot (start / middle / end frame) in order and labels each cut; the soundtrack
dropping to near silence at a cut is passed along as a hint only (title cards are visible in the strips). Results
are cached in RUN/plan/scenes.json with Flash's location names and reason for every cut. The agent checks them against
the shot sheets and corrects any cut in RUN/plan/lock_overrides.json: {"S019": "shot", "S020": "paragraph", "S007": "none"}
(key = the shot AFTER the cut).

  scenes.py RUN            # print the lock level of every cut of a run's working master
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, hashlib, json, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media  # noqa: E402

WINDOW = 0.05          # RMS window, seconds
NEAR_CUT = 0.15        # look this far either side of the cut for the dip
CONTEXT = 2.0          # local median level is taken over this many seconds either side
SILENCE_RATIO = 0.08   # dip below this fraction of the local median ...
SILENCE_GLOBAL = 0.05  # ... and below this fraction of the whole clip's median = the bed was cut (a hint, not a rule)
BATCH = 24             # shots per Flash request (consecutive windows overlap by one shot)
ZONE_S = 20.0          # only cuts within this many seconds of a replaced shot are asked (a segment is at most 14.9 s)
WORKERS = 6            # Flash requests at once (user 2026-11-19: 12 batches one after another took 11 min)
STRIP_H = 240
LEVELS = ('paragraph', 'shot', 'none')

PROMPT = (
    'These images are consecutive shots of one video ad, in order. Each image is one shot: its first, middle and last '
    'frame side by side, labelled before it with the shot id and time. For EVERY cut between two consecutive shots, '
    'decide two things.\n'
    'same_location: is the next shot in the same physical environment as the previous one? A different camera angle, a '
    'wide shot, a close-up of a face, hands or an object, or another person in the same room is the SAME location. A '
    'different room or place (for example a florist shop, then a kitchen, then a hair salon) is NOT.\n'
    'continuous: does the next shot continue the same moment? Same people, same clothes, same time and the same ongoing '
    'action or conversation (a reverse angle or an insert of the same conversation) is continuous. A time jump (other '
    'clothes, day/night change, a "WEEK 1" or "MONDAY" card), or an unrelated new action, is NOT continuous.\n'
    'Two exceptions belong to the ongoing scene (same_location true, continuous true): (1) cross-cutting inside ONE '
    'continuing conversation or action between adjoining spaces, such as the two sides of a door, a phone call, or a '
    'hallway and the room it opens into; (2) a cutaway that is not a real place - a 3D or medical animation, a diagram, '
    'a graphic or product card - inserted into a scene that resumes afterwards; apply this both going into the cutaway '
    'and coming back out of it.\n'
    'Name the location of the next shot in a few words. Judge from the pictures; the audio hints are only supporting '
    'evidence. Answer strictly in the JSON schema, one entry per cut, in order.')
SCHEMA = {'type': 'object', 'properties': {'cuts': {'type': 'array', 'items': {'type': 'object', 'properties': {
    'after_shot': {'type': 'string'}, 'next_shot': {'type': 'string'}, 'same_location': {'type': 'boolean'},
    'continuous': {'type': 'boolean'}, 'location_next': {'type': 'string'}, 'reason': {'type': 'string'}},
    'required': ['after_shot', 'next_shot', 'same_location', 'continuous', 'location_next', 'reason']}}}, 'required': ['cuts']}


def _rms_track(master, sr=16000):
    import numpy as np
    pcm = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-i', str(master), '-vn', '-ac', '1', '-ar', str(sr), '-f', 's16le', '-'],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, np.int16).astype(float)
    n = int(sr * WINDOW); k = len(x) // n
    return np.sqrt((x[:k * n].reshape(k, n) ** 2).mean(axis=1)) if k else np.zeros(0)


def audio_breaks(rms, cuts):
    """{cut seconds: {'dip', 'median', 'ratio'}} for cuts where the bed drops to near digital silence (hint only)."""
    import numpy as np
    out = {}
    if not len(rms) or not (rms > 0).any(): return out
    whole = float(np.median(rms[rms > 0]))
    for c in cuts:
        i = int(round(c / WINDOW))
        near = rms[max(0, i - int(NEAR_CUT / WINDOW)):i + int(NEAR_CUT / WINDOW) + 1]
        ctx = rms[max(0, i - int(CONTEXT / WINDOW)):i + int(CONTEXT / WINDOW) + 1]
        if not len(near) or not len(ctx): continue
        med = float(np.median(ctx)); dip = float(near.min())
        if med > 0 and dip / med < SILENCE_RATIO and dip / whole < SILENCE_GLOBAL:
            out[c] = {'dip': round(dip, 1), 'median': round(med, 1), 'ratio': round(dip / med, 3)}
    return out


QUIET_RATIO = 0.12     # a cut whose quietest 50 ms is below this fraction of the local level has nobody talking on it


def quiet_cuts(rms, cuts, ratio=QUIET_RATIO):
    """Cut times where the soundtrack goes quiet right at the cut. ASR start/end times are off by up to ~0.3 s; at a
    quiet cut nobody is speaking, so it is sentence-safe whatever the transcript says (v025 166.38 s: Flash put
    "Four" 0.26 s before the cut, the audio dips to 3 % on it, and a reference cut there held "Four herbs" whole)."""
    import numpy as np
    out = set()
    if not len(rms): return out
    for c in cuts:
        i = int(round(c / WINDOW))
        near = rms[max(0, i - 2):i + 3]; ctx = rms[max(0, i - int(CONTEXT / WINDOW)):i + int(CONTEXT / WINDOW) + 1]
        if len(near) and len(ctx) and float(np.median(ctx)) > 0 and float(near.min()) / float(np.median(ctx)) < ratio: out.add(c)
    return out


def shot_strip(master, shot, fps, out):
    """One JPEG per shot: first, middle and last frame side by side."""
    if out.is_file(): return out
    from PIL import Image
    import io
    a, b = shot['in_frame'] / fps, shot['out_frame'] / fps
    times = [a + min(0.08, (b - a) / 4), (a + b) / 2, max(a, b - min(0.1, (b - a) / 4))]
    ims = []
    for t in times:
        raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-ss', f'{t:.3f}', '-i', str(master), '-frames:v', '1',
                              '-vf', f'scale=-2:{STRIP_H}', '-f', 'image2pipe', '-vcodec', 'png', '-'], capture_output=True, check=True).stdout
        ims.append(Image.open(io.BytesIO(raw)).convert('RGB'))
    w, h = ims[0].size
    strip = Image.new('RGB', (w * 3 + 8, h), 'white')
    for i, im in enumerate(ims): strip.paste(im, (i * (w + 4), 0))
    out.parent.mkdir(parents=True, exist_ok=True); strip.save(out, quality=85)
    return out


def level_of(same_location, continuous):
    return 'paragraph' if not same_location else ('none' if continuous else 'shot')


def wanted_cuts(shots, fps, zone_s=ZONE_S):
    """Cut indices k (cut before shot k) within zone_s of a shot marked replace; None when no decisions are given."""
    rep = [s for s in shots if s.get('decision') == 'replace' or
           any((s.get('rewrites') or {}).get(k) for k in ('dialogue', 'vo'))]
    if not any('decision' in s for s in shots): return None
    spans = [(s['in_frame'] / fps - zone_s, s['out_frame'] / fps + zone_s) for s in rep]
    return {k for k in range(1, len(shots)) if any(a <= shots[k]['in_frame'] / fps <= b for a, b in spans)}


def classify_cuts(run, master, shots, fps, hints, call=None, wanted=None):
    """{k: verdict} for the cut before shot k (k ≥ 1), from Flash looking at the shot strips. With `wanted`, only those
    cuts are asked (in parallel requests); every other cut is far from any product and counts as a paragraph lock."""
    if call is None:
        import agent_native
        if agent_native.is_native():
            # Local zero-Gemini cut classifier: compare HSV color histograms across the cut
            # plus audio RMS dip hints, and save boundary strips for agent lock_overrides review.
            import cv2, numpy as np
            strips = Path(run)/'plan'/'scene_strips'
            out = {}
            cut_ks = sorted(wanted) if wanted is not None else list(range(1, len(shots)))
            def _hist(img_path):
                im = cv2.imread(str(img_path))
                if im is None: return None
                hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
                h = cv2.calcHist([hsv], [0, 1], None, [18, 8], [0, 180, 0, 256])
                cv2.normalize(h, h)
                return h.flatten()
            strip_paths = {}
            for k in cut_ks:
                for idx in (k - 1, k):
                    if idx not in strip_paths:
                        strip_paths[idx] = shot_strip(master, shots[idx], fps, strips/f"{shots[idx]['id']}.jpg")
            for k in cut_ks:
                h1, h2 = _hist(strip_paths[k - 1]), _hist(strip_paths[k])
                corr = float(cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL)) if (h1 is not None and h2 is not None) else 0.5
                has_dip = k in hints
                same_loc = corr >= 0.45 and not (has_dip and corr < 0.65)
                cont = corr >= 0.65 and not has_dip
                out[k] = {
                    'shot': shots[k]['id'], 'after': shots[k - 1]['id'], 'at_s': round(shots[k]['in_frame'] / fps, 3),
                    'level': level_of(same_loc, cont), 'same_location': same_loc, 'continuous': cont,
                    'location_next': None, 'reason': f'local hist corr={corr:.2f}' + (f'; {hints[k]}' if has_dip else ''),
                    **({'hint': hints[k]} if has_dip else {})
                }
            for k in range(1, len(shots)):
                if wanted is not None and k not in wanted:
                    out[k] = {'shot': shots[k]['id'], 'after': shots[k - 1]['id'], 'at_s': round(shots[k]['in_frame'] / fps, 3),
                              'level': 'paragraph', 'reason': f'more than {ZONE_S:.0f} s from any replaced shot: not asked', 'far': True}
            return out
        import flash_screen
        call = lambda parts: flash_screen.call_flash(parts, SCHEMA, run)['result']
    strips = Path(run)/'plan'/'scene_strips'
    ids = {s['id']: k for k, s in enumerate(shots)}
    out = {}
    cut_ks = sorted(wanted) if wanted is not None else list(range(1, len(shots)))
    runs = []                                     # consecutive wanted cuts -> shot windows of at most BATCH shots
    for k in cut_ks:
        if runs and k == runs[-1][-1] + 1 and len(runs[-1]) < BATCH - 1: runs[-1].append(k)
        else: runs.append([k])
    windows = [[run_[0] - 1] + run_ for run_ in runs]

    def ask(window):
        parts = [('text', PROMPT)]
        for k in window:
            s = shots[k]
            parts.append(('text', f"Shot {s['id']} ({s['in_frame'] / fps:.2f}-{s['out_frame'] / fps:.2f} s)"))
            parts.append(('image', str(shot_strip(master, s, fps, strips/f"{s['id']}.jpg"))))
        notes = [f"cut {shots[k - 1]['id']} -> {shots[k]['id']}: {hints[k]}" for k in window[1:] if k in hints]
        if notes: parts.append(('text', 'Audio hints:\n' + '\n'.join(notes)))
        return window, call(parts)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max(1, min(WORKERS, len(windows)))) as ex:
        answers = list(ex.map(ask, windows))
    for window, answer in answers:
        for c in answer.get('cuts', []):
            k = ids.get(c.get('next_shot'))
            if k is None or k not in window[1:]: continue
            out[k] = {'shot': shots[k]['id'], 'after': shots[k - 1]['id'], 'at_s': round(shots[k]['in_frame'] / fps, 3),
                      'level': level_of(c['same_location'], c['continuous']), 'same_location': c['same_location'],
                      'continuous': c['continuous'], 'location_next': c.get('location_next'), 'reason': c.get('reason'),
                      **({'hint': hints[k]} if k in hints else {})}
    for k in range(1, len(shots)):
        if wanted is not None and k not in wanted:
            out[k] = {'shot': shots[k]['id'], 'after': shots[k - 1]['id'], 'at_s': round(shots[k]['in_frame'] / fps, 3),
                      'level': 'paragraph', 'reason': f'more than {ZONE_S:.0f} s from any replaced shot: not asked', 'far': True}
    for k in range(1, len(shots)):   # a cut Flash skipped: treat as continuous, flagged for the agent
        out.setdefault(k, {'shot': shots[k]['id'], 'after': shots[k - 1]['id'], 'at_s': round(shots[k]['in_frame'] / fps, 3),
                           'level': 'none', 'reason': 'not labelled by Flash: check the sheets', 'unlabelled': True})
    return out


def cut_levels(run, master, shots, fps, call=None):
    """Every cut's lock level, cached per master sha + shot list; RUN/plan/lock_overrides.json wins."""
    run = Path(run); cache = run/'plan'/'scenes.json'
    key = {'master_sha256': hashlib.sha256(Path(master).read_bytes()).hexdigest(),
           'shots': [[s['in_frame'], s['out_frame']] for s in shots], 'prompt': hashlib.sha256(PROMPT.encode()).hexdigest()[:16], 'v': 4}
    wanted = wanted_cuts(shots, fps)
    key['wanted'] = sorted(wanted) if wanted is not None else None
    cuts = None
    ov_path = run/'plan'/'lock_overrides.json'
    ov = json.loads(ov_path.read_text()) if ov_path.is_file() else {}
    if ov and all(shots[k]['id'] in ov for k in range(1, len(shots))):   # the agent labelled every cut by hand: no Flash call
        cuts = {k: {'shot': shots[k]['id'], 'after': shots[k - 1]['id'], 'at_s': round(shots[k]['in_frame'] / fps, 3),
                    'level': 'none', 'reason': 'labelled in lock_overrides.json'} for k in range(1, len(shots))}
    if cuts is None and cache.is_file():
        old = json.loads(cache.read_text())
        if old.get('key') == key and 'cuts' in old: cuts = {int(k): v for k, v in old['cuts'].items()}
    if cuts is None:
        times = {k: shots[k]['in_frame'] / fps for k in range(1, len(shots))}
        audio = audio_breaks(_rms_track(master), list(times.values()))
        hints = {k: f"the soundtrack drops to {audio[t]['ratio']:.0%} of the local level at this cut" for k, t in times.items() if t in audio}
        cuts = classify_cuts(run, master, shots, fps, hints, call, wanted=wanted)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({'key': key, 'cuts': {str(k): v for k, v in sorted(cuts.items())}}, ensure_ascii=False, indent=1))
    if ov:
        ids = {s['id']: k for k, s in enumerate(shots)}
        for sid, level in ov.items():
            if sid in ids and ids[sid] > 0 and level in LEVELS:
                cuts[ids[sid]] = {**cuts.get(ids[sid], {}), 'level': level, 'override': True}
    return cuts


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('run'); a = p.parse_args()
    import swap
    run, state = swap.load(a.run)
    master = swap.working_master(state)
    dec = json.loads((run/'plan/decisions.json').read_text())['shots']
    fps = int(media.ratio(media.probe(str(master))['fps']))
    for k, c in sorted(cut_levels(run, master, dec, fps).items()):
        print(f"{c['after']} | {c['shot']}  {c['at_s']:8.2f}s  {c['level']:<9} {c.get('location_next') or ''} :: {c.get('reason') or ''}")


if __name__ == '__main__':
    main()
