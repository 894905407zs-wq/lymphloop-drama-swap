#!/usr/bin/env python3
"""Character cards for goof redos in card mode (user 2026-11-23: "第一次生成采用原片参考的模式，如果第一次出现穿帮了，
第二次补生就要采用人物卡模式").

A redo in card mode has no reference video: the model gets the people from these cards, the product from the product
images and everything else from the prompt, so nothing of the competitor's container or the reference's motion can be
copied (2026-11-23 film 1603578481269031 G01: three shots of a tossed, side-on, stretched carton redone in one go).

    RUN/cards/cast.json        written by the agent:
      {"characters": {"WALT": {"zh": "胖男人（左）",
                               "identity": "OBESE Black American man, about 290 lb ... (body weight first, quantified)",
                               "body": "...", "outfit": "...", "expression": "...",
                               "srcs": [{"t": 113.2, "crop": [0, 0, 1, 0.62]}, {"t": 140.6}]}}}
      srcs: the clearest full faces of this person anywhere in the film (not only the goof segment), source seconds
      of the working master; crop = [x0, y0, x1, y1] fractions to cut the caption line or another person away.
    cards.py build --run RUN [--only WALT] [--force]
      -> RUN/cards/src/WALT/*.png, RUN/cards/WALT.png (4:3, portrait left, full body front + back right, white),
         RUN/cards/WALT.json (prompt, task, cost; older versions kept as WALT_vN.png)

Cards are images (about $0.10 each), not paid video generations: rebuild a wrong one with --force after fixing its
identity sentence. Lessons (nano-banana-card-lessons): weight and belly go first and quantified, or the model slims the
person; a card that looks like a celebrity gets refused by the video model's moderation ("@Image2 could not be
prepared"): rebuild it from other source frames.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media  # noqa: E402

IMAGE_MODEL = 'nano-banana-pro'     # 2026-11-02: the nano-banana-2 aliases were refused; pro 4:3 2K passes first time
STYLE_REAL = ("PHOTOREALISTIC: a real human being, photographed as-is. True-to-life skin with pores, fine lines and uneven tone, real hair strands, real fabric weave and creases. "
              "NOT an illustration, NOT a 3D render, NOT stylized, NOT beauty-retouched, NOT a fashion or studio photoshoot look. "
              "Lighting: perfectly flat, even, shadowless ambient light from every direction — no key light, no rim light, no visible light direction, no highlights that imply a lamp, "
              "no shadow cast on the background or floor, no strobe or softbox look, no lens flare. Neutral white balance, no color grading, no vignette.")
LAYOUT43 = ("Layout: one 4:3 character reference sheet on a pure seamless white background (#FFFFFF edge to edge), no floor line, no horizon, no props, no environment, "
            "no text, no letters, no labels, no captions, no watermark, no frame lines. "
            "Left half of the frame: one large head-and-shoulders portrait, straight-on frontal view, neutral relaxed expression, eyes to camera, face filling most of the left half. "
            "Right half: two full-body views standing side by side, a plain natural stance with arms hanging at the sides and feet fully visible — full-body FRONT view and full-body BACK view. "
            "Exactly the same person, same outfit, same proportions in all three views.")
SRC_PREFIX = ("The reference photos are film stills of ONE real person. Reproduce this exact person: same face shape, hairline, eye shape and color, nose, mouth, skin tone and apparent age. "
              "Ignore the stills' background, lighting, camera angle, expression, clothing and any text in them. ")


def read(p): return json.loads(Path(p).read_text())
def write(p, d): Path(p).write_text(json.dumps(d, ensure_ascii=False, indent=1))


def cards_dir(run): return Path(run)/'cards'
def card_png(run, cid): return cards_dir(run)/f'{cid}.png'


def cast(run):
    f = cards_dir(run)/'cast.json'
    if not f.is_file(): raise SystemExit(f'{f} missing: write the characters (identity, outfit, srcs = clearest faces in the film) first')
    c = read(f).get('characters') or {}
    if not c: raise SystemExit(f'{f}: no "characters"')
    return c


def card_prompt(ch):
    for k in ('identity', 'outfit'):
        if not str(ch.get(k) or '').strip(): raise SystemExit(f'cast.json: "{k}" is empty')
    parts = [f"Create a character reference sheet of ONE real person. IDENTITY: {ch['identity'].rstrip('.')}."]
    if ch.get('body'): parts.append(f"BODY: {ch['body'].rstrip('.')}.")
    parts.append(f"OUTFIT: {ch['outfit'].rstrip('.')}.")
    if ch.get('expression'): parts.append(f"EXPRESSION baseline (kept neutral in the portrait): {ch['expression'].rstrip('.')}.")
    return (SRC_PREFIX if ch.get('srcs') else '') + ' '.join(parts) + '\n' + STYLE_REAL + '\n' + LAYOUT43


def source_frames(run, cid, ch):
    """The film stills a card is drawn from, cut from the working master (captions cropped away by the agent's crop)."""
    import swap
    from PIL import Image
    srcs = ch.get('srcs') or []
    if not srcs: return []
    if len(srcs) > 3: raise SystemExit(f'{cid}: at most 3 srcs (the clearest full face first)')
    master = Path(str(swap.working_master(swap.load(str(run))[1])))
    d = cards_dir(run)/'src'/cid; d.mkdir(parents=True, exist_ok=True); out = []
    for i, s in enumerate(srcs):
        if s.get('path'): p = Path(s['path'])
        else:
            p = d/f"{i}_{float(s['t']):.2f}.png"
            subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-y', '-ss', f"{float(s['t']):.3f}", '-i', str(master), '-frames:v', '1', str(p)], check=True)
            if s.get('crop'):
                x0, y0, x1, y1 = (float(v) for v in s['crop'])
                if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1): raise SystemExit(f'{cid}: crop is [x0, y0, x1, y1] fractions, got {s["crop"]}')
                im = Image.open(p); w, h = im.size
                im.crop((round(x0 * w), round(y0 * h), round(x1 * w), round(y1 * h))).save(p)
        small = d/f'{p.stem}.small.jpg'
        im = Image.open(p).convert('RGB'); im.thumbnail((1024, 1024)); im.save(small, quality=90)
        out.append(small)
    return out


def wait_task(key, tid, timeout_s=900):
    import apimart_wan as am
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        d = am.task_data(key, tid); st = str(d.get('status', '')).lower()
        if st in am.DONE: return d
        if st in am.FAILED: raise SystemExit(f'card task {tid} failed: {str(d.get("error") or d.get("message") or "")[:200]}')
        time.sleep(6)
    raise SystemExit(f'card task {tid}: no result after {timeout_s} s')


def image_url(d):
    for v in (d.get('result') or {}).get('images') or []:
        u = v.get('url') if isinstance(v, dict) else v
        if isinstance(u, list): u = u[0] if u else None
        if isinstance(u, str) and u.startswith('http'): return u
    return None


def _build_x100(run: Path, chars: dict, only: set, force: bool):
    import uuid, x100_gen
    x = x100_gen.client_module()
    model = getattr(x, 'CARD_MODEL', '100x-nano-banana-pro')
    done = []
    with x.Client() as c:
        for cid, ch in chars.items():
            if only and cid not in only:
                continue
            png = card_png(run, cid); led = cards_dir(run)/f'{cid}.json'
            if png.is_file() and not force:
                print(f'{cid}: exists (--force to rebuild)'); continue
            prompt = card_prompt(ch); srcs = source_frames(run, cid, ch)
            ref_ids = [x.upload(s) for s in srcs]
            mode = 'reference' if ref_ids else 'text'
            qargs = {'model': model, 'prompt': prompt, 'mode': mode, 'aspect_ratio': '4:3', 'resolution': '2K', 'references': ref_ids}
            entry = {'key': f'card-{cid}-{uuid.uuid4().hex[:12]}', 'mode': mode, 'refs': ref_ids}
            x.submit(c, 'image', qargs, entry)
            tid = entry.get('task_id')
            print(f'{cid}: {tid} (100x, {entry.get("credits")} credits)', flush=True)
            tmp = cards_dir(run)/f'.{cid}.{uuid.uuid4().hex[:8]}.tmp.png'
            x.poll(c, [entry], lambda _e, t=tmp: t, timeout_s=900)
            if entry.get('status') != 'downloaded' or not tmp.is_file():
                raise SystemExit(f'{cid}: 100x card build failed: {entry.get("error") or entry.get("status")}')
            L = read(led) if led.is_file() else {'card': cid, 'versions': []}
            if png.is_file():
                v = len(L['versions']); png.rename(cards_dir(run)/f'{cid}_v{v}.png')
            tmp.replace(png)
            L['versions'].append({'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'provider': 'x100', 'model': model,
                                  'task_id': tid, 'cost_credits': entry.get('credits_charged', entry.get('credits')),
                                  'srcs': [str(s) for s in srcs], 'prompt': prompt})
            write(led, L)
            done.append({'card': cid, 'png': str(png), 'cost_credits': entry.get('credits_charged', entry.get('credits'))})
    print(json.dumps({'built': done, 'provider': 'x100',
                      'next': '亲自看每张卡：脸像不像原片这个人、体型对不对（胖瘦按原片量化）；不对改 cast.json 再 --force --only ID'},
                     ensure_ascii=False, indent=1))


def cmd_build(a):
    import agent_native
    run = Path(a.run).resolve(); chars = cast(run)
    only = set(a.only or []); unknown = only - set(chars)
    if unknown: raise SystemExit(f'--only: not in cast.json: {sorted(unknown)}')
    if agent_native.gen_channel() == 'x100':
        _build_x100(run, chars, only, a.force)
        return
    import apimart_wan as am
    key = am.credential()
    (run/'apimart').mkdir(exist_ok=True); done = []
    for cid, ch in chars.items():
        if only and cid not in only: continue
        png = card_png(run, cid); led = cards_dir(run)/f'{cid}.json'
        if png.is_file() and not a.force: print(f'{cid}: exists (--force to rebuild)'); continue
        prompt = card_prompt(ch); srcs = source_frames(run, cid, ch)
        urls = [am.upload_image(key, s, run/'apimart'/'image_uploads.json') for s in srcs]
        body = {'model': IMAGE_MODEL, 'prompt': prompt, 'size': '4:3', 'resolution': '2K', 'n': 1, **({'image_urls': urls} if urls else {})}
        r = am.call(key, '/images/generations', payload=body)
        data = r.get('data'); data = data[0] if isinstance(data, list) and data else (data or {})
        tid = data.get('task_id') or data.get('id')
        if not tid: raise SystemExit(f'{cid}: no task id in the response')
        print(f'{cid}: {tid}', flush=True)
        d = wait_task(key, tid); url = image_url(d)
        if not url: raise SystemExit(f'{cid}: task done but no image URL')
        L = read(led) if led.is_file() else {'card': cid, 'versions': []}
        if png.is_file():
            v = len(L['versions']); png.rename(cards_dir(run)/f'{cid}_v{v}.png')
        with urllib.request.urlopen(url, timeout=300) as s: png.write_bytes(s.read())
        L['versions'].append({'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'model': IMAGE_MODEL, 'task_id': tid, 'cost': d.get('cost'),
                              'srcs': [str(s) for s in srcs], 'prompt': prompt})
        write(led, L); done.append({'card': cid, 'png': str(png), 'cost': d.get('cost')})
    print(json.dumps({'built': done, 'next': '亲自看每张卡：脸像不像原片这个人、体型对不对（胖瘦按原片量化）；不对改 cast.json 再 --force --only ID'},
                     ensure_ascii=False, indent=1))


def available(run):
    """Card ids with a built png."""
    d = cards_dir(run)
    return sorted(p.stem for p in d.glob('*.png') if '_v' not in p.stem) if d.is_dir() else []


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest='cmd', required=True)
    q = s.add_parser('build'); q.add_argument('--run', required=True); q.add_argument('--only', nargs='*'); q.add_argument('--force', action='store_true')
    q.set_defaults(fn=cmd_build)
    a = p.parse_args(); a.fn(a)


if __name__ == '__main__':
    main()
