"""Did the reference crop people out of the original? (user 2026-11-14: characters are generated as in the original;
nobody and no shot subject may be cut away.)

Measured 11-14 (batch6, 52 segments): references zoom-cropped to hide the old bottle that also cut a person out
(NV0560 G03/G05/G06/G07/G08, NV0481, NV0401 G08, NV0435 G08) were all failures: the model invented a new person,
new clothes or a new scene. Crops that kept every face in the frame (NV0560 G02/G04, batch5 close-ups) were fine.

For a few moments of the segment the reference frame is located inside the original frame (template matching over
scales), and the face detector (macOS Vision, or OpenCV YuNet on Windows / Linux) finds the faces in the original frame. A face whose box is mostly outside the reference's
crop window is a person the model will not see."""
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import json, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def detector(work):
    """The face detector command for this machine: macOS Vision, or OpenCV YuNet on Windows / Linux (vision_scan.py)."""
    import vision_scan
    return vision_scan.people_cmd(work)


def grab(path, t):
    import cv2
    c = cv2.VideoCapture(str(path)); c.set(cv2.CAP_PROP_POS_MSEC, t * 1000); ok, f = c.read(); c.release()
    return f if ok else None


def crop_window(S, R):
    """(score, (x, y, w, h) normalised): where the reference frame R sits inside the original frame S."""
    import cv2, numpy as np
    W = 270; s = cv2.resize(cv2.cvtColor(S, cv2.COLOR_BGR2GRAY), (W, int(W * S.shape[0] / S.shape[1])))
    r0 = cv2.cvtColor(R, cv2.COLOR_BGR2GRAY); best = (-1.0, None)
    for f in np.linspace(0.2, 1.0, 41):
        w = int(round(W * f)); h = int(round(w * r0.shape[0] / r0.shape[1]))
        if h > s.shape[0] or w < 20: continue
        m = cv2.matchTemplate(s, cv2.resize(r0, (w, h)), cv2.TM_CCOEFF_NORMED); _, v, _, loc = cv2.minMaxLoc(m)
        if v > best[0]: best = (float(v), (loc[0] / W, loc[1] / s.shape[0], w / W, h / s.shape[0]))
    return best


def covered(b, box, need):
    x0, y0 = max(b[0], box[0]), max(b[1], box[1]); x1, y1 = min(b[0] + b[2], box[0] + box[2]), min(b[1] + b[3], box[1] + box[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0) >= need * b[2] * b[3]


def lost_people(source, start_s, duration_s, reference, work, samples=8, min_score=0.8):
    """[{t, window, faces_lost, people_lost}] for the sampled moments where the reference leaves a face out."""
    import cv2
    work = Path(work); work.mkdir(parents=True, exist_ok=True); rows = []
    for k in range(samples):
        t = duration_s * (k + 0.5) / samples
        S, R = grab(source, start_s + t), grab(reference, t)
        if S is None or R is None: continue
        score, box = crop_window(S, R)
        if box is None or score < min_score or box[2] > 0.95: continue      # not located, or not cropped
        p = work/f'frame_{k}.jpg'; cv2.imwrite(str(p), S); rows.append((t, score, box, p))
    if not rows: return []
    found = {}
    for line in subprocess.check_output([*detector(work)] + [str(r[3]) for r in rows]).decode('utf-8').splitlines():
        d = json.loads(line); found[d['path']] = d
    out = []
    for t, score, box, p in rows:
        d = found.get(str(p), {})
        faces = [f for f in d.get('faces', []) if not covered(f, box, 0.25)]
        people = [f for f in d.get('people', []) if not covered(f, box, 0.3)]
        if faces:
            out.append({'t': round(t, 2), 'window': [round(x, 3) for x in box], 'score': round(score, 2),
                        'faces_lost': len(faces), 'people_lost': len(people)})
    return out
