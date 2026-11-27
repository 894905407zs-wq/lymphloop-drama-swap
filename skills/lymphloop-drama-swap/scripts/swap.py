#!/usr/bin/env python3
"""LymphLoop swap orchestrator: one video per RUN directory, driven by the agent — Claude or Codex (several RUNs can run in parallel).

State lives in RUN/state.json. Every command is idempotent and resumable; nothing here
looks at pictures for you — the agent reads the contact sheets this produces.
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, contextlib, hashlib, json, math, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import apimart_wan as seedance
import agent_native  # noqa: E402
import competitor as comp  # noqa: E402
import media_pipeline as media  # noqa: E402
import google_api as api  # noqa: E402

DEFAULT_VIEWS = 'handheld_photo'   # @图片1 when a prompt has no '# views:' line
GEN_DIR = 'generation'             # RUN/generation/{jobs,results}: the APIMART Seedance ledgers

PRODUCT = SKILL/'assets/product/profile.json'
TIMECODE_RE = re.compile(r'\[(\d{2}):(\d{2}):(\d{2}):(\d{2})\]')
SEGMENT_MAX = 14.9   # reference must be strictly < 15 s; one source frame of slack stays under 15
OUTPUT_MIN_SECONDS = 4   # the shortest output the API generates
BREATH_KEEP = 0.15   # seconds of pause kept on each side when an in-shot cut's breath is trimmed in recut


# ---------------------------------------------------------------- state helpers
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S')
def read(p): return json.loads(Path(p).read_text())
def save(p, v): api.save(p, v)
def sha(p): return api.file_sha(p)
def ref(p): p = Path(p).resolve(); return {'path': str(p), 'sha256': sha(p)}


def load(run):
    run = Path(run).resolve(); p = run/'state.json'
    if not p.is_file(): raise SystemExit('run not initialised: ' + str(p))
    return run, read(p)


def update_state(run, fn):
    """Read-modify-write RUN/state.json under a short lock. Stages that run at the same time (watermark scan,
    detect-pro, screen: user 2026-11-19, they used to queue behind one RUN lock) each write only their own record."""
    run = Path(run)
    with api.lock(run/'.state.lock', wait=120, what='RUN state.json'):
        fresh = read(run/'state.json') if (run/'state.json').is_file() else {}
        fn(fresh); save(run/'state.json', fresh)
    return fresh


def commit(run, state, stage=None, **fields):
    if stage:
        rec = {'done_at': now(), **fields}
        state.setdefault('stages', {})[stage] = rec
        def merge(fresh):
            for k, v in state.items():
                if k != 'stages': fresh[k] = v
            fresh.setdefault('stages', {})[stage] = rec
        new = update_state(run, merge)
    else:
        new = update_state(run, lambda fresh: fresh.update(state))
    state.clear(); state.update(new)       # the caller goes on with what is on disk now, other stages included


def script(name, *args, parse=True):
    """Run a sibling CLI (shots/flash_screen/apimart_wan/...) and return its JSON stdout."""
    cmd = [sys.executable, str(HERE/name), *map(str, args)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f'{name} {args[0] if args else ""} failed:\n{result.stderr.strip() or result.stdout.strip()}')
    if not parse: return result.stdout
    text = result.stdout.strip()
    try: return json.loads(text)
    except ValueError:
        # Tolerate progress lines before the final JSON document.
        return json.loads(text[text.rfind('\n{') + 1:]) if '\n{' in text else {}


# ---------------------------------------------------------------- init / prepare
def cmd_init(a):
    run = Path(a.run).resolve(); out = Path(a.out).resolve(); src = Path(a.video).resolve()
    if not src.is_file(): raise SystemExit('video missing: ' + str(src))
    run.mkdir(parents=True, exist_ok=True); out.mkdir(parents=True, exist_ok=True)
    p = run/'state.json'
    if p.is_file():
        state = read(p)
        if state['source_sha256'] != sha(src): raise SystemExit('run already belongs to another video')
        print(json.dumps(state, ensure_ascii=False, indent=2)); return
    info = media.probe(str(src), include_frames=True)
    state = {'schema': 'lymphloop_swap_claude_v1', 'created_at': now(), 'source': str(src), 'source_sha256': sha(src),
             'run_dir': str(run), 'output_dir': str(out), 'probe': {k: info[k] for k in ('fps', 'frame_count', 'video_duration', 'video', 'audio')},
             'product_profile': ref(PRODUCT), 'stages': {}}
    save(p, state); print(json.dumps(state, ensure_ascii=False, indent=2))


def cmd_prepare(a):
    """Integer-CFR working master (normalize if needed). Watermark cleanup is a separate step."""
    run, state = load(a.run)
    info = media.probe(state['source'], include_frames=True)
    cfr_ok = True
    try: media.require_cfr(info)
    except Exception: cfr_ok = False
    rate = media.ratio(info['fps'])
    if cfr_ok and rate.denominator == 1:
        master = Path(state['source']); normalization = None
    else:
        master = run/'master-cfr.mp4'
        if not master.is_file(): media.normalize(state['source'], str(master), a.fps)
        normalization = str(master) + '.normalization.json'
    commit(run, state, 'prepare', cfr_master=ref(master), normalization=normalization, working_master=ref(master))
    print(json.dumps(state['stages']['prepare'], ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- competitor profile (any competitor, user 2026-11-11)
def cmd_competitor(a):
    """Describe the product this ad sells from the film itself (scripts/competitor.py). The agent looks at the anchor
    frames, corrects RUN/detect/competitor.json by hand when anything is wrong or missing, then accepts it; the watermark
    scan, screen, review and final-screen read it from then on."""
    run, state = load(a.run)
    p = comp.path(run)
    if a.accept:
        if not p.is_file(): raise SystemExit('competitor: run swap.py competitor first')
        data = read(p)
        if not data.get('brand_names') or not data.get('container_look'):
            raise SystemExit('competitor: fill in brand_names and container_look before accepting')
        if data.get('form') not in comp.IN_SCOPE and not a.out_of_scope_ok:
            raise SystemExit(f"competitor: form {data.get('form')!r} is not one of the supported forms ({', '.join(sorted(comp.IN_SCOPE))}); "
                             'tell the user before going on (--out-of-scope-ok only on their word)')
        data.update(reviewed=True, review_note=a.note or 'checked against the anchor frames', reviewed_at=now()); save(p, data)
        commit(run, state, 'competitor', profile=ref(p), form=data['form'], brands=data['brand_names'], watermark_target=comp.watermark_target(data))
        print(json.dumps({'accepted': str(p), 'form': data['form'], 'brands': data['brand_names'], 'watermark_target': comp.watermark_target(data),
                          'next': 'swap.py watermark scan'}, ensure_ascii=False, indent=2)); return
    if a.force and p.is_file(): p.unlink()
    if agent_native.is_native() and not p.is_file():
        master = working_master(state)
        overview = agent_native.build_overview_grid(master, run/'agent_review/overview_grid.jpg')
        sents, tr_path = agent_native.cached_full_transcript(run, master)
        voice_samples = agent_native.export_voice_samples(run, master, sents)
        data = {
            'mode': 'antigravity_native',
            'reviewed': False,
            'brand_names': [],
            'product_name': '',
            'form': 'drops',
            'secondary_forms': [],
            'container_look': '',
            'dose_unit_look': '',
            'ingestion_actions': [],
            'hard_cases': [],
            'watermark_text': '',
            'anchor_images': [str(overview)] if overview else [],
            'voice_samples': voice_samples,
            'transcript': str(tr_path),
            'in_scope': True,
            'scope_note': '',
        }
        save(p, data)
        print(json.dumps({
            'native_mode': True,
            'profile': str(p),
            'overview_grid': str(overview) if overview else None,
            'voice_samples': voice_samples,
            'transcript': str(tr_path),
            'sentences': len(sents),
            'next': '用 read_file 看 overview_grid、听 voice_samples/*.mp4、读 transcript.json，把竞品信息与音色记入 competitor.json 后执行 swap.py competitor --accept --note "..."',
        }, ensure_ascii=False, indent=2))
        return
    data = read(p) if p.is_file() else comp.identify(run, working_master(state))
    print(json.dumps({k: data.get(k) for k in ('brand_names', 'product_name', 'form', 'secondary_forms', 'container_look', 'dose_unit_look',
                                               'ingestion_actions', 'hard_cases', 'watermark_text', 'anchor_images', 'in_scope', 'scope_note')}
                     | {'profile': str(p),
                        **({'native_mode': True, 'voice_samples': data.get('voice_samples'), 'transcript': data.get('transcript')} if agent_native.is_native() else {}),
                        'next': 'look at every anchor image, correct the JSON where it is wrong or thin, then swap.py competitor --accept --note "..."'},
                     ensure_ascii=False, indent=2))


def watermark_target(run, explicit=None):
    """The floating watermark word: --target, else the reviewed scan's, else the accepted competitor profile's."""
    if explicit: return explicit.upper()
    scan = run/'watermark/scan.json'
    if scan.is_file() and read(scan).get('target'): return read(scan)['target']
    return comp.watermark_target(comp.active(run))


def watermark_position_review(run, state=None):
    """Agent-reviewed fixed overlays; source-bound so package print stays intact."""
    path = Path(run)/'watermark/agent_positions.json'
    if not path.is_file(): return None
    review = read(path)
    state = state or read(Path(run)/'state.json')
    master = state['stages']['prepare']['cfr_master']
    if not review.get('reviewed') or not review.get('all_candidates_classified') or review.get('source_sha256') != master.get('sha256'):
        raise SystemExit('watermark position review is missing approval or describes a different source master')
    if review.get('width', 0) <= 0 or review.get('height', 0) <= 0:
        raise SystemExit('watermark position review needs the reviewed source dimensions')
    return review


def reviewed_watermark_box(box, review, width, height):
    import watermark_brand as wb
    kx, ky = width / review['width'], height / review['height']
    positions = [{**p, 'box': [p['box'][0] * kx, p['box'][1] * ky, p['box'][2] * kx, p['box'][3] * ky]}
                 for p in review['positions']]
    return wb.position_of(box, positions) is not None


def classify_reviewed_watermarks(data, review, width, height):
    kept = 0
    for d in data.get('detections', []):
        if d.get('classification') == 'floating_overlay' and not reviewed_watermark_box(d['bbox'], review, width, height):
            d['classification'] = 'printed_label_keep'
            d['agent_reason'] = 'not a floating overlay: outside every native-frame reviewed fixed watermark position'
            kept += 1
    data['overlay_classification_reviewed'] = True
    data['reviewer'] = 'Codex'
    data['review_note'] = review.get('note', 'Native-frame semantic review of every candidate watermark position')
    data['agent_non_overlay_boxes_kept'] = kept
    return data


def cmd_watermark(a):
    """scan → (agent reviews scan.json classifications) → apply. Working master becomes the clean file."""
    run, state = load(a.run)
    import watermark_cleanup as wm, watermark_brand as wb
    source = Path(state['stages']['prepare']['cfr_master']['path'])
    wdir = run/'watermark'; wdir.mkdir(exist_ok=True)
    scan = wdir/'scan.json'
    if a.action == 'scan' and not a.target and not comp.load(run):
        raise SystemExit('watermark scan: accept the competitor profile first (swap.py competitor), or pass --target WORD')
    target = a.target.upper() if a.target else (comp.watermark_target(comp.active(run)) if a.action == 'scan' else watermark_target(run))
    if not target and a.action != 'none': raise SystemExit('watermark: no watermark word known; pass --target WORD, or watermark none when there is no watermark')
    target = target or 'NONE'
    pattern = wb.set_target(target)
    if a.action == 'scan':
        info = media.probe(str(source), include_frames=True)
        import watermark_template as wtpl
        tpl = None if (a.scan_json or a.ocr_scan) else wtpl.scan(source, target, wdir/'template')
        if tpl and tpl['scanned_frames'] != int(info['frame_count']):
            print(f"watermark scan: template pass decoded {tpl['scanned_frames']} frames, probe says {info['frame_count']}; OCR scan", file=sys.stderr)
            tpl = None
        if tpl:
            # The stored template of this watermark (user 2026-11-21: "固定位置+有水印才盖"): six fixed places, every frame
            # matched against the mark's strokes, no OCR. Same scan.json shape, so apply / brand / final-screen are unchanged.
            data = tpl
            data.update(type='lymphloop_watermark_scan', target=target, fuzzy_pattern=pattern, source=ref(source), frame_count=info['frame_count'],
                        reviewer='claude', review_note='stored template, every frame', overlay_classification_reviewed=False)
            save(scan, data)
            frames = sorted({d['frame'] for d in data['detections']})
            print(json.dumps({'scan': str(scan), 'method': 'template', 'overlay_frames': len(frames), 'first_frames': frames[:10],
                              'places': [{'box': p['box'], 'orient': p['orient'], 'frames': p['frames'], 'bursts': p['bursts'], 'offset_px': p['offset_px']} for p in data['positions']],
                              'scores': data['template']['places'],
                              'next': 'swap.py watermark apply --note "..."'}, ensure_ascii=False, indent=2))
            return
        if not a.scan_json and not a.ocr_scan and wtpl.available(target):
            print(f'watermark scan: the stored {target} template does not match this film (no place shows the mark); OCR scan', file=sys.stderr)
        if a.scan_json:
            # External OCR (scan_watermark.swift or any tool) already produced detections.
            data = read(a.scan_json)
        else:
            import vision_scan
            binary = vision_scan.watermark_cmd(wdir)       # macOS Vision, or RapidOCR on Windows / Linux
            # Every 4th frame in 4 processes, then every frame around each hit (user 2026-11-19: one process on every
            # frame took 15 min for a 10-minute film; a floating mark stays on screen for seconds and fades are padded).
            data = wb.fast_scan(binary, source, wdir/'scan_parts', sha(source), pattern, target, int(info['frame_count']))
        data.update(type='lymphloop_watermark_scan', target=target, fuzzy_pattern=pattern, source=ref(source), frame_count=info['frame_count'],
                    scanned_frames=data.get('scanned_frames', info['frame_count']), coverage=data.get('coverage', 'every_frame'), reviewer='claude', review_note='pending agent review',
                    overlay_classification_reviewed=False)
        for d in data.get('detections', []):
            # scan_watermark.swift writes classification="unreviewed" on every box; setdefault never replaced it, so
            # nothing was ever marked floating_overlay and apply refused. Classify missing AND unreviewed boxes.
            if d.get('classification') in (None, '', 'unreviewed'):
                d['classification'] = 'floating_overlay' if wb.is_watermark_text(d.get('text', '')) else 'printed_label_keep'
            if d['classification'] == 'floating_overlay' and not d.get('icon_box'):
                d['bbox'] = wb.with_icon(d); d['icon_box'] = True      # the droplet icon is part of the watermark (user, 2026-10-25)
        if not a.no_edge_pass:
            edge = wb.edge_scan(source, wdir/'edge_scan.json', wdir/'edge_work')
            for d in edge['detections']: data['detections'].append({**d, 'classification': 'floating_overlay', 'icon_box': True})
            data['edge_pass'] = {'step': edge['step'], 'variants': edge['variants'], 'detections': len(edge['detections'])}
        v = info['video']; W, H = int(v['width']), int(v['height'])
        for d in data['detections']:      # icon growth and edge-strip mapping can step a pixel past the picture
            x, y, w, h = d['bbox']; x, y = max(0, min(W - 1, x)), max(0, min(H - 1, y)); d['bbox'] = [x, y, max(1, min(W - x, w)), max(1, min(H - y, h))]
        position_review = watermark_position_review(run, state)
        if position_review:
            classify_reviewed_watermarks(data, position_review, W, H)
            data['agent_position_review'] = ref(wdir/'agent_positions.json')
        # the fixed places of the mark (user 2026-11-20: same places all film long), measured from the pixels and given
        # one shared size; then only those places are read on every frame (Codex 30542865505302802: a top-right burst
        # at 265 s that the full-frame and edge passes both missed)
        places = wb.find_places(data['detections'], source, W, H)
        if places and not a.no_place_check:
            # the brand word is matched anywhere, so the competitor's own bottle label is found too (user 2026-11-21):
            # Flash looks at each place; product print and captions stop being watermark candidates
            wb.classify_places(places, source, data['detections'], run)
            all_places = places
            places, n_relabelled = wb.drop_print_places(data['detections'], places)
            data['place_check'] = {'places': [{k: p.get(k) for k in ('id', 'box', 'vertical', 'frames', 'kind', 'kind_reason')} for p in all_places],
                                   'watermark_places': len(places), 'reads_relabelled_as_print': n_relabelled}
        data['positions'] = places
        if places and not a.no_position_pass:
            pos = wb.position_scan(source, places, wdir/'position_scan')
            have = {(d['frame'], tuple(d['bbox'])) for d in data['detections']}
            new = [{**d, 'classification': 'floating_overlay', 'icon_box': True} for d in pos['detections'] if (d['frame'], tuple(d['bbox'])) not in have]
            data['detections'] += new
            data['position_pass'] = {'places': len(places), 'slots': pos['slots'], 'scanned_frames': pos['scanned_frames'],
                                     'hits': len(pos['detections']), 'added': len(new), 'coverage': pos['coverage']}
        ocr_frames = len({d['frame'] for d in data['detections'] if d['classification'] == 'floating_overlay'})
        pad = a.fade_pad if a.fade_pad is not None else fade_pad_frames(info['fps'])
        v = info['video']; added = pad_overlay_fades(data['detections'], info['frame_count'], int(v['width']), int(v['height']), pad, target, cuts=hard_cuts(source))
        data['fade_pad'] = {'frames': pad, 'grow_px': FADE_GROW, 'added_boxes': added,
                            'note': 'OCR cannot read the watermark while it fades in/out; each run of OCR hits is extended by this many frames on both sides and short gaps are bridged (classification floating_overlay, synthetic=fade_pad/gap_fill)'}
        save(scan, data)
        frames = sorted({d['frame'] for d in data['detections'] if d['classification'] == 'floating_overlay'})
        print(json.dumps({'scan': str(scan), 'detections': len(data['detections']), 'overlay_frames': len(frames), 'ocr_overlay_frames': ocr_frames,
                          'fade_pad_boxes': added, 'first_frames': frames[:10],
                          'places': [{'box': p['box'], 'vertical': p['vertical'], 'frames': p['frames'], 'size': p.get('size', 'ocr')} for p in places],
                          'next': 'optional look (shots.py frames ...); then swap.py watermark apply --note "..." (classifications are already set)'}, ensure_ascii=False, indent=2))
        return
    if a.action == 'add-final':
        # --manual A-B:X,Y,W,H adds frames the agent or Flash saw and OCR could not read (2026-11-11, 293 at 52 s: the
        # mark is on screen from the first frame after a hard cut but too faint to read for a second). Master frames.
        manual = []
        for spec in a.manual or []:
            m = re.fullmatch(r'(\d+)-(\d+):(\d+),(\d+),(\d+),(\d+)', spec)
            if not m: raise SystemExit(f'--manual is A-B:X,Y,W,H in master frames and pixels, got {spec!r}')
            f0, f1, *box = map(int, m.groups())
            manual += [{'frame': f, 'bbox': box, 'text': target, 'pass': 'manual'} for f in range(f0, f1 + 1)]
        fs = state.get('stages', {}).get('final_screen') or {}
        left = Path(fs.get('dir', ''))/'watermark/left.json'
        # Explicit measured additions can be registered before the one branding encode.
        # Accept only the exact current recut bytes here; after branding retain the
        # normal screened-master gate and verify that the actual branded bytes match.
        master = Path(state['stages']['recut']['master']['path']) if manual else screened_master(state, run)
        if manual and sha(master) != state['stages']['recut']['master']['sha256']:
            master = screened_master(state, run)
            branded = state.get('stages', {}).get('brand', {}).get('master') or {}
            if str(master) != branded.get('path') or sha(master) != branded.get('sha256'):
                raise SystemExit('watermark add-final: manual additions need the exact current recut or verified branded master')
        current = left.is_file() and sha(master) == fs.get('master_sha256')
        if not manual:
            if not left.is_file(): raise SystemExit('watermark add-final: run swap.py final-screen first')
            if not current: raise SystemExit('watermark add-final: final-screen must match the current master')
        recut_sha = state['stages']['recut']['master']['sha256']
        record = wdir/f'final-overlays-{recut_sha}.json'
        prior = read(record).get('detections', []) if record.is_file() else []
        new = [{**d, 'classification': 'floating_overlay', 'icon_box': True} for d in (read(left)['detections'] if current else []) + manual]
        detections = {(d['frame'], tuple(d['bbox'])): d for d in prior + new if not d.get('synthetic')}
        exact = [d for d in detections.values() if d.get('pass') == 'manual']     # given frame by frame: not padded
        boxes = [d for d in detections.values() if d.get('pass') != 'manual']
        info = media.probe(str(master), include_frames=True)
        v = info['video']
        added = pad_overlay_fades(boxes, info['frame_count'], int(v['width']), int(v['height']), fade_pad_frames(info['fps']), target, cuts=hard_cuts(master)) if boxes else 0
        boxes += exact
        save(record, {'recut_sha256': recut_sha, 'detections': boxes})
        print(json.dumps({'added': len(new), 'fade_pad_boxes': added, 'next': 'swap.py brand, then swap.py final-screen'}, indent=2)); return
    if a.action == 'none':
        # Agent confirmed there is no floating overlay: register an empty, fully covered scan.
        info = media.probe(str(source), include_frames=True)
        save(scan, {'type': 'lymphloop_watermark_scan', 'target': target, 'source': ref(source), 'frame_count': info['frame_count'],
                    'scanned_frames': info['frame_count'], 'coverage': 'every_frame', 'detections': [], 'reviewer': 'claude',
                    'review_note': a.note or 'Agent reviewed the shot sheets: no floating overlay present.', 'overlay_classification_reviewed': True})
    data = read(scan)
    if a.note: data['review_note'] = a.note
    data['overlay_classification_reviewed'] = True; save(scan, data)
    clean = run/'master-clean.mp4'; report = wdir/'cleanup.json'
    if clean.exists() and not a.force: clean.unlink()
    proof = wm.render(str(source), str(scan), str(clean), str(report))
    commit(run, state, 'watermark', scan=ref(scan), cleanup=ref(report), working_master=ref(clean), modified_frames=proof['modified_frames'])
    wm_ref = ref(clean); state['stages']['prepare']['working_master'] = wm_ref
    update_state(run, lambda fresh: fresh['stages']['prepare'].__setitem__('working_master', wm_ref))
    print(json.dumps({'clean_master': str(clean), 'modified_frames': proof['modified_frames'], 'method': proof['method']}, ensure_ascii=False, indent=2))


FADE_PAD = 10     # frames added before/after each run of OCR watermark hits (fade-in/out frames OCR cannot read)
FADE_PAD_S = 0.8  # the default, in seconds (2026-10-25, 305 at 127 s: the mark fades in over ~0.6 s; 10 frames left it visible)


def fade_pad_frames(fps):
    from fractions import Fraction
    return max(FADE_PAD, int(round(FADE_PAD_S * float(Fraction(str(fps))))))   # probe gives '30000/1001'
FADE_GROW = 4     # px the held box grows on each side (the overlay drifts slowly)


def hard_cuts(video, threshold=0.2):
    """First frames of shots, from ffmpeg's scene score (a few seconds for a whole film). 0.2 on purpose: the cut at
    52 s of 293 (same kitchen, new angle) scores under 0.3; a false cut only lets a fade pad run a little longer."""
    from fractions import Fraction
    info = media.probe(str(video)); fps = float(Fraction(str(info['fps'])))
    r = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-i', str(video), '-an', '-vf', f"select='gt(scene,{threshold})',metadata=print:file=-",
                        '-f', 'null', '-'], capture_output=True, text=True)
    return sorted({int(round(float(m) * fps)) for m in re.findall(r'pts_time:([0-9.]+)', r.stdout)})


def pad_overlay_fades(detections, frame_count, width, height, pad=FADE_PAD, target='LYMPHORIA', grow=FADE_GROW, cuts=()):
    """Extend floating_overlay detections in time: OCR reads the watermark only while it is opaque enough, so each
    mark is held `pad` frames earlier and later (its nearest box, grown by `grow` px), and gaps of up to `pad` frames
    are bridged with its previous box. Done per mark (boxes linked by overlap), not per frame: a second mark or a
    caption on screen must not hide this one's fade (305, 127 s). Caption/sign lines naming the brand
    (watermark_brand.caption_line) pop in and out, so they are not padded. `cuts` are the first frames of shots: a mark
    that is already on screen when a shot starts does not fade in, it is there from the cut, faint, for longer than the
    pad (2026-11-11, 293 at 52 s: readable 26 frames after the cut, pad 19, six frames left bare). When a cut lies up to
    one more pad before the padded start, the pad runs back to the cut; likewise forward to the last frame before a cut.
    Appends to `detections`; returns how many."""
    import watermark_brand as wb
    if pad <= 0: return 0
    def grown(b):
        x, y = max(0, b[0] - grow), max(0, b[1] - grow)
        return [x, y, min(width, b[0] + b[2] + grow) - x, min(height, b[1] + b[3] + grow) - y]
    def iou(a, b):
        ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])); iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
        inter = ix * iy; return inter / max(1, a[2] * a[3] + b[2] * b[3] - inter)
    tracks = []                                                  # each: {frame: [bbox, ...]} of one mark's real hits
    for d in sorted((d for d in detections if d.get('classification') == 'floating_overlay' and not d.get('synthetic')
                     and not wb.caption_line(d.get('text', ''))), key=lambda d: d['frame']):
        f, b = d['frame'], d['bbox']
        best = max((t for t in tracks if 0 <= f - max(t) <= pad + 1), key=lambda t: iou(b, t[max(t)][0]), default=None)
        if best is None or iou(grown(b), grown(best[max(best)][0])) <= 0.1: best = {}; tracks.append(best)
        best.setdefault(f, []).append(b)
    taken = {}
    for d in detections:
        if d.get('classification') == 'floating_overlay': taken.setdefault(d['frame'], []).append(d['bbox'])
    n = 0
    for t in tracks:
        hits = sorted(t); add = {}
        for f, nxt in zip(hits, hits[1:]):
            for g in range(f + 1, nxt): add[g] = ('gap_fill', t[f][0])
        start = max(0, hits[0] - pad); end = min(frame_count, hits[-1] + pad + 1)
        back = [c for c in cuts if hits[0] - 2 * pad <= c < start]
        if back: start = max(back)                                   # back to the cut the mark came in with
        ahead = [c for c in cuts if end < c <= hits[-1] + 2 * pad + 1]
        if ahead: end = min(ahead)                                   # up to the cut it leaves with
        for g in range(start, hits[0]): add.setdefault(g, ('fade_pad', t[hits[0]][0]))
        for g in range(hits[-1] + 1, end): add.setdefault(g, ('fade_pad', t[hits[-1]][0]))
        for g, (kind, b) in sorted(add.items()):
            bb = grown(b)
            if any(iou(bb, o) > 0.5 for o in taken.get(g, [])): continue
            detections.append({'frame': g, 'text': target, 'confidence': 0.0, 'bbox': bb, 'classification': 'floating_overlay', 'synthetic': kind})
            taken.setdefault(g, []).append(bb); n += 1
    return n


def working_master(state):
    return Path(state['stages']['prepare']['working_master']['path'])


# ---------------------------------------------------------------- shots / detection
def cmd_shots(a):
    run, state = load(a.run)
    script('shots.py', 'detect', working_master(state), '--out', run/'shots', '--threshold', a.threshold,
           '--min-shot-seconds', a.min_shot_seconds, '--fps-sample', a.fps_sample)
    data = read(run/'shots/shots.json')
    commit(run, state, 'shots', shots_json=ref(run/'shots/shots.json'), count=len(data['shots']))
    print(json.dumps({'shots': len(data['shots']), 'sheets': str(run/'shots/sheets'), 'json': str(run/'shots/shots.json')}, ensure_ascii=False, indent=2))


def tc_to_frame(h, m, s, f, fps): return ((int(h)*60 + int(m))*60 + int(s))*fps + int(f)


def parse_pro_markdown(text, fps):
    """Turn the fixed-contract Markdown into nodes: [{in_frame,out_frame,scene,vo,dialogue,visual}]."""
    nodes = []
    blocks = re.split(r'\n(?=#{2,4}\s*⏱️?\s*Timecode)', text)
    for block in blocks:
        tcs = TIMECODE_RE.findall(block)
        if len(tcs) < 2: continue
        node = {'in_frame': tc_to_frame(*tcs[0], fps), 'out_frame': tc_to_frame(*tcs[1], fps) + 1, 'raw': block.strip()}
        def field(label):
            m = re.search(label + r'[^\n]*\n(?:[^\n]*\n){0,3}?[^\n]*原词\*{0,2}:\s*\**\s*(.+?)\s*\n[^\n]*改写为\*{0,2}:\s*\**\s*(.+?)\s*(?:\n|$)', block)
            if not m: return None
            orig, new = m.group(1).strip(' *`'), m.group(2).strip(' *`')
            return None if orig.lower() in ('none', '无', '[none]') else {'original': orig, 'rewrite': new}
        node['vo'] = field('VO冲突'); node['dialogue'] = field('Dialogue冲突')
        m = re.search(r'冲突元素\*{0,2}:\s*\**\s*(.+?)\s*(?:\n|$)', block)
        visual = m.group(1).strip(' *`') if m else None
        node['visual'] = None if not visual or visual.lower() in ('none', '无', '[none]') else visual
        m = re.search(r'场景简述\*{0,2}:\s*\**\s*(.+?)\s*(?:\n|$)', block); node['scene'] = m.group(1).strip(' *`') if m else ''
        nodes.append(node)
    return nodes


def cmd_detect_pro(a):
    """One fixed-contract Gemini 3.1 Pro pass over the timecoded working master (or Antigravity native see+hear)."""
    run, state = load(a.run)
    master = working_master(state); tdir = run/'detect'; tdir.mkdir(exist_ok=True)
    fps = int(media.ratio(media.probe(str(master))['fps']))
    if getattr(a, 'import_nodes', None):
        raw_in = read(a.import_nodes)
        nodes = raw_in['nodes'] if isinstance(raw_in, dict) else list(raw_in)
        save(tdir/'pro_nodes.json', {'fps': fps, 'source': 'antigravity_native', 'nodes': nodes})
        commit(run, state, 'detect_pro', source='antigravity_native', nodes=len(nodes))
        print(json.dumps({'native_mode': True, 'nodes': len(nodes), 'parsed': str(tdir/'pro_nodes.json')}, ensure_ascii=False, indent=2))
        return
    if agent_native.is_native():
        sents, tr_path = agent_native.cached_full_transcript(run, master)
        sheets = sorted((run/'shots/sheets').glob('S*.png')) if (run/'shots/sheets').is_dir() else []
        grids = agent_native.tile_sheets(sheets, run/'agent_review/shot_grids', prefix='shot_grid') if sheets else []
        print(json.dumps({
            'native_mode': True,
            'fps': fps,
            'frame_count': state['probe']['frame_count'],
            'transcript': str(tr_path),
            'sentences': len(sents),
            'shot_grids': grids,
            'clips_dir': str(run/'clips'),
            'output_path': str(tdir/'pro_nodes.json'),
            'next': '用 read_file 读 transcript.json 并逐批看 shot_grids/*.jpg（有疑点的短镜直接 read_file 看 clips/S###.mp4），写出 pro_nodes.json 后跑 swap.py detect-pro --run RUN --import-nodes RUN/detect/pro_nodes.json',
        }, ensure_ascii=False, indent=2))
        return
    burned = tdir/'timecoded.mp4'
    if not burned.is_file(): media.burn_timecode(str(master), str(burned))
    profile = read(PRODUCT)
    images = []
    for view in profile['analysis_default_reference_views']:
        p = SKILL/profile['reference_views'][view]; images.append({**api.file_description(str(p)), 'role': 'product_reference', 'view': view})
    scope = f'本次分析视频工作母版帧率 {fps} fps，总帧数 {state["probe"]["frame_count"]}；时间码从 [00:00:00:00] 开始覆盖整片。'
    cp = comp.load(run)
    if cp:
        # The fixed contract names the dropper bottle only as an example; this run's competitor comes from its profile.
        scope += (f"\n本条视频的竞品（以此为准，原始契约里的「棕色玻璃瓶、滴管」只是举例）：品牌 {'/'.join(cp.get('brand_names') or [])}，"
                  f"剂型 {cp.get('form')}；容器：{cp.get('container_look')}；剂型单元：{cp.get('dose_unit_look')}。"
                  "竞品容器和任何剂型单元（胶囊、软胶囊、片剂、软糖、粉末、粉勺、冲好的饮料，哪怕散放不连包装）露出都报 Visual 冲突；"
                  "台词里的竞品名、剂型词（capsule、softgel、pill、gummy、powder、scoop 等）和用量用法都报 VO 或 Dialogue 冲突并给最小改写"
                  "（目标是条包 packet，直接饮用）。"
                  # user, 2026-11-11: an ad from another health category (cinnamon / blood sugar) gets every element that
                  # contradicts LymphLoop changed, whether or not the rewritten mechanism still makes sense
                  "原片若不是淋巴排毒品类：凡与 yesnap LymphLoop 矛盾的元素都报冲突并给改写——点名的、LymphLoop 没有的成分改成它自己的成分"
                  "（Cleavers、Burdock Root、Dandelion、Bromelain、Echinacea、Calendula、Red Clover），对应的成分画面报 Visual，"
                  "别的品类的功效说法（如 blood sugar）改成淋巴排毒的说法；改后的机制是否讲得通不用管。")
    spec = {'kind': 'analyze', 'model': api.MODELS['analyze'], 'prompt': scope, 'sampling_fps': a.fps,
            'video': {'path': str(burned), 'sha256': sha(burned), 'mime_type': 'video/mp4'}, 'images': images}
    raw = tdir/'pro_raw.md'
    result = api.execute(spec, str(run/'google'), str(raw), wait_seconds=a.wait)
    if result.get('status') != 'completed':
        raise SystemExit('Pro detection not completed: ' + json.dumps(api.public_state(result), ensure_ascii=False))
    nodes = parse_pro_markdown(raw.read_text(), fps)
    save(tdir/'pro_nodes.json', {'fps': fps, 'raw': ref(raw), 'nodes': nodes})
    commit(run, state, 'detect_pro', raw=ref(raw), nodes=len(nodes), request=result.get('request_dir'))
    print(json.dumps({'nodes': len(nodes), 'raw': str(raw), 'parsed': str(tdir/'pro_nodes.json'),
                      'summary': [{k: n[k] for k in ('in_frame', 'out_frame', 'scene')} | {'vo': bool(n['vo']), 'dialogue': bool(n['dialogue']), 'visual': bool(n['visual'])} for n in nodes]},
                     ensure_ascii=False, indent=2))


def cmd_screen(a):
    run, state = load(a.run)
    if not comp.load(run): raise SystemExit('screen: accept the competitor profile first (swap.py competitor, then --accept)')
    if getattr(a, 'import_verdicts', None):
        shots = read(run/'shots/shots.json')['shots']
        raw = read(a.import_verdicts)
        replace_set = set(raw.get('replace_ids') or [])
        unsure_set = set(raw.get('unsure_ids') or [])
        notes = raw.get('notes') or {}
        sdir = run/'screen'; sdir.mkdir(exist_ok=True)
        rows = []
        for s in shots:
            sid = s['id']
            verdict = 'replace' if sid in replace_set else ('unsure' if sid in unsure_set else 'keep')
            row = {'id': sid, 'verdict': verdict, 'source': 'antigravity_native',
                   **({'reason': notes[sid]} if sid in notes else {})}
            save(sdir/f'{sid}.json', row)
            rows.append(row)
        summary = {
            'source': 'antigravity_native',
            'replace_ids': [r['id'] for r in rows if r['verdict'] == 'replace'],
            'unsure_ids': [r['id'] for r in rows if r['verdict'] == 'unsure'],
            'error_ids': [],
            'shots': rows,
        }
        save(sdir/'summary.json', summary)
        commit(run, state, 'screen', summary=ref(sdir/'summary.json'), replace=len(summary['replace_ids']), unsure=len(summary['unsure_ids']))
        print(json.dumps({'native_mode': True, 'replace_ids': summary['replace_ids'], 'unsure_ids': summary['unsure_ids'],
                          'error_ids': [], 'summary': str(sdir/'summary.json')}, ensure_ascii=False, indent=2))
        return
    if agent_native.is_native():
        wanted = set(a.ids.split(',')) if a.ids else None
        sheets = [p for p in sorted((run/'shots/sheets').glob('S*.png')) if wanted is None or p.stem in wanted]
        grids = agent_native.tile_sheets(sheets, run/'agent_review/shot_grids', prefix='shot_grid') if sheets else []
        print(json.dumps({
            'native_mode': True,
            'shot_count': len(sheets),
            'shot_grids': grids,
            'clips_dir': str(run/'clips'),
            'next': '用 read_file 逐批看 shot_grids/*.jpg（有疑点的镜头直接 read_file 看 clips/S###.mp4），写出 {"replace_ids": [...], "unsure_ids": [...], "notes": {...}} 后执行 swap.py screen --run RUN --import-verdicts FILE',
        }, ensure_ascii=False, indent=2))
        return
    args = ['screen', '--shots', run/'shots/shots.json', '--run', run, '--clip-dir', run/'clips']
    if a.ids: args += ['--ids', a.ids]
    script('flash_screen.py', *args); summary = read(run/'screen/summary.json')
    commit(run, state, 'screen', summary=ref(run/'screen/summary.json'), replace=len(summary.get('replace_ids', [])), unsure=len(summary.get('unsure_ids', [])))
    print(json.dumps({'replace_ids': summary.get('replace_ids'), 'unsure_ids': summary.get('unsure_ids'), 'error_ids': summary.get('error_ids', []),
                      'summary': str(run/'screen/summary.json')}, ensure_ascii=False, indent=2))


def overlaps(a0, a1, b0, b1): return a0 < b1 and b0 < a1


COUNT_WORDS = r'(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty)'
HERB_COUNT = re.compile(r'\b(' + COUNT_WORDS + r')(\s+(?:powerful\s+|natural\s+|plant\s+)?(?:herbs?|ingredients?|botanicals?|plants?)\b)', re.I)


def keep_herb_count(original, rewrite):
    """User rule 2026-10-24: "four herbs" / "twelve herbs" is NOT a conflict. Only a specific named ingredient that is not
    in the product (e.g. Stillingia, Prickly ash) is rewritten. Pro sometimes also changes the count ("Four herbs" ->
    "Twelve herbs"); put the original count back, keeping the rewrite's own capitalisation."""
    counts = [m.group(1) for m in HERB_COUNT.finditer(original)]
    if not counts: return rewrite
    it = iter(counts)
    def back(m):
        c = next(it, m.group(1))
        return (c[:1].upper() + c[1:] if m.group(1)[:1].isupper() else c.lower()) + m.group(2)
    return HERB_COUNT.sub(back, rewrite)


def only_real_conflicts(node):
    """Drop a Pro text rewrite that is no longer a change once the herb count is restored."""
    norm = lambda t: re.sub(r'[^a-z0-9]+', ' ', t.lower()).strip()
    out = dict(node)
    for k in ('dialogue', 'vo'):
        rw = node.get(k)
        if not rw: continue
        fixed = keep_herb_count(rw['original'], rw['rewrite'])
        out[k] = None if norm(fixed) == norm(rw['original']) else {**rw, 'rewrite': fixed}
    return out


def cmd_merge(a):
    """Union of Pro nodes, Flash verdicts and agent overrides → per-shot decision list (recall-biased)."""
    run, state = load(a.run)
    shots = read(run/'shots/shots.json')['shots']
    pro = read(run/'detect/pro_nodes.json')['nodes'] if (run/'detect/pro_nodes.json').is_file() else []
    flash = {s['id']: s for s in read(run/'screen/summary.json')['shots']} if (run/'screen/summary.json').is_file() else {}
    overrides = read(run/'detect/overrides.json') if (run/'detect/overrides.json').is_file() else {}
    # Rewrite fix: {"S052": {"rewrites": {"dialogue": [], "vo": [{"original": "...", "rewrite": "..."}]}, "note": "..."}} replaces
    # Pro's rewrites for that shot (e.g. a generic claim Pro reworded that is not a real conflict).
    # Manual splits: {"S103": {"split_frames": [7917], "pieces": ["replace", "keep"], "kinds": ["visual"], "note": "..."}}
    expanded = []
    for s in shots:
        ov = overrides.get(s['id'], {})
        if ov.get('split_frames'):
            bounds = [s['in_frame']] + sorted(ov['split_frames']) + [s['out_frame']]
            fps = s['out_frame'] - s['in_frame']; fps = (s['out_frame'] - s['in_frame'])/max(s['duration_s'], 1e-6)
            for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
                pid = f"{s['id']}{chr(97+i)}"
                expanded.append({**s, 'id': pid, 'in_frame': a, 'out_frame': b, 'start_s': a/fps, 'end_s': b/fps, 'duration_s': (b-a)/fps, 'parent': s['id']})
                # a piece may carry its own override (kinds, rewrites) under its id S###a/b/...: it wins over the defaults
                overrides[pid] = {'decision': ov['pieces'][i], 'kinds': ov.get('kinds', ['visual']), 'note': f"split of {s['id']} ({ov.get('note', '')})",
                                  **overrides.get(pid, {})}
        else:
            expanded.append(s)
    shots = expanded
    pro = [only_real_conflicts(n) for n in pro]
    decisions = []
    for s in shots:
        hits = [n for n in pro if overlaps(s['in_frame'], s['out_frame'], n['in_frame'], n['out_frame'])]
        reasons = []; kind = set()
        for n in hits:
            if n['visual']: reasons.append('pro:visual:' + n['visual']); kind.add('visual')
            if n['dialogue']: reasons.append('pro:dialogue:' + n['dialogue']['original']); kind.add('dialogue')
            if n['vo']: reasons.append('pro:vo:' + n['vo']['original']); kind.add('vo')
        fv = flash.get(s.get('parent', s['id']))
        if fv and fv.get('error'): reasons.append('flash-error:' + fv['error'][:200])
        if fv and not s.get('parent'):
            # Re-derive the verdict from the raw answer so rule changes apply without re-screening.
            import flash_screen
            verdict, freasons = flash_screen.verdict_for(fv.get('raw', {}), run)
            if verdict == 'replace':
                reasons += ['flash:' + r for r in freasons]; kind.add('visual')
                raw = fv.get('raw', {})
                if raw.get('speech_mentions_product') in ('yes', 'unsure'): kind.add('speech_unclassified')
            else:
                reasons += ['flash-note:' + r for r in freasons]
        ov = overrides.get(s['id'])
        if ov:
            reasons.append('agent:' + ov.get('note', ''))
            if ov.get('decision') == 'keep': kind = set(); reasons = ['agent:keep:' + ov.get('note', '')]
            else: kind.update(ov.get('kinds', ['visual']))
        decision = 'replace' if kind else 'keep'
        decisions.append({**{k: s[k] for k in ('id', 'in_frame', 'out_frame', 'start_s', 'end_s', 'duration_s')}, 'decision': decision,
                          'kinds': sorted(kind), 'reasons': reasons,
                          'rewrites': ov['rewrites'] if ov and 'rewrites' in ov else {'dialogue': [n['dialogue'] for n in hits if n['dialogue']], 'vo': [n['vo'] for n in hits if n['vo']]}})
    out = run/'plan/decisions.json'; save(out, {'shots': decisions, 'replace_ids': [d['id'] for d in decisions if d['decision'] == 'replace']})
    commit(run, state, 'merge', decisions=ref(out), replace=sum(d['decision'] == 'replace' for d in decisions))
    print(json.dumps({'replace': [d['id'] for d in decisions if d['decision'] == 'replace'], 'file': str(out)}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- plan / generate
def needs_edit(d):
    """A shot is regenerated when it shows the product OR carries a line to reword (brand, dosage, ingredient, narration).
    No TTS (user rule, 2026-10-24): a reworded line is re-voiced by Seedance native edit from the original, so the voice
    matches; a product-free shot with only a narration change keeps its source picture (select --audio-only)."""
    return d['decision'] == 'replace' or bool(d['rewrites']['dialogue'] or d['rewrites']['vo'])


def cmd_plan(a):
    """Plan by LOCKS (user rule, 2026-10-24): scenes.py labels every cut 'paragraph' (a completely different
    environment), 'shot' (same place, no continuity) or 'none'. Each block between two locks that holds a product shot
    is regenerated whole (speech_map.lock_blocks), then cut under the 15 s ceiling at beats / semantic breaks
    (segment_run); a piece is generated only when it needs_edit (a product shot or a line to reword), others are
    skipped. --runs-only keeps the old rule (consecutive replace shots only) for comparison. Writes segments.json and
    the prompt stubs; reference clips are cut later by generate."""
    import speech_map
    run, state = load(a.run)
    dec = read(run/'plan/decisions.json')['shots']; master = working_master(state)
    fps = int(media.ratio(state['probe']['fps'])) if media.ratio(state['probe']['fps']).denominator == 1 else int(media.ratio(media.probe(str(master))['fps']))
    runs, scene_info, block_notes, quiet = [], {}, {}, set()
    if a.runs_only:
        cur = []
        for d in dec:
            if d['decision'] != 'replace':
                if cur: runs.append((cur, None)); cur = []
                continue
            cur.append(d)
        if cur: runs.append((cur, None))
    else:
        import scenes
        cuts = {} if a.no_scenes else scenes.cut_levels(run, master, dec, fps)
        locks = {k: c['level'] for k, c in cuts.items() if c['level'] in ('paragraph', 'shot')}
        quiet = scenes.quiet_cuts(scenes._rms_track(master), [d['in_frame']/fps for d in dec[1:]])
        speech_cuts = run/'plan/speech_cut_overrides.json'
        if speech_cuts.is_file():
            # A reviewed in-sentence pause (e.g. It's called | BRAND) is not
            # permission to duplicate the full line in adjacent native clips.
            unsafe = set(read(speech_cuts).get('unsafe_frames') or [])
            quiet = {q for q in quiet if int(round(q*fps)) not in unsafe}
        scene_info = {k: {'level': c['level'], 'shot': c['shot'], 'reason': c.get('reason'), 'location_next': c.get('location_next')}
                      for k, c in cuts.items() if k in locks}
        all_spans = [(d['in_frame']/fps, d['out_frame']/fps) for d in dec]
        edges = [0] + sorted(k for k, v in locks.items() if v == 'paragraph') + [len(dec)]
        for x, y in zip(edges, edges[1:]):     # one paragraph (one environment) at a time, transcribed once
            if not any(needs_edit(d) for d in dec[x:y]): continue
            sents = [] if a.no_speech else speech_map.transcribe_chunked(run, master, all_spans[x][0], all_spans[y - 1][1], [sp[0] for sp in all_spans[x:y]])
            inner = {k - x: v for k, v in locks.items() if x < k < y}
            for i, j, notes in speech_map.lock_blocks(all_spans[x:y], [needs_edit(d) for d in dec[x:y]], inner, sents, quiet=quiet):
                runs.append((dec[x + i:x + j], [z for z in sents if z['end'] > all_spans[x + i][0] - 0.5 and z['start'] < all_spans[x + j - 1][1] + 0.5]))
                if notes: block_notes[dec[x + i]['id']] = notes
    segments, skipped = [], []
    for shots, run_sentences in runs:
        spans = [(d['in_frame']/fps, d['out_frame']/fps) for d in shots]
        sentences = run_sentences if run_sentences is not None else ([] if a.no_speech else speech_map.transcribe(run, master, spans[0][0], spans[-1][1]))
        # A single shot longer than the ceiling is pre-split at a sentence pause (see speech_map.split_long_shots).
        long_spans = [(a0, b0) for a0, b0 in spans if b0 - a0 > SEGMENT_MAX]
        transitions = [x for a0, b0 in long_spans for x in scene_scores(working_master(state), a0, b0)]
        pieces, origin, vcuts = speech_map.split_long_shots(spans, sentences, SEGMENT_MAX, transitions=transitions)
        if len(pieces) != len(spans):
            split = []
            for k, ((x, y), o) in enumerate(zip(pieces, origin)):
                parent = shots[o]; n_parts = origin.count(o)
                if n_parts == 1: split.append(parent); continue
                part = origin[:k].count(o) + 1
                split.append({**parent, 'id': f"{parent['id']}.{part}", 'parent_shot': parent['id'],
                              'in_frame': parent['in_frame'] if part == 1 else int(round(x * fps)),
                              'out_frame': parent['out_frame'] if part == n_parts else int(round(y * fps))})
            shots = split; spans = [(d['in_frame']/fps, d['out_frame']/fps) for d in shots]
        run_start = len(segments)
        groups, notes = speech_map.plan_run(run, spans, sentences, max_s=SEGMENT_MAX, virtual=set(vcuts), quiet={round(q, 6) for q in quiet} | {sp[0] for sp in spans if any(abs(sp[0] - q) < 1e-6 for q in quiet)})
        for (i, j), note in zip(groups, notes):
            group = shots[i:j]
            if not a.runs_only and not any(needs_edit(g) for g in group):
                skipped.append({'shots': [g['id'] for g in group], 'reason': 'no product shot and no line to reword: pulled in only to keep a sentence whole; the original plays here'})
                continue
            in_f, out_f = group[0]['in_frame'], group[-1]['out_frame']; dur = (out_f - in_f)/fps
            segments.append({'id': f'G{len(segments)+1:02d}', 'shot_ids': [g['id'] for g in group], 'in_frame': in_f, 'out_frame': out_f,
                             'duration_s': round(dur, 4), 'kinds': sorted({k for g in group for k in g['kinds']}), 'output_seconds': output_seconds(dur),
                             'rewrites': {'dialogue': [r for g in group for r in g['rewrites']['dialogue']], 'vo': [r for g in group for r in g['rewrites']['vo']]},
                             'reasons': [r for g in group for r in g['reasons']],
                             'speech': [{**s, 'start': round(s['start'] - in_f/fps, 3), 'end': round(s['end'] - in_f/fps, 3)} for s in note.get('sentences', [])],
                             'cut_in': note.get('cut_in'), **({'warning': note['warning']} if note.get('warning') else {}),
                             'shot_times': [[g['id'], round((g['in_frame'] - in_f)/fps, 2), round((g['out_frame'] - in_f)/fps, 2)] for g in group],
                             'product_shots': [g['id'] for g in group if g['decision'] == 'replace'],
                             **({'lock_in': scene_info[k]} if (k := next((q for q in scene_info if dec[q]['id'] == group[0]['id'].split('.')[0]), None)) is not None else {}),
                             **({'block_notes': block_notes[group[0]['id']]} if group[0]['id'] in block_notes else {})})
            seg = segments[-1]
            for kind in ('dialogue', 'vo'):
                kept, dropped = segment_rewrites(seg['rewrites'][kind], seg['speech'])
                seg['rewrites'][kind] = kept
                if dropped: seg.setdefault('rewrites_not_spoken_here', []).extend(dropped)
            if i in vcuts and i > 0:
                # In-shot cut: both neighbours are generated; offer a trim hint for the original pause. Select against
                # the generated performance before using it.
                info = vcuts[i]; prev = segments[-2] if len(segments) - 1 > run_start and segments[-2]['out_frame'] == in_f else None
                cut_s = in_f / fps; seg = segments[-1]
                seg['in_shot_cut_in'] = {'kind': info['kind'], 'pause': info['pause']}
                if prev is not None: prev['in_shot_cut_out'] = {'kind': info['kind'], 'pause': info['pause']}
                if info['pause']:
                    p, q = info['pause']
                    if prev is not None and generated_audio(prev):
                        prev.setdefault('trim', {})['tail_s'] = round(max(0.0, cut_s - (p + BREATH_KEEP)), 3)
                    if generated_audio(seg):
                        seg.setdefault('trim', {})['head_s'] = round(max(0.0, (q - BREATH_KEEP) - cut_s), 3)
                else:
                    seg['warning'] = 'in-shot cut without a usable pause (cut evenly): check the seam'
    snaps = []
    if segments and not getattr(a, 'no_edge_snap', False):
        # User 2026-11-21 ("杜绝滴剂瓶闪回"): shots.py folds pieces under 0.4 s into a neighbour, so an edge could sit a
        # few frames off the real cut and leave a sliver of the competitor's shot in the original. Edges go onto
        # frame-exact cuts and short pieces next to an edge are pulled into the segment (cutscan.snap_edges).
        import cutscan
        raw = cutscan.cuts(master, cache=run/'plan/raw_cuts.json')
        snaps = cutscan.snap_edges(segments, raw['cuts'], fps, raw['frames'], max_s=SEGMENT_MAX)
        for seg in segments:
            seg['output_seconds'] = output_seconds(seg['duration_s'])
            if any('check' in m for m in seg.get('edge_snap', [])):
                seg['warning'] = '; '.join(x for x in (seg.get('warning'), 'edge snap: ' + ' | '.join(m for m in seg['edge_snap'] if 'check' in m)) if x)
        if snaps: print(json.dumps({'edge_snap': snaps}, ensure_ascii=False), file=sys.stderr)
    if a.dry_run:
        print(json.dumps([{'id': s['id'], 'shots': f"{s['shot_ids'][0]}-{s['shot_ids'][-1]}", 'source_s': [round(s['in_frame']/fps, 2), round(s['out_frame']/fps, 2)],
                           'duration_s': round(s['duration_s'], 2), 'product_shots': s['product_shots'], 'lock_in': (s.get('lock_in') or {}).get('level'), 'block_notes': s.get('block_notes'), 'cut_in': s['cut_in'], 'warning': s.get('warning'), 'edge_snap': s.get('edge_snap'), 'trim': s.get('trim'),
                           'speech': [f"{x['start']:.1f}-{x['end']:.1f} {x['speaker']}: {x['text']}" for x in s['speech']]} for s in segments], ensure_ascii=False, indent=1))
        if skipped: print(json.dumps({'skipped_product_free': skipped}, ensure_ascii=False), file=sys.stderr)
        return
    (run/'clips').mkdir(exist_ok=True); (run/'prompts').mkdir(exist_ok=True)
    for seg in segments:
        if seg['duration_s'] >= 15.0: seg['warning'] = 'segment is 15 s or longer: split at a suitable boundary'
        rng = run/'prompts'/(seg['id'] + '.range.json'); key = {'in_frame': seg['in_frame'], 'out_frame': seg['out_frame']}
        if rng.is_file() and read(rng) != key:
            # segment numbering shifted (new overrides/merge): never reuse an unauthored stub written for another range
            stale = run/'prompts'/(seg['id'] + '-a1.txt')
            if stale.is_file() and stale.read_text()[:2].startswith('#'): stale.unlink()
        save(rng, key)
        stub = run/'prompts'/(seg['id'] + '-a1.txt')
        if stub_untouched(run, seg['id']):
            design = (read(run/'plan/swap_design.json')['shots'] if (run/'plan/swap_design.json').is_file() else None)
            write_stub(run, seg, prompt_stub(seg, comp.active(run), design))
        seg['prompt_file'] = str(stub)
    save(run/'plan/segments.json', {'fps': fps, 'segments': segments, 'skipped_product_free': skipped, 'locks': {str(k): v for k, v in scene_info.items()}})
    commit(run, state, 'plan', segments=ref(run/'plan/segments.json'), count=len(segments))
    print(json.dumps({'segments': [{k: s[k] for k in ('id', 'shot_ids', 'duration_s', 'output_seconds', 'kinds')} | ({'warning': s['warning']} if 'warning' in s else {}) for s in segments],
                      'prompts_dir': str(run/'prompts'), 'next': 'author each prompt file from the shot sheets, then swap.py generate --prepare-only'}, ensure_ascii=False, indent=2))


def _fold(text):
    import unicodedata
    t = unicodedata.normalize('NFKD', str(text or '').lower())
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', ''.join(c for c in t if not unicodedata.combining(c)))).strip()


def segment_rewrites(rewrites, speech):
    """(kept, dropped): the rewrites of a segment whose original words are actually spoken in it.
    2026-11-20 film 2210: plan split one 30 s shot into three segments and each inherited all of the shot's rewrites,
    so segments without the word would have needed a mute window for nothing. Pro also joins two lines with " / "
    ("esto es Lymphoria / un gotero completo ..."), which never matches a line: such pairs are split first.
    A segment without a transcript keeps everything."""
    out = []
    for r in rewrites:
        o, w = str(r.get('original') or ''), str(r.get('rewrite') or '')
        if ' / ' in o and o.count(' / ') == w.count(' / '):
            out += [{**r, 'original': a.strip(), 'rewrite': b.strip()} for a, b in zip(o.split(' / '), w.split(' / '))]
        else:
            out.append(r)
    seen, uniq = set(), []
    for r in out:
        k = (_fold(r.get('original')), r.get('rewrite'))
        if k not in seen: seen.add(k); uniq.append(r)
    spoken = ' '.join(_fold(l.get('text')) for l in speech or [])
    if not spoken.strip(): return uniq, []
    keep = [r for r in uniq if rewrite_heard(r.get('original'), r.get('rewrite'), spoken)]
    return keep, [r for r in uniq if r not in keep]


HEARD_WORD = 0.7      # difflib ratio of a heard word to the written one ("linforia" for Lymphoria, "fresno" for fresco)
HEARD_SHARE = 0.5     # share of the original's words heard in the segment
HEARD_PART = 3        # or this many of its words, the changed one among them (a line split between two segments)
HEARD_JOINED = 0.85   # stricter for two heard words read as one ("still india" for Stillingia)


def rewrite_heard(original, rewrite, spoken):
    """Whether a rewrite's original line is spoken in a segment's transcript (folded text). Not an exact match: Pro and
    Whisper spell differently (2026-11-20 film 1753: "Lymphoria" / "Linforia", "fresco" / "fresno", "5 frascos" /
    "cinco frascos"; all four rewrites were dropped and their segments would have kept the old words). The words that
    the rewrite CHANGES must be heard (one of them, fuzzily) and at least half of the line's words, numbers aside; a line
    that runs over two segments stays only where its changed words are said ("...cinco frascos tiene un 70 por" /
    "un 70% de descuento y una garantia")."""
    import difflib
    words = [w for w in _fold(original).split() if not w.isdigit()]
    # content words only: "por la ... y ... de" are in every Spanish line (film 1753: "Medio gotero por la mañana y por la
    # noche" was kept in the segment before it on its short words alone)
    words = [w for w in words if len(w) >= 4] or words
    new = set(_fold(rewrite).split())
    heard = spoken.split()
    if not words: return False
    def sound(w):        # spellings that sound alike: Lymphoria / Linforia
        return re.sub(r'm(?=[fbp])', 'n', w.replace('ph', 'f').replace('y', 'i'))
    # a name split in two by the transcript (2026-11-22 film 283: "Stillingia" heard as "Still India"): adjacent
    # words joined count as one heard word too
    heard_s = [sound(h) for h in heard]
    joined = [sound(a + b) for a, b in zip(heard, heard[1:])]
    def said(w):
        ws = sound(w)
        return (w in heard or any(difflib.SequenceMatcher(None, ws, h).ratio() >= HEARD_WORD for h in heard_s if abs(len(h) - len(ws)) <= 3)
                or any(difflib.SequenceMatcher(None, ws, h).ratio() >= HEARD_JOINED for h in joined if abs(len(h) - len(ws)) <= 2))
    changed = [w for w in words if w not in new] or words
    # a line cut by a shot change is heard only in part (2026-11-22 film 2836804316692183 G15: "Still, India breaks down
    # that hard..."): with the changed word heard, three of its words are enough
    n_said = sum(map(said, words))
    return any(said(w) for w in changed) and (n_said >= HEARD_SHARE * len(words) or n_said >= HEARD_PART)


def scene_scores(video, a, b, threshold=0.05):
    """[(time, scene score)] of picture changes above `threshold` between a and b seconds (dissolves, camera moves)."""
    r = subprocess.run([media.binary('ffmpeg'), '-hide_banner', '-ss', f'{a:.3f}', '-t', f'{b - a:.3f}', '-i', str(video), '-vf',
                        f"select='gt(scene,{threshold})',metadata=print", '-an', '-f', 'null', '-'], capture_output=True, text=True)
    ts = [float(x) for x in re.findall(r'pts_time:([0-9.]+)', r.stderr)]
    sc = [float(x) for x in re.findall(r'scene_score=([0-9.]+)', r.stderr)]
    return [(a + t, s) for t, s in zip(ts, sc)]


def whole(seconds):
    return int(math.ceil(float(seconds) - 1e-9))


def prompt_stub(seg, competitor_profile=None, design=None):
    """Five-section skeleton of a segment (user 2026-11-20: "统一成五段式"; the structure of the Gemini
    clone-seedance-video prompts that worked first time on G05/G16, see references/rules-now.md):
    生成目标 / 参考素材职责 / 主体与关系 / 事件脚本 (one stage per reference shot, each ending 结束时…) / 保持一致.
    `design` = RUN/plan/swap_design.json shots: product shots with a design row come with their form, images, count,
    removals and action filled in (the rule table decides, the agent does not). Without it (right after `plan`) the
    product parts are 【写】 slots and `swap_design.py decide` rewrites the skeleton. The agent fills the 【写…】 slots;
    '#' lines are dropped before submission. No timestamps, ratio or other parameters in the body."""
    import swap_design as sd
    cp = competitor_profile or comp.fallback()
    rival = f"{'/'.join(cp.get('brand_names') or ['竞品'])}（{cp.get('form')}：{cp.get('container_look')}；剂型单元：{cp.get('dose_unit_look')}）"
    shots = seg.get('shot_times') or []
    # a line that belongs to the neighbouring segment (its middle falls outside this one) is listed there, not here
    speech = [l for l in (seg.get('speech') or []) if l.get('text') and 0 <= (float(l['start']) + float(l['end'])) / 2 <= float(seg['duration_s'])]
    rew = seg['rewrites']['dialogue'] + seg['rewrites']['vo']
    product = set(seg.get('product_shots', [s[0] for s in shots]))
    design = design or {}

    # Every line of the segment is written out and generated again with its lip-sync (user, 2026-11-11: "模型应该要完全
    # 重新生成替换段的台词和口型同步"). The replaced original words never appear in the prompt body.
    def new_text(text):
        for r in rew:
            if r.get('original') and r['original'].lower() in text.lower():
                i = text.lower().index(r['original'].lower()); text = text[:i] + r['rewrite'] + text[i + len(r['original']):]
        return text

    def shot_of(line):
        start = max(0.0, float(line['start']))
        return next((i for i, (_, a, b) in enumerate(shots) if a - 1e-6 <= start < b), max(0, len(shots) - 1))

    by_shot = {}
    for l in speech: by_shot.setdefault(shot_of(l), []).append(l)
    shot_ids = [sid for sid, _, _ in shots]
    with_product = [sid for sid in shot_ids if sid in product] if shots else sorted(product)
    # the design looked at this shot and saw no competitor (form None): no product here, not a gap in the design
    with_product = [sid for sid in with_product if not (sid in design and not design[sid].get('form'))]
    rows = {sid: design[sid] for sid in with_product if (design.get(sid) or {}).get('form')}
    missing = [sid for sid in with_product if sid not in rows]
    views = sd.views_of(rows.values())
    forms = sd.forms_of(rows.values()) + (['【写：按 rules-now.md 规则表定的产品形态和数量】'] if missing else [])
    removals = sorted({x for r in rows.values() for x in r['removals']}) + (['【写：原片里竞品的颜色样子和位置，有几个写几个】'] if missing else [])
    has_product = bool(with_product)
    lang = '【写：语言】'

    goal = '复刻@视频1的【写：几个镜头的景别切换、环境、人物神态】' + (f'与用{lang}说的台词' if speech else '')
    if has_product:
        goal += f'，将原片中的{"、".join(removals)}完全替换为{"和".join(forms)}' + ('，并按@图片2的手掌比例写抓握' if 'carton_hand' in views else '')
    roles = ['@视频1用于复刻【写：人物长相服装、陈设】、镜头切换机位与时机' + ('和说话口型节奏' if speech else '')
             + (f'，完全移除原片中的{"、".join(removals)}及字幕。' + sd.SCALE_VIDEO if has_product else '，完全移除原片字幕。')]
    roles += [f'@图片{i}{sd.IMG_ROLE[v]}' for i, v in enumerate(views, 1)]
    if missing: roles.append('【写：逐张写 @图片N 用于……，不采用……（图按 rules-now.md 规则表）】')
    subject = '【写：光线、场景、背景里看得见的东西；人物的年龄、族裔、发型、衣服、站位】。'
    if has_product: subject += f'画面里只有{"和".join(forms)}，原片竞品所在的其他位置是空的。' + sd.physics_of(views)
    if speech: subject += '音色——【写：每个说话人一句：年龄、口音、音色、语气，不是播音腔】'

    def said(lines):
        return ''.join(f"【写：说话人称呼；画外说就写“画外”加称呼】台词（【写：语气】，用{lang}）：{{{new_text(l['text'])}}}。" for l in lines)

    script = []
    for i, (sid, a, b) in enumerate(shots, 1):
        r = rows.get(sid)
        if sid in product: act = sd.act_of(r) if r else '【写：产品形态和动作（rules-now.md 规则表）】'
        else: act = '【写：谁在做什么】'
        script.append(f"# 阶段{sd.stage_label(i)} = 镜头{i} {sid} 原片 {a:.2f}–{b:.2f} 秒" + (f"；设计 {r['rule']}：{r['form']} / {r['action']}" if r else '')
                      + '；这一镜里有溶接或没切开的机位变化时，按画面每个机位各写一个阶段')
        script.append(f"阶段{sd.stage_label(i)}：{'切至' if i > 1 else ''}【写：景别和机位（谁的过肩、拍谁）】。{act}。{sd.SCALE_HOLE if sid in product else ''}{said(by_shot.get(i - 1, []))}"
                      + ('结束时【写：产品和人的终态】。' if sid in product else '结束时【写：人的终态】。'))
    if not shots:
        script.append('阶段一：【写：景别和机位，谁在做什么】。' + said(speech) + '结束时【写：终态】。')
    keep = ('全程仅' + '和'.join(forms) + '，' if has_product else '') + ('盒身尺寸与手掌比例严格匹配@图片2，硬挺无挤压变形；' if 'carton_hand' in views else '') \
        + (sd.SCALE_KEEP if has_product else '') \
        + '人物面容服装、道具、空间方向与光线同@视频1一致；' + ('听者自然闭口聆听；' if speech else '') \
        + '【写：声音——原片有背景音乐写“背景音乐和环境声同@视频1”，没有写“无bgm，只有环境声和动作声”】' + ('；台词只有上面写出的这些' if speech else '') + '；画面不显示字幕。'

    unplaced = [r for r in rew if not any((r.get('original') or '\0').lower() in (l.get('text') or '').lower() for l in speech)]
    speech_notes = ''.join(f"# 原声 {line['start']:.2f}–{line['end']:.2f} 秒 {line['speaker']}: {line['text']}\n" for line in speech)
    seam = []
    if seg.get('in_shot_cut_in'): seam.append('本段开头在一个连续长镜头中间（句间停顿处切开），按本段内容写。回剪时根据生成同期声复核气口。')
    if seg.get('in_shot_cut_out'): seam.append('本段结尾在一个连续长镜头中间（句间停顿处切开），回剪时根据实际生成复核结尾气口。')
    seam_lines = ''.join(f'# {x}\n' for x in seam)
    mute_note = (f"# 消音：本段有改词，用 generate --prepare-only --segment {seg['id']} --mute {seg['id']}:起-止[,起-止]（只消冲突词，起止看波形，见 workflow.md 附录），参考片的声音里就不再有旧词。\n" if rew else '')
    unplaced_note = ('# 下面这些改词没能自动放进句子，写进对应的那一句：' + '；'.join(r['rewrite'] for r in unplaced) + '\n' if unplaced else '')
    design_note = ('' if not has_product else
                   f"# 设计表还没覆盖这些产品镜头：{missing}。先 swap_design.py observe + decide（decide 会重写还没动过的骨架），不要自己定形态\n" if missing else
                   f"# 形态、图、数量、动作来自设计表（rules-now.md 规则表），不要改；判错就改规则表和它的测试\n")
    views_line = ','.join(views) if views and not missing else ('【写：按设计表】' if has_product else 'none')
    route = f'APIMART {seedance.MODEL}（画幅 {seedance.ASPECT_RATIO}、分辨率 {seedance.RESOLUTION}、时长 {output_seconds(seg["duration_s"])} 秒，都由接口参数设定）'
    return f"""# {seg['id']} — 五段式（references/rules-now.md；用户 2026-11-20 统一）。提交时去掉 # 行。@视频1 = 原段（字幕和漂浮水印已去掉，竞品留着；有改词的段把冲突词消音），@图片N = # views 里按顺序上传的图。生成：{route}。
# 一次成片：本段只付费生成一次，生成什么都要用。成败全在提交前。
# 写法：一个镜头一个阶段，每阶段一句景别机位、一句产品在哪/手怎么动、台词 {{原词}}（带语气和语言）、一句“结束时……”；
#   【事件脚本】只写正向；“移除 / 不采用 / 仅 / 空的”只写在参考素材职责、主体与关系、保持一致里。
#   不写时间戳、画幅、分辨率、时长；不写镜头号和规则号；不写“将@视频1中…更换为…”“这不是视频编辑”“动作照@视频1”、厘米数、勺子。
# 本条竞品：{rival}。竞品盘点：逐镜头找出每一处（容器、瓶盖/瓶口、滴管/胶囊/软糖/粉勺、手里/桌上/背景/镜面/虚焦/出画一半），每一处都在【生成目标】和【参考素材职责】的移除清单里，出现的那一阶段写我方产品在那里。
# 服用一律闭唇轻抿（用户 2026-11-11）、条包不在画面里撕、不自己立着。
# 镜头：{seg['shot_ids']}  原片帧 [{seg['in_frame']},{seg['out_frame']})  {seg['duration_s']} 秒，输出 {output_seconds(seg['duration_s'])} 秒
{design_note}{seam_lines}{speech_notes}# 识别给出的改词：{[(r['original'], r['rewrite']) for r in rew]}
# 改词原则：只改产品名、剂型、用量用法、产品里没有的具体成分名这些冲突词，音节尽量接近原词；其余台词逐字照写。
{mute_note}{unplaced_note}# views: {views_line}
# design: {','.join(rows)}
【生成目标】
{goal}。

【参考素材职责】
{chr(10).join(roles)}

【主体与关系】
{subject}

【事件脚本】
{chr(10).join(script)}

【保持一致】
{keep}
"""


def stub_sha_file(run, gid): return Path(run)/'prompts'/f'{gid}-a1.stub.sha256'


def write_stub(run, seg, text):
    """Write a segment skeleton and remember its hash: a skeleton nobody has edited may be rewritten later (decide)."""
    p = Path(run)/'prompts'/f"{seg['id']}-a1.txt"; p.write_text(text)
    stub_sha_file(run, seg['id']).write_text(sha(p))
    return p


def stub_untouched(run, gid):
    p = Path(run)/'prompts'/f'{gid}-a1.txt'; h = stub_sha_file(run, gid)
    return not p.is_file() or (h.is_file() and h.read_text().strip() == sha(p))


def rewrite_untouched_stubs(run, design):
    """After `swap_design.py decide`: every segment skeleton still exactly as written is rebuilt with the design rows."""
    run = Path(run); plan = read(run/'plan/segments.json'); out = []
    for seg in plan['segments']:
        if stub_untouched(run, seg['id']):
            write_stub(run, seg, prompt_stub(seg, comp.active(run), design)); out.append(seg['id'])
    return out


def parse_views(spec):
    """'# views:' / --views value -> ordered view names. 'none' (or an empty header) = no product image at all: a
    segment that only re-voices narration and shows no product sends the reference video alone."""
    spec = (spec or '').strip()
    if spec.lower() in ('none', '-'): return []
    return [v.strip() for v in (spec or DEFAULT_VIEWS).split(',') if v.strip()]


def resolve_view(profile, name):
    """Registered product view name (handheld_photo, opened_packet_hand, carton_views, carton_hand) → skill-relative path.
    Competitor anchors and any unregistered name are refused."""
    if name in profile['reference_views']: return profile['reference_views'][name]
    raise SystemExit('unknown reference view: ' + name + ' (known: ' + ', '.join(profile['reference_views']) + ')')


def view_path(run, profile, name):
    """A registered product view, or `run:<file>` = RUN/refs/<file>: a reference image made for this film from the
    product photos (user 2026-11-19: "应该从提示词和参考图下手" -- e.g. our cartons on the shopping page the original
    shows, or packed in its shipping box), never a competitor image."""
    if name.startswith('run:'):
        f = Path(run)/'refs'/name[4:]
        if not f.is_file() or f.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
            raise SystemExit(f'reference image {f} missing (run: views are images in RUN/refs/)')
        return f
    return SKILL/resolve_view(profile, name)


def clean_prompt(text):
    return '\n'.join(l for l in text.splitlines() if not l.startswith('#')).strip()


CAPTION_BOX_MAX_H = 0.12  # --caption-box: one or two caption lines of a 9:16 frame at most
REF_SHORT_EDGE = 720     # short edge of the reference video the model receives
REF_CRF = 12             # the one encode; sources are ~0.2 Mbps, so this is still visually lossless (SSIM 0.994 vs CRF 4)
REF_MAXRATE, REF_BUFSIZE = '1500k', '3000k'   # APIMART drops a reference download after 60 s and the tunnel gives ~100-180 KB/s
REF_MAX_BYTES = 2_200_000   # whole-file budget: 2026-10-25 a 15 s clip at 1.5 Mbps was 3.1 MB and took APIMART 40 s to fetch
REF_AUDIO_KBPS = 256        # audio is copied, or re-encoded at 256k for muted references
REF_MIN_KBPS = 700          # still 3-4x the ~0.2 Mbps sources


def ref_maxrate(duration_s):
    """Video bitrate cap for a reference clip: 1.5 Mbps, lowered for long segments so the whole file stays under
    REF_MAX_BYTES (8 s -> 1500k, 15 s -> ~900k)."""
    kbps = REF_MAX_BYTES * 8 / max(float(duration_s), 0.5) / 1000 - REF_AUDIO_KBPS
    return f'{int(max(REF_MIN_KBPS, min(1500, kbps)))}k'
                                              # per download (2026-10-25 access log): CRF 4 clips (10-25 MB) never arrived,
                                              # 1.5-2.3 MB capped clips arrived in 5-11 s on the first try


def source_window(seg, fps):
    """Segment frames are on the integer-CFR working grid; as times they address the original source directly."""
    return seg['in_frame'] / fps, (seg['out_frame'] - seg['in_frame']) / fps


def reference_scale(width, height, short_edge=REF_SHORT_EDGE):
    """Lanczos upscale to the target short edge; a source already at or above it is passed through unscaled."""
    if min(width, height) >= short_edge: return None, (width, height)
    if width <= height: w, h = short_edge, int(round(height * short_edge / width / 2)) * 2
    else: h, w = short_edge, int(round(width * short_edge / height / 2)) * 2
    return f'scale={w}:{h}:flags=lanczos', (w, h)


REF_MARK_MAX = 0.028   # of the frame height: the thickest a floating watermark line gets (36 px at 720x1280)
OCR_LOOKALIKES = str.maketrans('УЯОАЕРНМСІТКВХ', 'YROAEPHMCITKBX')   # Cyrillic letters Vision OCR puts into faint Latin text


def watermark_boxes(run, seg, fps, ref_fps, size, clip=None):
    """Where the floating watermark is on this segment's reference: {reference frame: [boxes]} at the reference's size.
    Two sources: RUN/watermark/scan.json (the master scan) and, with `clip`, an edge scan of the reference itself, which
    reads faint marks the 360p master scan missed (Lymphoria G09, 2026-11-11: the vertical mark at the right edge was in
    no master detection). Every fuzzy match of the brand word is a 'floating_overlay' in the master scan, the bottle
    label and the caption word too, and each is held 0.8 s before and after. A held box is kept only where a watermark
    can be: outside caption position and no thicker than a watermark line. The held copy of a label box sat on the
    man's forearm in the next shot and LaMa erased the arm (same segment). Text that is really there is removed by the
    OCR pass on the frames where it is readable."""
    import caption_clean as cc
    W, H = size

    def keep(box, synthetic):
        return not synthetic or (not cc.is_caption(box, W, H) and min(box[2], box[3]) <= REF_MARK_MAX * H)

    out = {}
    scan_path = run/'watermark'/'scan.json'
    scan = read(scan_path) if scan_path.is_file() else {}
    if not scan.get('overlay_classification_reviewed'): scan = {}
    src = (scan.get('source') or {}).get('path')
    if src and Path(src).is_file():
        v = media.probe(src)['video']; kx, ky = W / int(v['width']), H / int(v['height'])
        for d in scan.get('detections') or []:
            f = d.get('frame')
            if d.get('classification') != 'floating_overlay' or f is None or not seg['in_frame'] <= f < seg['out_frame']: continue
            x, y, w, h = d['bbox']; box = [int(x * kx), int(y * ky), int(math.ceil(w * kx)), int(math.ceil(h * ky))]
            if not keep(box, d.get('synthetic')): continue
            j0 = int(math.floor((f - seg['in_frame']) / fps * ref_fps)); j1 = int(math.ceil((f + 1 - seg['in_frame']) / fps * ref_fps))
            for j in range(j0, max(j1, j0 + 1)): out.setdefault(j, []).append(box)
    tpl_clip = None
    if clip is not None and Path(clip).is_file() and scan.get('method') == 'template':
        # the master was matched against the stored template: match the reference too (every frame, no OCR)
        import watermark_template as wtpl
        tpl_clip = wtpl.scan(clip, scan['target'], require=False)
        if tpl_clip:
            for d in tpl_clip['detections']: out.setdefault(int(d['frame']), []).append(d['bbox'])
    if clip is not None and Path(clip).is_file() and not tpl_clip:
        import watermark_brand as wb
        target = watermark_target(run)
        if target:
            wb.set_target(target)
            work = run/'clips'/'caption_work'/(Path(clip).stem + '.edge')
            rec = wb.edge_scan(clip, work.with_suffix('.json'), work)
            dets = [{'frame': d['frame'], 'bbox': d['bbox'], 'text': target, 'classification': 'floating_overlay'} for d in rec['detections']]
            # A faint mark away from the edges is read in pieces ("LYMPE", "@LYMPE SRI", "LYMP"): on a reference, which never
            # shows our own product, any four letters in a row of the competitor's word in a thin line outside caption
            # position is the mark. The stricter matcher drops pieces shared with LYMPHLOOP, and three such marks went to the
            # generator and came back in the output (Lymphoria G01, G02, G09, 2026-11-11).
            pieces = re.compile('|'.join(sorted({target[i:i + 4] for i in range(max(1, len(target) - 3))})))
            for d in cc._scan(clip, run/'clips'/'caption_work')['detections']:
                letters = ''.join(c for c in d['text'].upper().translate(OCR_LOOKALIKES) if c.isalpha())
                if pieces.search(letters) and not cc.is_caption(d['bbox'], W, H) and min(d['bbox'][2], d['bbox'][3]) <= REF_MARK_MAX * H:
                    dets.append({'frame': d['frame'], 'bbox': d['bbox'], 'text': target, 'classification': 'floating_overlay'})
            info = media.probe(str(clip)); frames = int(round(float(info['video_duration']) * ref_fps))
            pad_overlay_fades(dets, frames, W, H, pad=fade_pad_frames(ref_fps), target=target, cuts=hard_cuts(clip))
            for d in dets:
                if keep(d['bbox'], d.get('synthetic')): out.setdefault(int(d['frame']), []).append(d['bbox'])
    review = watermark_position_review(run)
    if review:
        kept = {k: [b for b in boxes if reviewed_watermark_box(b, review, W, H)] for k, boxes in out.items()}
        out = {k: boxes for k, boxes in kept.items() if boxes}
    return out


EDGE_FRAMES = 3      # frames checked at each end of a reference for a stray frame of the neighbouring shot
EDGE_JUMP = 16.0     # mean absolute difference (0-255, 64 px wide grey frames) that only a hard cut produces
# After an input seek the first frame's timestamp is a fraction of a frame above 0 and ffmpeg fills the gap with a copy
# of it, so frame numbers inside the filter chain and in the output differ by one (measured 2026-11-11: trim=end_frame=284
# let frame 284, the next shot, through). Timestamps are zeroed first, in the probe and in the encode alike.
EDGE_HEAD = 'setpts=PTS-STARTPTS'


def edge_trim(src, ss, dur, ref_fps):
    """(first_frame, end_frame) of the reference window without stray frames of the neighbouring shots.
    Segment boundaries live on the integer-fps working grid; cut by time from a 23.976 / 29.97 fps source, the window can
    start one frame early or end one frame late, on the other side of the hard cut (2026-11-11, Lymphoria 293 G03: the
    last frame was the next scene's bathroom, and the tail hold repeated it). A hard cut inside the first or last
    EDGE_FRAMES frames is never a planned shot boundary, so the frames beyond it are dropped."""
    import numpy as np
    n_expected = int(round(dur * ref_fps))
    w, h = 64, 112
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-ss', f'{ss:.6f}', '-t', f'{dur:.6f}', '-i', str(src), '-map', '0:v:0',
                          '-vf', f'{EDGE_HEAD},fps={ref_fps},scale={w}:{h},format=gray', '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
    n = len(raw) // (w * h)
    if n < 2 * EDGE_FRAMES + 2: return 0, min(n, n_expected) or n_expected
    frames = np.frombuffer(raw, np.uint8, n * w * h).reshape(n, h, w).astype(np.int16)
    jump = lambda k: float(np.abs(frames[k] - frames[k - 1]).mean())      # between frame k-1 and k
    first = max([k for k in range(1, EDGE_FRAMES + 1) if jump(k) > EDGE_JUMP], default=0)
    end = min([k for k in range(n - EDGE_FRAMES, n) if jump(k) > EDGE_JUMP], default=n)
    return first, min(end, first + n_expected)


def reference_edge_policy(run, state, seg):
    """Agent-verified transition edges; a source/window-bound local reference policy."""
    path = run/'plan/reference_edge_overrides.json'
    if not path.is_file(): return None
    data = read(path); item = data.get('segments', {}).get(seg['id'])
    if item is None: return None
    if data.get('source_sha256') != state.get('source_sha256') or item.get('source_frames') != [seg['in_frame'], seg['out_frame']]:
        raise SystemExit(f"{seg['id']}: reference edge review does not match this source/window")
    if not item.get('reviewed') or not item.get('basis') or not all(type(item.get(k)) is int for k in ('first', 'end')):
        raise SystemExit(f"{seg['id']}: reference edge override needs reviewed native-frame evidence")
    return item


def reference_clip_hq(run, state, seg, fps, short_edge=REF_SHORT_EDGE, clean=False, upload=False, erase=()):
    """Source reference: the segment cut by time from the ORIGINAL source (not master-cfr / the watermark master), with
    its original audio, upscaled once to a 720 short edge at CRF 12 capped at 1.5 Mbps.
    upload=True is the clip the generator receives: at least 24 fps and held on its last frame (with silence) up to the
    whole number of seconds the output is generated at, so reference and output have the same length.
    clean=True removes the burned-in captions and the floating watermark with LaMa in the same encode (OCR runs on the
    uncleaned clip; pixels are re-read from the source). User, 2026-11-11: "送给模型的视频不要带字幕，处理好了再送模型".
    erase=[(x, y, w, h, a, b)]: competitor objects removed from the reference with LaMa in the same encode (x, y, w, h as
    fractions of the frame, a-b seconds from the segment start; user 2026-11-15: a bottle left in a mid or wide shot of
    the reference is copied into the output whatever the prompt says, batch7 8 of 10 severe goofs). Needs clean=True.
    Cached per source sha + range."""
    erase = [tuple(float(v) for v in e) for e in (erase or ())]
    if erase and not clean: raise SystemExit('erase needs the cleaned reference (clean=True)')
    src = Path(state['source']); ss, dur = source_window(seg, fps)
    stem = seg['id'] + ('.hq.clean' if clean else '.hq') + ('.ref' if upload else '')
    out = run/'clips'/(stem + '.mp4'); meta = run/'clips'/(stem + '.json')
    policy_file = run/'plan/reference_caption_policy.json'
    caption_policy = read(policy_file).get(seg['id']) if clean and policy_file.is_file() else None
    source_rewrites = seg.get('rewrites') or {}
    source_spoken = [s['text'] for s in seg.get('speech', [])] + [rw['original'] for rw in source_rewrites.get('dialogue', []) + source_rewrites.get('vo', [])]
    key = {'source_sha256': sha(src), 'start_s': round(ss, 6), 'duration_s_requested': round(dur, 6), 'short_edge': short_edge,
           'crf': REF_CRF, 'maxrate': ref_maxrate(dur), 'clean': 'lama_line_mask+watermark' if clean else False, 'upload': upload,
           'seconds': output_seconds(dur) if upload else None, 'v': 14 if clean else 11, **({'erase': [list(e) for e in erase]} if erase else {})}
    edge_policy = reference_edge_policy(run, state, seg) if upload else None
    if edge_policy: key['reviewed_reference_edge'] = edge_policy
    if caption_policy:
        key['caption_policy'] = caption_policy
        key['source_caption_words_sha256'] = api.sha(api.json_bytes(source_spoken))
    wm_review = watermark_position_review(run, state) if clean else None
    if wm_review and any(p.get('agent_classification') != 'caption_keep'
                         and p.get('first', 0) - fade_pad_frames(fps) < seg['out_frame']
                         and p.get('last', 0) + fade_pad_frames(fps) >= seg['in_frame']
                         for p in wm_review.get('excluded_positions', [])):
        key['watermark_position_review_sha256'] = sha(run/'watermark/agent_positions.json')
    if out.is_file() and meta.is_file():
        m = read(meta)                     # an erased version is not the plain one (11-19: the plain request matched an erased cache),
        if all(m.get(k) == v for k, v in key.items()) and m.get('erase') == key.get('erase') and m.get('caption_policy') == caption_policy \
                and m.get('reviewed_reference_edge') == edge_policy \
                and m.get('output_sha256') in (None, sha(out)):    # and a file cut short by a killed run is rebuilt
            return out
    info = media.probe(str(src)); v = info['video']
    vf, size = reference_scale(int(v['width']), int(v['height']), short_edge)
    ref_fps = fps; pad_s = 0.0; edge = None; out_t = []
    if upload:
        ref_fps = min(60, max(24, fps))
        first, end = edge_trim(src, ss, dur, ref_fps)
        if edge_policy:
            first, end = edge_policy['first'], edge_policy['end']
            if not 0 <= first < end <= round(dur * ref_fps):
                raise SystemExit(f"{seg['id']}: reviewed reference edge exceeds the actual source window")
        edge = [first, end]; kept = end - first
        total = output_seconds(dur) * ref_fps                     # exactly this many frames reach the generator
        pad_s = max(0.0, (total - kept) / ref_fps)
        out_t = ['-t', f'{total / ref_fps:.6f}']                   # picture and sound end together on the whole second
        # frame-exact: a stray head frame is skipped by starting later (picture and sound together), a stray tail frame
        # by the frame count; then the last real frame is held up to the whole-second length
        ss, dur = ss + first / ref_fps, (kept + 2) / ref_fps        # two spare frames to read; the frame count below decides
        vf = ','.join(filter(None, (EDGE_HEAD, vf, f'fps={ref_fps}', f'trim=end_frame={kept}',
                                    f'tpad=stop_mode=clone:stop={total - kept}' if total > kept else None, f'fps={ref_fps}')))
    audio_copy = not pad_s and bool(info['audio']) and info['audio'].get('codec_name') in ('aac', 'mp3')
    if clean:
        plain = reference_clip_hq(run, state, seg, fps, short_edge, upload=upload)
        import caption_lama
        extra = watermark_boxes(run, seg, fps, ref_fps, size, clip=plain)
        n_frames = int(total) if upload else int(round(dur * ref_fps))
        seg_dur = (seg['out_frame'] - seg['in_frame']) / fps          # a box that lasts to the segment end also covers the held tail
        boxes_in = [(x, y, w, h, a, 1e9 if b >= seg_dur - 0.05 else b) for x, y, w, h, a, b in erase]
        for k, boxes in erase_boxes(boxes_in, size, ref_fps, edge[0] if edge else 0, n_frames).items():
            extra.setdefault(k, []).extend(boxes)
        report = caption_lama.clean(plain, out, run/'clips'/'caption_work', keep_audio=True, extra=extra,
                                    pixels={'path': str(src), 'ss': ss, 't': dur, 'vf': vf, 'size': size, 'crf': REF_CRF, 'maxrate': ref_maxrate(dur), 'bufsize': f'{2 * int(ref_maxrate(dur)[:-1])}k', 'audio_copy': audio_copy, 'pad_s': pad_s, 'out_t': out_t,
                                            'caption_policy': caption_policy,
                                            'source_spoken': source_spoken})
        left_n = report.get('frames_left_caption', report.get('frames_left', 0))
        if left_n > max(2, 0.02 * report['frames']):
            raise SystemExit(f"{seg['id']}: text is still readable where captions were removed on {left_n} frames ({report.get('texts_left_caption') or report.get('texts_left')}); look at {out} before anything is submitted")
        if report.get('frames_left', 0) > left_n:
            print(f"{seg['id']}: {report['frames_left']} frames still show text inside the caption band that is not the captions "
                  f"({report.get('texts_left')}): labels or signs; look at {out}", file=sys.stderr)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        # -ss and -t are both INPUT options: with -t on the output, the copied audio kept packets from the preceding
        # keyframe and started 2.2 s early on v025 G01 (measured); as input options audio lines up at 0 ms.
        media.ff(['-y', '-ss', f'{ss:.6f}', '-t', f'{dur:.6f}', '-i', str(src), '-map', '0:v:0', '-map', '0:a:0?',
                  *(['-vf', vf] if vf else []), '-c:v', 'libx264', '-preset', 'slow', '-crf', str(REF_CRF), '-maxrate', ref_maxrate(dur), '-bufsize', f'{2 * int(ref_maxrate(dur)[:-1])}k', '-profile:v', 'high',
                  '-pix_fmt', 'yuv420p', *(['-c:a', 'copy'] if audio_copy else ['-c:a', 'aac', '-b:a', '256k']),
                  *(['-af', f'apad=pad_dur={pad_s:.6f}'] if pad_s and info['audio'] else []), *out_t,
                  '-movflags', '+faststart', str(out)])
        report = None
    got = media.probe(str(out))
    if upload:
        stray = edge_trim(out, 0.0, kept / ref_fps, ref_fps)
        if stray != (0, kept):
            raise SystemExit(f"{seg['id']}: the reference still starts or ends on a frame of the neighbouring shot (frames kept {stray} of {kept}); check {out}")
    save(meta, {**key, 'source': str(src), 'output_sha256': sha(out), 'size': [got['video']['width'], got['video']['height']],
                'duration_s': got['video_duration'], 'has_audio': bool(got['audio']), 'audio': 'copied' if audio_copy else 'aac 256k',
                'encodes': 1, **({'edge_frames_kept': edge} if edge else {}), **({'caption_clean': {k: report[k] for k in ('method', 'frames', 'frames_cleaned', 'frames_with_ocr_captions', 'watermark_frames', 'frames_left') if k in report}} if report else {})})
    return out


def erase_boxes(erase, size, ref_fps, first=0, n_frames=None):
    """{reference frame: [[x, y, w, h] px]} for --erase boxes given as frame fractions over segment seconds a-b. The
    reference starts `first` frames into the segment (stray head frames skipped); held tail frames past the segment
    keep the box of its last second."""
    import math
    W, H = size; out = {}
    for x, y, w, h, a, b in erase:
        box = [int(x * W), int(y * H), int(math.ceil(w * W)), int(math.ceil(h * H))]
        k0 = max(0, int(math.floor(a * ref_fps)) - first)
        k1 = int(math.ceil(b * ref_fps)) - first
        if n_frames is not None and b >= 1e8: k1 = n_frames
        for k in range(k0, max(k0, k1)):
            if n_frames is None or k < n_frames: out.setdefault(k, []).append(list(box))
    return out


def parse_erase(specs):
    """--erase G##:x,y,w,h@a-b (frame fractions 0-1; seconds from the segment start; b may be 'end')."""
    out = {}
    for spec in specs or []:
        try:
            seg, rest = spec.split(':', 1); box, _, span = rest.partition('@')
            x, y, w, h = (float(v) for v in box.split(','))
            a, _, b = span.partition('-'); a = float(a); b = 1e9 if b.strip() == 'end' else float(b)
        except ValueError:
            raise SystemExit(f'--erase G##:x,y,w,h@a-b (fractions of the frame, seconds), got {spec!r}') from None
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.0001 and y + h <= 1.0001 and 0 <= a < b):
            raise SystemExit(f'--erase {spec!r}: box must lie inside the frame (fractions 0-1) and a < b')
        out.setdefault(seg, []).append((x, y, w, h, a, b))
    return out


RESIDUE_HARD_STATES = ('standing', 'pouring', 'opening', 'spooning')


def residue_blocking(items):
    """Competitor objects left in a reference that the generator will copy (batch7, 11-15: every one of these came back):
    anything that is not the large subject of a close-up, any spoon or dropper, and any bottle standing alone, being
    poured or opened. A large container held in a close-up is left to the prompt (close-ups were replaced cleanly)."""
    return [x for x in items or [] if x.get('size') != 'large' or x.get('kind') in ('spoon', 'dropper')
            or x.get('state') in RESIDUE_HARD_STATES]


def reference_residue(run, video):
    """Flash's list of competitor objects still visible in an uploaded reference (cached per file)."""
    if agent_native.is_native(): return []
    if os.environ.get('LYMPHLOOP_NO_FLASH') == '1': raise RuntimeError('Flash disabled (LYMPHLOOP_NO_FLASH=1)')
    import flash_screen
    return flash_screen.reference_residue(video, run)


def parse_mutes(specs):
    out = {}
    for spec in specs or []:
        seg, spans = spec.split(':', 1)
        out.setdefault(seg, []).extend(tuple(float(x) for x in span.split('-')) for span in spans.split(','))
    return out


def mute_cuts(run, state, seg, windows):
    """Mute windows that cut through a word (words.cut_words), from the word timings words.py saved for this segment.
    Nothing when the timings are missing: the check is advice, the agent still listens to the muted reference."""
    wfile = run/'plan/words'/f"{seg['id']}.json"
    if not windows or not wfile.is_file(): return []
    try:
        import words as words_mod
        from fractions import Fraction
        fps = float(Fraction(str(read(run/'plan/segments.json').get('fps') or 30)))
        audio = words_mod.pcm(state['source'], seg['in_frame'] / fps, (seg['out_frame'] - seg['in_frame']) / fps)
        return words_mod.cut_words(audio, words_mod.source_words(run, seg, fps, state['source']), windows)
    except Exception as exc:                                  # advice only: never stops a prepare
        print(f"{seg['id']}: mute-window check skipped ({exc})", file=sys.stderr); return []


def with_tail(seg, spans):
    """Mute windows plus the padded tail after the segment's own length (see prepare_jobs)."""
    end = float(seg['duration_s']); total = float(output_seconds(seg['duration_s']))
    return list(spans) + ([(round(end, 2), round(total + 0.05, 2))] if total - end > 0.05 else [])


def muted_reference(clip, spans):
    """Silence conflicting words in the prepared reference while copying its video stream."""
    tag = '-'.join(f'{a:.2f}_{b:.2f}' for a, b in spans)
    out = clip.with_name(clip.stem + f'.muted-{tag}.mp4')
    # rebuilt whenever the clip it was cut from is newer (2026-10-25: the reference encode changed to a capped bitrate,
    # and a stale CRF 4 muted file of the same name would otherwise be reused and never download in time)
    if not out.is_file() or out.stat().st_mtime < clip.stat().st_mtime:
        expr = '+'.join(f'between(t,{a:.3f},{b:.3f})' for a, b in spans)
        media.ff(['-y', '-i', str(clip), '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'copy', '-af', f"volume=enable='{expr}':volume=0",
                  '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart', str(out)])
    return out


def output_seconds(duration):
    """Whole seconds the segment is generated at: its length rounded UP (user, 2026-11-11: decimals are rounded up, the
    official guide works in 1-second units), never under the API minimum."""
    return int(max(OUTPUT_MIN_SECONDS, math.ceil(round(float(duration), 3) - 1e-9)))


TIME_UNIT = r'(秒|s\b)'
RANGE_RE = re.compile(r'(\d+(?:\.\d+)?)(\s*[–—~\-至到]\s*)(\d+(?:\.\d+)?)(\s*)' + TIME_UNIT)
POINT_RE = re.compile(r'(\d+\.\d+)(\s*)' + TIME_UNIT)


def ceil_timestamps(text):
    """Every timestamp with a decimal point is rounded UP to a whole second (user, 2026-11-11; the official Seedance 2.5
    guide: timestamps are in units of 1 second). '0.00–0.87 秒' -> '0–1 秒', '3.33–4.68秒' -> '4–5秒', '第 2.5 秒' -> '第 3 秒'.
    Returns (text, how many numbers changed)."""
    n = 0
    def up(x):
        nonlocal n
        v = int(math.ceil(float(x) - 1e-9))
        if '.' in x: n += 1
        return str(v)
    text = RANGE_RE.sub(lambda m: up(m.group(1)) + m.group(2) + up(m.group(3)) + m.group(4) + m.group(5), text)
    text = POINT_RE.sub(lambda m: up(m.group(1)) + m.group(2) + m.group(3), text)
    return text, n


NEGATIVE_RE = re.compile(r'不要|不得|不能|不可|不会|不再|不许|不准|不应|不该|不见|不出现|不显示|不添加|不新增|不增加|不生成|不采用|不沿用|不保留|不改变|不改动|不替换|不移除|不撕|不滴|不倒|不伸|不露|不张|不带|不含|不留|绝不|决不|切勿|请勿|禁止|严禁|避免|防止|杜绝|别让|没有|无需|不是|看不到|看不见')
NEGATIVE_OK_RE = re.compile(r'字幕|叠字|文字叠加|标题卡|水印|bgm|BGM|背景音乐|配乐|音乐|音效|环境音|旁白|对白|声音|人声|说话|闭口|这不是视频编辑')
FIVE_SECTION_RE = re.compile(r'【事件脚本】(.*?)(?=【[^】]+】|\Z)', re.S)   # carton-swap.md template (user 2026-11-20)


def checked_part(text):
    """The part of a prompt the positive-description rule applies to. The five-section carton template (carton-swap.md)
    states removals and counts in its role / subject / consistency sections ("完全移除原片中的…", "不采用九宫格版式",
    "无第二个盒子") -- that is how it worked (user 2026-11-20, G05/G16 one-shot); only its 【事件脚本】 stays positive."""
    if '【参考素材职责】' not in text: return text
    m = FIVE_SECTION_RE.search(text)
    return m.group(1) if m else ''


def negative_sentences(text):
    """Sentences that tell the model what NOT to show. The official guide: describe positively; negative control is
    supported for captions and audio only (user, 2026-11-11: "提示词按官方说的只写正向描述"). Spoken lines in quotes or
    braces are not checked."""
    bare = re.sub(r'[“"{][^”"}]*[”"}]', '', checked_part(text))
    out = []
    for sent in re.split(r'[。；;！!？?\n]+', bare):
        sent = sent.strip()
        if sent and NEGATIVE_RE.search(sent) and not NEGATIVE_OK_RE.search(sent): out.append(sent)
    return out


SPOON_RE = re.compile(r'勺|茶匙|汤匙|小匙|spoon|scoop', re.I)
SIZE_RE = re.compile(r'\d+(\.\d+)?\s*(厘米|公分|毫米|cm|mm)(?![a-z])', re.I)   # user 2026-11-20: proportions only
COPY_RISK = [  # (regex over one sentence, controlled-edit.md §4.8 row, advice) — warnings, the agent decides
    (re.compile(r'(动作|节奏|手势|握法|拿法)[^。，；]{0,8}照(@视频1|它)|照@视频1[^。，；]{0,6}(动作|手势|握法|拿法)'),
     '§4.4 动作', '“动作照@视频1”会让模型照搬原视频的动作和手里竞品的样子（用户 2026-11-19）。@视频1 只管场景、人物、机位和时长，拿产品的手做什么逐拍写出来'),
    (re.compile(r'(条包|软袋).{0,40}(平放|平躺|放在|放到|放下|立在).{0,12}(掌心|手心|长凳|凳|台面|桌面|桌上|抽屉)|(掌心|手心|长凳|凳|台面|桌面|桌上|抽屉).{0,12}(平放|平躺|放着|躺着).{0,20}(条包|软袋)'),
     '§4.8 第 1 类', '条包离手放到掌心 / 长凳 / 台面：原物在这一拍立着时，模型会让条包也立着（11-13 五段）。原物立着就写成条包一直捏在指尖，或改用能立的纸盒'),
    (re.compile(r'双手(覆|捂|合|包|拢|握住|夹住)|握拳|合握|攥|包在(手|掌)'),
     '§4.8 第 3 类', '产品被手整个包住或遮住：遮挡后模型会把产品画丢、缩小或凭空长出来。改成只握纸盒两侧 / 只捏条包一端，产品主体露在手外'),
    (re.compile(r'(纸盒|盒子|条包|软袋).{0,15}(落在|落到|落下|落于|掉在|掉到|掉落|飘落|降落|进入镜头|进入画面|入画|飞入|滑入|移入)'),
     '§4.8 第 7 类', '产品自己"落下 / 进入镜头"：生成出没有手、悬空掉下来或虚影飞入（11-14 五段）。写成静止的"立在台面上，底面贴着台面"，或写清哪只手一直握着'),
    (re.compile(r'^(?=.*(条包|软袋))(?=.*(横放|平放|平躺|放着|摆着|躺着|搁着))(?!.*(一支|同一支|这支|那支|单支|一根))'),
     '数量', '条包放着却没写数量：11-14 生成了两支叠放。写明"一支"'),
    (re.compile(r'(条包|软袋).{0,20}(翻转|侧转|转动|翻过来|转过来|侧过来|翻面|旋转)'),
     '§4.8 第 16 类', '条包翻转 / 侧转：扁平软袋侧过来会画成一根皱细条（11-14 NV0481 三段）。写条包正面一直朝镜头'),
    (re.compile(r'(条包|纸盒|盒子?).{0,20}(排布|排成|一排|几盒|几支|若干|多支|多盒)|(排布|一排|几盒|几支|若干|多支|多盒).{0,20}(条包|纸盒)'),
     '数量', '产品写成一排 / 几盒 / 若干：数量不明，模型会多画或少画。写确切数量"一个纸盒""一支条包"'),
    (re.compile(r'(蒲公英|拉拉藤|猪殃殃|牛蒡|红三叶草|金盏花|紫锥菊|松果菊).{0,30}(锯齿|掌状|铺开|铺展)|(锯齿|掌状).{0,20}(叶)'),
     '成分植物', '植物写"锯齿叶""叶子铺开"会画成大麻状掌状叶（11-14 两条片子），有合规风险。按 product-actions.md 成分特写表写可见特征'),
]
EXTRA_FOREIGN_INGREDIENTS = ['rutin', 'diosmin', 'hesperidin', 'quercetin', 'horse chestnut', "butcher's broom", 'prickly ash']


def foreign_ingredient_hits(run, body):
    """Ingredients our pack does not have, named anywhere in the prompt (lines included): the competitor's named
    ingredients (speech_gate.vocabulary) plus a few the prompts have invented (11-14: "Rutin" written into two films)."""
    try:
        import speech_gate
        phrases = [list(p) for p in speech_gate.vocabulary(run)['foreign_ingredients']]
    except Exception:
        phrases = []
    own = {t.lower() for name in (read(PRODUCT).get('facts') or {}).get('ingredients_printed_front') or [] for t in name.split()}
    phrases += [p.split() for p in EXTRA_FOREIGN_INGREDIENTS if p.split()[0] not in own]
    toks = _fold(body).split()          # accents folded: "raíz" was split into "ra" + "z" and never matched
    hits = []
    for ph in phrases:
        ph = [w.lower() for w in ph]
        for i in range(len(toks) - len(ph) + 1):
            if toks[i:i + len(ph)] == ph: hits.append(' '.join(ph)); break
    return sorted(set(hits))


LINE_RE = re.compile(r'\{([^{}]+)\}|“([^”]+)”')


def spoken_lines(body):
    """Spoken lines of a prompt: {原词} in the five-section layout, “…” in older prompts."""
    return [a or b for a, b in LINE_RE.findall(body)]


PARAM_RE = re.compile(r'\d+\s*[:：]\s*\d+\s*(画幅|比例|画面)?|竖版|横版|竖屏|横屏|画幅|(?<![A-Za-z0-9])(480|720|1080)\s*[pP](?![A-Za-z])|[24]\s*[kK](?![A-Za-z])|分辨率|帧率|'
                      r'(?<![A-Za-z])fps(?![A-Za-z])|时长|\d+(\.\d+)?\s*(秒|s(?![A-Za-z]))|第\s*\d+\s*秒')


def param_sentences(body):
    """Ratio, duration, resolution and frame rate are request parameters, never prompt text (user 2026-11-20: "画幅、时长、
    分辨率确实都不应该写进提示词"; Gemini clone-seedance-video rule 9). Timestamps count too: the five-section prompt
    has stages, not seconds."""
    return [x.strip() for x in re.split(r'[。；;\n]+', re.sub(r'\{[^{}]*\}', '', body)) if x.strip() and PARAM_RE.search(x)]


def speech_slack(body, seconds):
    """(needed_s, slack_s) for the spoken lines of a prompt against the output length. Lines sized well below the
    output let the model pad and garble words (11-14 trysulptique 1628: 9 s for 14 words -> "Yepa Sos Limb Loop";
    12 s for 18 words -> invented words; segments sized to their lines were clean). Rate from duration-from-lines:
    words / 2.7 + 0.5 s per sentence + 1 s."""
    lines = spoken_lines(body)
    words = sum(len(l.split()) for l in lines)
    if not words: return 0.0, 0.0
    sentences = sum(max(1, len(re.findall(r'[.!?]', l))) for l in lines)
    need = words / 2.7 + 0.5 * sentences + 1.0
    return round(need, 1), round(seconds - need, 1)


def unscripted_reference_words(run, seg, windows, body):
    """Words still audible in the reference (outside the mute windows) that the prompt's lines do not contain. The
    model reads reference speech out (11-14 trysulptique: kept "voice sample" sentences were spoken in 9 segments,
    swallowing the new first line or the brand line). Uses the source word timings from words.py."""
    wfile = run/'plan/words'/f"{seg['id']}.json"
    if not wfile.is_file(): return []
    try:                                                       # the plan may have moved the segment: heard again then
        import words as words_mod
        from fractions import Fraction
        fps = float(Fraction(str(read(run/'plan/segments.json').get('fps') or 30)))
        source = words_mod.source_words(run, seg, fps, read(run/'state.json')['source'])
    except (OSError, KeyError, ValueError):                    # no plan / state / frame range to check it against
        source = read(wfile)
    said = {re.sub(r'[^a-z0-9]', '', w.lower()) for line in spoken_lines(body) for w in line.split()}
    out = []
    for w in source:
        a, b = float(w.get('a', 0)), float(w.get('b', 0)); n = re.sub(r'[^a-z0-9]', '', str(w.get('w', '')).lower())
        if not n or b <= a: continue
        inside = sum(max(0.0, min(b, e) - max(a, s)) for s, e in windows)
        if inside >= 0.5 * (b - a): continue
        if n not in said: out.append(str(w['w']))
    return out


PHONETIC_RE = re.compile(r'[ˈˌːɪʊəɛæɔʌθðŋʃʒ]|/[^/\n“”"]{1,30}/|(读音|发音|音标|读作|念作|连读|两段连读|音节)')


# A stick pack left lying on a surface, no hand on it, is drawn standing on its end (user rule: 条包不自立). Seen on
# three films: 1753 (four "flat packet" shots all stood up), 1812 G15 ("长边贴着承托面平躺", three redos), 2114 G05
# (nightstand, first and second redo both stood up). Refused, not warned: a hand keeps holding it, or a carton stands
# where the original item stood.
FLAT_PACKET_RE = re.compile(r'(条包|软袋)[^。；;]{0,40}(平放|平躺|平贴|横放|躺在|贴着|搁在|放在|放到|放回|放下)[^。；;]{0,20}'
                            r'(床头柜|柜面|柜子|柜上|木柜|桌面|桌上|桌子|台面|台上|岛台|承托面|地面|地上|床上|长凳|凳|抽屉|架子|托盘|纸巾盒)'
                            r'|(床头柜|柜面|桌面|台面|承托面|长凳)[^。；;]{0,12}(平放|平躺|放着|躺着|搁着)[^。；;]{0,20}(条包|软袋)')
FLAT_PACKET_ADVICE = ('（条包离手平放在台面 / 柜面上：三条片子实测全被画成自己立着（1753、1812 G15、2114 G05），规则表 R2：'
                      '有手就让那只手一直捏着条包，不放下；没有手就在原物的位置立 1 个纸盒，carton_views,carton_hand 两张图）')


def copy_risks(body):
    """Sentences of an authored prompt that invite the generator to copy the reference (controlled-edit.md §4.8).
    Spoken lines in quotes are skipped. Returns (hard, warnings): a spoon anywhere in the description is refused
    (user 2026-11-14: the prompt never names a spoon — the model draws the competitor's scoop), the rest warn."""
    bare = re.sub(r'[“"{][^”"}]*[”"}]', '', body)
    hard, warn = [], []
    for sent in re.split(r'[。；;！!？?\n]+', bare):
        sent = sent.strip()
        if not sent: continue
        if SPOON_RE.search(sent): hard.append(sent)
        if FLAT_PACKET_RE.search(sent): hard.append(FLAT_PACKET_ADVICE + sent)
        if PHONETIC_RE.search(sent): hard.append('（音标 / 读音说明会被念出来）' + sent)
        if SIZE_RE.search(sent): hard.append('（写了厘米 / 毫米数：模型不理解尺寸，用和手掌、手指、画面里看得见的东西的比例写，用户 2026-11-20）' + sent)
        for rx, row, advice in COPY_RISK:
            if rx.search(sent): warn.append((row, advice, sent))
    return hard, warn


REF_WORKERS = 3   # references cleaned at once (LaMa); `generate --refs-only` runs this right after plan, while prompts are written


def prepare_refs(run, state, plan, todo):
    """The cleaned upload references for these segments (captions and watermark off with LaMa, edges trimmed, held to the
    whole second), built before any prompt exists: `prepare-only` reuses them from cache; only --erase segments and the
    audio-only --mute copy are redone."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=REF_WORKERS) as ex:
        return list(ex.map(lambda s: reference_clip_hq(run, state, s, plan['fps'], clean=True, upload=True), todo))


def prepare_jobs(a, run, state, plan, todo, job_paths):
    """Prepare and fingerprint exactly the files the submit step will use."""
    if getattr(a, 'erase', None):
        raise SystemExit('--erase 已停用（用户 2026-11-19："不能用抹除原片竞品的方式，因为模糊地带会被2.5给照搬回来，甚至抹除到人脸的部分，还会被重塑人物"）：'
                         '竞品留在参考片里，提示词在那一阶段写出我方产品、在【参考素材职责】写“完全移除原片中的…”，必要时加专门的参考图（# views: run:文件名）')
    profile = read(PRODUCT)
    for job_path in job_paths:
        ledger = run/GEN_DIR/'jobs'/job_path.name
        if ledger.is_file():
            raise SystemExit(f'{ledger.stem}: generation already recorded; keep the original prepared job')
    authored = []
    for seg in todo:
        prompt_path = run/'prompts'/f"{job_paths[todo.index(seg)].stem}.txt"
        if not prompt_path.is_file(): raise SystemExit('prompt missing: ' + str(prompt_path))
        raw_text = prompt_path.read_text(); body = clean_prompt(raw_text)
        header_views = next((l.split(':', 1)[1].strip() for l in raw_text.splitlines() if l.startswith('# views:')), '')
        if not body or '【写' in body or '【写' in header_views: raise SystemExit('prompt not authored (fill every 【写…】 slot): ' + str(prompt_path))
        if '【参考素材职责】' not in body:
            raise SystemExit(f"{seg['id']}: 提示词统一按五段式写（用户 2026-11-20，references/rules-now.md）。旧写法的稿子："
                             f"删掉 {prompt_path.name} 后重跑 plan（或 swap_design.py decide）生成五段式骨架再填")
        import swap_design as sd
        hv = parse_views(a.views if a.views else (header_views if header_views else DEFAULT_VIEWS))
        problems = sd.five_section_problems(body, n_shots=len(seg.get('shot_ids') or []), speech=bool(spoken_lines(body)), n_images=len(hv))
        problems += [f'正文里写了画幅/分辨率/帧率（这些是接口参数）：{x}' for x in param_sentences(body)]
        if '# design:' in raw_text:
            dfile = run/'plan/swap_design.json'
            if not dfile.is_file(): raise SystemExit(f"{seg['id']}: 提示词引用了设计表，但 {dfile} 不存在（先 swap_design.py observe + decide）")
            problems += sd.check_against_design(raw_text, read(dfile)['shots'])
        if problems:
            raise SystemExit(f"{seg['id']}: 五段式检查没过（references/rules-now.md）：\n- " + '\n- '.join(problems))
        body, rounded = ceil_timestamps(body)
        negatives = negative_sentences(body)
        if negatives:
            raise SystemExit(f"{seg['id']}: 提示词只写正向描述（官方指南：否定只对字幕和音频有效）。把下面这些句子改成“画面里是什么”：\n- " + '\n- '.join(negatives))
        spoons, risks = copy_risks(body)
        if spoons:
            raise SystemExit(f"{seg['id']}: 这些句子要改掉再准备。勺子：提示词里不写任何勺子（controlled-edit.md §4.8 第 2 类，模型照参考片画成竞品量勺），"
                             "原片搅拌写成手端杯轻晃或直接递出、喝；音标 / 斜杠 / 读音讲解：会被模型念出来（§5），品牌名只写 LymphLoop：\n- " + '\n- '.join(spoons))
        for row, advice, sent in risks:
            print(f"{seg['id']}: 照抄风险（{row}）：{advice}\n    “{sent}”", file=sys.stderr)
        foreign = foreign_ingredient_hits(run, body)
        if foreign:
            raise SystemExit(f"{seg['id']}: 提示词里写了 LymphLoop 没有的成分：{', '.join(foreign)}（包装正面只有 "
                             f"{', '.join((read(PRODUCT).get('facts') or {}).get('ingredients_printed_front') or [])}）。改成我们有的成分或删掉这句")
        authored.append((seg, raw_text, body, rounded))
    mutes = parse_mutes(a.mute)
    # leftover caption text the OCR missed (2026-11-22 film 2896957363997251 G04: a pink "IT" stayed at 14.5-15 s while
    # the receipt said 0 frames left). Only caption-sized boxes: a competitor object is never erased (user 2026-11-19).
    erases = parse_erase(getattr(a, 'caption_box', None))
    for gid, boxes in erases.items():
        for x, y, w, h, t0, t1 in boxes:
            if h > CAPTION_BOX_MAX_H:
                raise SystemExit(f'--caption-box {gid}: 高 {h:.2f} 超过一行字幕（≤{CAPTION_BOX_MAX_H}）。只用于参考片里没去干净的字幕文字，'
                                 '不抹竞品、不抹人和手（用户 2026-11-19）')
    unknown = set(erases) - {s['id'] for s in todo}
    if unknown: raise SystemExit(f"--caption-box for a segment not being prepared: {', '.join(sorted(unknown))}")
    syllable_warns = {}
    for seg in todo:
        windows = mutes.get(seg['id'], [])
        rewrites = seg['rewrites']['dialogue'] + seg['rewrites']['vo']
        if rewrites and not windows:
            raise SystemExit(f"{seg['id']}: prepare with --mute for the conflicting words")
        if any(not 0 <= start < end <= seg['duration_s'] for start, end in windows):
            raise SystemExit(f"{seg['id']}: mute window falls outside the source segment")
        if has_onscreen_dialogue(seg):
            for rw in seg['rewrites']['dialogue']:
                chk = agent_native.check_dialogue_syllables(rw.get('original', ''), rw.get('rewrite', ''))
                if chk.get('warning'):
                    syllable_warns.setdefault(seg['id'], []).append(chk)
                    print(f"{seg['id']}: {chk['warning']}（“{rw.get('original')}” -> “{rw.get('rewrite')}”）", file=sys.stderr)
        body_for_seg = next((b for s, _, b, _ in authored if s['id'] == seg['id']), '')
        need, slack = speech_slack(body_for_seg, output_seconds(seg['duration_s']))
        if need and slack > max(3.0, 0.6 * need):
            print(f"{seg['id']}: 台词大约只要 {need} 秒，输出却有 {output_seconds(seg['duration_s'])} 秒，空出 {slack} 秒。空余太多时模型会补词、念糊"
                  "（11-14 四段，品牌名也在这种段里念错）：切段时把没有台词、没有产品的镜头留在原片，或把原句补回来", file=sys.stderr)
        extra = unscripted_reference_words(run, seg, windows, body_for_seg)
        if len(extra) >= 3:
            print(f"{seg['id']}: 参考音轨里还听得到 {len(extra)} 个提示词台词里没有的词（{' '.join(extra[:12])}{' …' if len(extra) > 12 else ''}）。"
                  "模型会把参考片里的人声照着念出来（11-14 九段串词、丢句）：把这些话逐字写进台词，或用 --mute 消掉；整段改写旁白时参考片去掉全部人声，音色用文字写",
                  file=sys.stderr)
        for c in mute_cuts(run, state, seg, windows):
            print(f"{seg['id']}: 消音窗口 {c['window'][0]:.2f}-{c['window'][1]:.2f} 切到了 “{c['word']}” 的{'开头' if c['side'] == 'start' else '结尾'}，"
                  f"{c['left'][0]:.2f}-{c['left'][1]:.2f} 秒还听得到它的一截；留下的这一截会被模型念出来，把窗口放宽到 {c['widen_to'][0]:.2f}-{c['widen_to'][1]:.2f} 再准备",
                  file=sys.stderr)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=REF_WORKERS) as ex:      # every reference prepared up front (cached); LaMa already uses ~12 threads per clip
        # The generator never sees captions or the competitor watermark (user 2026-11-11; official guide: a reference
        # video with captions can defeat "不要字幕"). LaMa over the padded caption lines leaves cloth and skin texture
        # intact; the 11-10 OpenCV box inpaint left smears that edit mode copied, which is why references went out raw
        # for one day.
        clips = list(ex.map(lambda s: reference_clip_hq(run, state, s, plan['fps'], clean=True, upload=True, erase=erases.get(s['id'], ())), todo))
    # the padded tail of a short segment carries the next moment's sound (2026-11-22 film 2836804316692183 G15/G16: half a
    # second of the following line after a 1.5 s segment; the model and the transcript heard words that are not this
    # segment's): everything after the segment's own length is silenced in the uploaded reference
    clips = [muted_reference(c, with_tail(s, mutes.get(s['id'], []))) if with_tail(s, mutes.get(s['id'], [])) else c for s, c in zip(todo, clips)]

    def residue_of(clip):
        try: return reference_residue(run, clip), None
        except Exception as exc: return None, exc               # no network / key here: submit checks again
    with ThreadPoolExecutor(max_workers=6) as ex:              # one Flash call per segment, all at once (was one after another)
        residues = list(ex.map(residue_of, clips))
    prepared = []
    for (seg, raw_text, body, rounded), clip, job_path, (items, residue_error) in zip(authored, clips, job_paths, residues):
        final = run/'prompts'/f"{job_path.stem}.final.txt"
        final.write_text(body)
        header_views = next((l.split(':', 1)[1].strip() for l in raw_text.splitlines() if l.startswith('# views:')), None)
        views = parse_views(a.views if a.views else (header_views if header_views is not None else DEFAULT_VIEWS))
        import swap_design as sd
        carton = 'carton_views' in views
        duration = media.probe(str(clip))['video_duration']
        refs = [{**ref(clip), 'kind': 'video', 'duration_seconds': duration}]
        refs += [{**ref(view_path(run, profile, v)), 'kind': 'image'} for v in views]
        job = {'job_id': job_path.stem, 'run_dir': str(run), 'model': seedance.MODEL,
               'generation_mode': seedance.MODE, 'output_seconds': output_seconds(seg['duration_s']),
               'resolution': seedance.RESOLUTION, 'aspect_ratio': seedance.ASPECT_RATIO,
               'source_segment_duration_seconds': seg['duration_s'], 'source_frames': [seg['in_frame'], seg['out_frame']],
               'prompt_file': str(final),
               'prompt_sha256': sha(final), 'references': refs, 'candidates': 1}
        if erases.get(seg['id']): job['reference_caption_boxes'] = [list(e) for e in erases[seg['id']]]
        if carton and not sd.carton_views_ok(views, raw_text):
            raise SystemExit(f"{seg['id']}: 纸盒两张图一起给，顺序 carton_views,carton_hand（纸盒手持特写例外见 rules-now.md：# carton-close-up）")
        if job_path.stem != f"{seg['id']}-a1":
            job['user_approved_regen'] = {'quote': a.user_quote, 'replaces': regen_previous(run, seg['id'], job_path.stem), 'model': job['model'], 'at': now()}
        try:
            if residue_error: raise residue_error
            job['reference_competitor'] = {'video_sha256': refs[0]['sha256'], 'items': items, 'blocking': list(items or [])}
            for x in unreplaced(body, items or [], seg.get('shot_times')):
                print(f"{seg['id']}: 参考片 {x['t']} 秒处有竞品（{x['kind']} / {x['size']} / {x['state']}，{x['where']}）：它所在镜头那一阶段要写出画面里的"
                      "LymphLoop 纸盒或条包，且【参考素材职责】写“完全移除原片中的…”；这一镜不在设计表里就先 swap_design.py observe --shots S### + decide，提交时会核对", file=sys.stderr)
        except Exception as exc:                                  # no network / key here: submit checks again
            print(f"{seg['id']}: 参考片竞品检查没做成（{exc}），提交时会再查", file=sys.stderr)
        seedance.validate(job)
        sheet = run/'clips'/f"{seg['id']}.upload-{refs[0]['sha256'][:12]}.jpg"
        script('shots.py', 'sheet', clip, '--out', sheet, '--fps', '4', '--max-frames', '32')
        hp = agent_native.scan_highlight_backplates(clip)
        if hp['frames'] > 0:
            print(f"{seg['id']}: 检测到 {hp['frames']} 帧字幕高亮黄条残留（y={hp['y_min']}..{hp['y_max']}），"
                  f"建议提交前用 reference_highlight_repair.py 或 --caption-box 清干净再准备", file=sys.stderr)
        if agent_native.is_native() and mutes.get(seg['id']):
            print(f"{seg['id']}: 含 --mute 消音窗口，付费提交前请直接用 read_file 听 {clip} 确认旧词清零且未切到邻词", file=sys.stderr)
        save(job_path, job)
        prepared.append({'job': str(job_path), 'upload_video': str(clip), 'sheet': str(sheet),
                         'reference_duration_seconds': duration, 'output_seconds': job['output_seconds'],
                         'caption_clean': (read(clip.with_suffix('.json')) if clip.with_suffix('.json').is_file() else {}).get('caption_clean'),
                         **({'highlight_backplate': hp} if hp['frames'] > 0 else {}),
                         **({'syllable_warnings': syllable_warns[seg['id']]} if seg['id'] in syllable_warns else {}),
                         **({'listen_before_submit': str(clip)} if agent_native.is_native() and mutes.get(seg['id']) else {}),
                         **({'timestamps_rounded_up': rounded} if rounded else {})})
    print(json.dumps(prepared, ensure_ascii=False, indent=2))


def regen_attempt(run, gid):
    """Job number for a user-approved regeneration: one after the last job of this segment that has a ledger (a paid
    or finished result). A prepared but unsubmitted a2 keeps its number."""
    nums = {int(m.group(1)): read(p).get('provider', 'apimart') for p in (run/GEN_DIR/'jobs').glob(f'{gid}-a*.json')
            if (m := re.fullmatch(re.escape(gid) + r'-a(\d+)', p.stem))}
    if 'apimart' not in nums.values():
        raise SystemExit(f'{gid}: 还没有生成过，按 a1 正常生成，不用 --user-approved-regen')
    pending = [n for n in range(2, max(nums) + 2) if n not in nums and (run/'jobs'/f'{gid}-a{n}.json').is_file()]
    if pending: return pending[0]                          # prepared, not submitted yet
    for n in sorted(nums, reverse=True):                   # submitted, no result yet: resume it, never a new paid job
        if n == 1 or nums[n] != 'apimart': continue
        led = read(run/GEN_DIR/'jobs'/f'{gid}-a{n}.json')
        if (led.get('job') or {}).get('user_approved_regen') and not any(c.get('status') == 'downloaded' for c in (led.get('candidates') or {}).values()):
            return n
        break
    return max(nums) + 1                                   # numbers already used by local splices are skipped


def regen_previous(run, gid, job_id):
    """The paid generation this one replaces (the latest earlier apimart ledger)."""
    n = int(job_id.rsplit('-a', 1)[1])
    for k in range(n - 1, 0, -1):
        p = run/GEN_DIR/'jobs'/f'{gid}-a{k}.json'
        if p.is_file() and read(p).get('provider', 'apimart') == 'apimart': return p.stem
    return None


def cmd_generate(a):
    """Prepare the local reference inputs, or submit the reviewed inputs once.

    Each target segment gets one paid generation (a1). The only exception is a segment the USER asks to regenerate
    (user 2026-11-19: "把失败的镜头重新生成一遍"): `--user-approved-regen G## --user-quote "原话"` prepares G##-a2
    (a3 ...) with its own prompt file prompts/G##-a2.txt, the same gates, and the user's words in the job."""
    if a.attempt != 1 or a.candidates != 1:
        raise SystemExit('Each target segment allows only one a1 submission and one candidate')
    if a.aspect_ratio not in (None, seedance.ASPECT_RATIO):
        raise SystemExit('Seedance output is fixed to 9:16')
    run, state = load(a.run)
    plan = read(run/'plan/segments.json')
    selected = set(a.segment.split(',')) if a.segment else None
    todo = [s for s in plan['segments'] if selected is None or s['id'] in selected]
    if selected and selected != {s['id'] for s in todo}:
        raise SystemExit('Unknown target segment in --segment')
    if not todo:
        print('[]')
        return
    (run/'jobs').mkdir(exist_ok=True)
    regen = set(getattr(a, 'user_approved_regen', None) or [])
    if regen:
        if not (getattr(a, 'user_quote', '') or '').strip():
            raise SystemExit('--user-approved-regen 要带 --user-quote "用户原话"：第二次付费只在用户明确要求时做，原话记进账本')
        if regen - {s['id'] for s in todo}:
            raise SystemExit('--user-approved-regen 的段要在 --segment 里')
    job_paths = [run/'jobs'/f"{seg['id']}-a{regen_attempt(run, seg['id']) if seg['id'] in regen else 1}.json" for seg in todo]
    if getattr(a, 'refs_only', False):
        clips = prepare_refs(run, state, plan, todo)
        out_refs = {}
        for s, c in zip(todo, clips):
            hp = agent_native.scan_highlight_backplates(c)
            if hp['frames'] > 0:
                print(f"{s['id']}: 检测到 {hp['frames']} 帧字幕高亮黄条残留（y={hp['y_min']}..{hp['y_max']}），请先清理再提交", file=sys.stderr)
                out_refs[s['id']] = {'path': str(c), 'highlight_backplate': hp}
            else:
                out_refs[s['id']] = str(c)
        print(json.dumps(out_refs, ensure_ascii=False, indent=2))
        return
    if a.prepare_only:
        prepare_jobs(a, run, state, plan, todo, job_paths)
        return
    if a.mute or a.views is not None or getattr(a, 'erase', None) or getattr(a, 'caption_box', None):
        raise SystemExit('Set reference options with --prepare-only and review those files before submission')
    for seg, job_path in zip(todo, job_paths):
        if not job_path.is_file():
            raise SystemExit(f'{job_path.stem}: run generate --prepare-only and inspect the upload video first')
        job = read(job_path)
        if job.get('source_frames') != [seg['in_frame'], seg['out_frame']]:
            raise SystemExit(f'{job_path.stem}: the planned source window changed; review the prepared job')
        if not job.get('prompt_sha256') or any(not r.get('sha256') for r in job['references']):
            raise SystemExit(f'{job_path.stem}: prepare and review the Seedance references first')
    left = reference_competitors(run, todo, job_paths, set(a.allow_reference_competitor or []))
    if left:
        raise SystemExit('参考片里看得见竞品、提示词那一阶段却没有我方产品（用户 2026-11-19：不在参考片里抹竞品，模型会照搬抹出的糊块、重塑人脸；换品靠提示词和参考图）：\n- '
                         + '\n- '.join(g + ': ' + '；'.join(f"{x['t']} 秒 {x['kind']}（{x['size']}，{x['state']}，{x['where']}）" for x in rows[:5]) for g, rows in left.items())
                         + '\n这些时间所在镜头不在设计表里就先 swap_design.py observe --shots S### 再 decide（规则表定换成什么）；然后把它写进【生成目标】和【参考素材职责】的'
                         '“完全移除原片中的…”清单，并在那一阶段写出画面里的 LymphLoop 纸盒或条包。原片里特殊的呈现（网页、整箱）可以在 RUN/refs/ 放专门的参考图，# views: 写 run:文件名。'
                         '确实是误报（不是竞品）才加 --allow-reference-competitor G##，并在交付说明写明。')
    cropped = reference_crops(run, state, plan, todo, job_paths, set(a.allow_reference_crop or []))
    if cropped:
        raise SystemExit('参考片把原片里的人裁出了画面（用户 2026-11-14："照原片生成人物"；11-14 batch6 这样裁的段全部换人、换衣服、换场景）：\n- '
                         + '\n- '.join(g + ': ' + '，'.join(f"{x['t']} 秒处 {x['faces_lost']} 张脸在裁切框外" for x in rows[:4]) for g, rows in cropped.items())
                         + '\n参考片保留原画幅里的所有人物和这一镜的主体：中景远景的竞品用同镜头人物都在、只是没有瓶子的帧定格替换那一拍；'
                         '找不到这样的帧就保留原画面，靠提示词把瓶子写成产品（近景大特写这样做都换干净了）。'
                         '裁掉的只是海报、包装上的人像时才加 --allow-reference-crop G##，并在交付说明里写明。')
    # Channel routing: 100x 100x-minimax-h3 (standard, 768P, 9:16) by default (user 2026-11-26);
    # switch to APIMART seedance-2.0 only when channel.txt / LYMPHLOOP_GEN_CHANNEL=apimart
    ch = agent_native.gen_channel()
    if ch == 'x100':
        results = {r['job_id']: r for r in script('x100_gen.py', 'run-batch', *job_paths, '--candidates', a.candidates)}
    else:
        slow = uplink_too_slow(job_paths)
        if slow and not getattr(a, 'ignore_uplink', False):
            raise SystemExit(slow)
        results = {r['job_id']: r for r in script('apimart_wan.py', 'run-batch', *job_paths, '--candidates', a.candidates)}
    commit(run, state, 'generate', jobs=sorted(results))
    print(json.dumps({j: {c: {k: v.get(k) for k in ('status', 'cost_usd', 'cost_credits', 'output_path', 'task_id', 'error', 'generation_submit_count') if v.get(k) is not None}
                          for c, v in r.get('candidates', {}).items()} for j, r in results.items()}, ensure_ascii=False, indent=2))


REPLACEMENT_RE = re.compile(r'将@视频1(?:中|里)[^。]*?(?:更换为|换成|替换为)')
SHOT_LINE_RE = re.compile(r'镜头\s*(\d+)\s*[（(]\s*(\d+)\s*[–-]\s*(\d+)\s*秒')
OUR_PRODUCT_RE = re.compile(r'纸盒|条包|LymphLoop', re.I)


UPLINK_FETCH_S = 30     # APIMART seedance-2.0 gives up fetching a reference after ~35-50 s (2026-11-20 film 2210)


def uplink_too_slow(job_paths):
    """A refusal message when the shared tunnel cannot deliver the biggest reference in UPLINK_FETCH_S at its last
    measured speed, else None. 2026-11-20 film 2210: the tunnel measured 22-80 KB/s, every 1.5-2.2 MB reference was cut
    off ("Failed to probe reference video duration"), two free retries of all 9 segments were spent for nothing."""
    import apimart_wan
    if apimart_wan.VIDEO_HOST != 'tunnel': return None     # references go straight to APIMART (/v1/uploads/videos) since 11-20
    try:
        import hostd
        st = hostd.status() or {}
    except Exception:
        return None
    kbps = st.get('speed_kbps')
    if not kbps: return None
    sizes = []
    for p in job_paths:
        v = next((r['path'] for r in read(p).get('references') or [] if r.get('kind') == 'video'), None)
        if v and Path(v).is_file(): sizes.append(Path(v).stat().st_size)
    if not sizes: return None
    need = max(sizes) / 1024 / float(kbps)
    if need <= UPLINK_FETCH_S: return None
    return (f'托管隧道上行只有 {kbps:.0f} KB/s，最大的参考片 {max(sizes) / 1e6:.1f} MB 要约 {need:.0f} 秒才传完，APIMART seedance-2.0 约 35–50 秒就放弃取片'
            '（2026-11-20 2210 全部 9 段两轮都被拒）。现在提交只会白白用掉免费重提次数。可选：等隧道变快再提交；'
            '或请用户同意改走 100x 直传（x100_gen.py quote 免费报价 → 用户同意 → submit / poll，参数同样是 9:16、720P、按段时长）；'
            '确认要硬提交加 --ignore-uplink。')


def unreplaced(body, items, shot_times=None):
    """Competitor objects in the reference that the prompt does not replace. User 2026-11-19: the competitor stays in
    the reference (erasing it left blurs the model copied and re-shaped faces). Five-section prompts (2026-11-20): the
    【参考素材职责】 says 完全移除原片中的…, and the stage of the shot the object is in names our carton or packet.
    Older prompts: a "将@视频1中…更换为…" sentence on that shot's line."""
    if '【参考素材职责】' in body:
        import swap_design as sd
        st = sd.stages(body)
        removal = '完全移除原片中的' in sd.section(body, '【参考素材职责】')
        out = []
        for x in items:
            t = float(x.get('t') or 0)
            if shot_times: t = min(t, float(shot_times[-1][2]))     # the reference holds its last frame up to the whole second
            idx = [i for i, (_, a, b) in enumerate(shot_times or []) if a - 0.5 <= t <= b + 0.5]
            if shot_times and len(st) == len(shot_times): texts = [st[i][1] for i in idx]
            else: texts = [sd.section(body, '【事件脚本】')]       # more stages than detected shots: no 1:1 map
            if not removal or not any(OUR_PRODUCT_RE.search(re.sub(r'\{[^{}]*\}', '', tx)) for tx in texts): out.append(x)   # a spoken brand name is not a picture
        return out
    shots = []
    for line in body.splitlines():
        m = SHOT_LINE_RE.search(line)
        if m: shots.append((int(m.group(2)), int(m.group(3)), line))
    out = []
    for x in items:
        t = float(x.get('t') or 0)
        lines = [l for a, b, l in shots if a - 1 <= t <= b + 0.5] or ([body] if not shots else [])   # shot starts are rounded UP
        if not any(REPLACEMENT_RE.search(l) for l in lines): out.append(x)
    return out


def reference_competitors(run, todo, job_paths, allowed=()):
    """{G##: [objects]} the generator would copy: competitor objects Flash sees in the uploaded reference video that
    the prompt does not replace (no 将@视频1中…更换为… on that shot's line)."""
    out = {}
    for seg, job_path in zip(todo, job_paths):
        if seg['id'] in allowed: continue
        job = read(job_path)
        video = next((r for r in job.get('references') or [] if r.get('kind') == 'video'), None)
        if not video or not Path(video['path']).is_file(): continue
        rc = job.get('reference_competitor') or {}
        if rc.get('video_sha256') == sha(video['path']): blocking = rc.get('blocking') or []
        else: blocking = list(reference_residue(run, video['path']) or [])
        body = Path(job['prompt_file']).read_text() if job.get('prompt_file') and Path(job['prompt_file']).is_file() else ''
        # a shot the agent reviewed as holding no physical product (a dried drop, a laptop back, a picture on paper:
        # overrides "physical_competitor": false) gets no product; there the 【参考素材职责】 removal is enough
        # (2026-11-22 film 1659373095741717 S024: the drop on the shelf was refused for having no carton next to it)
        import swap_design as sd
        reviewed = {sid for sid, _, _ in seg.get('shot_times') or [] if sd.reviewed_no_product(run, sid)}
        def in_reviewed(x):
            t = float(x.get('t') or 0)
            if seg.get('shot_times'): t = min(t, float(seg['shot_times'][-1][2]))     # the held tail belongs to the last shot
            return any(a - 0.5 <= t <= b + 0.5 and sid in reviewed for sid, a, b in seg.get('shot_times') or [])
        removal = '完全移除原片中的' in body
        blocking = [x for x in unreplaced(body, blocking, seg.get('shot_times')) if not (removal and in_reviewed(x))]
        if blocking: out[seg['id']] = blocking
    return out


def reference_crops(run, state, plan, todo, job_paths, allowed=()):
    """{G##: [moments]} where the uploaded reference video crops a face of the original out of the frame (ref_framing)."""
    from fractions import Fraction
    import ref_framing
    rate = float(Fraction(str(plan.get('fps') or 30))); out = {}
    try: master = working_master(state)
    except (KeyError, TypeError): return {}
    if not Path(str(master)).is_file(): return {}
    for seg, job_path in zip(todo, job_paths):
        if seg['id'] in allowed: continue
        job = read(job_path)
        video = next((r['path'] for r in job.get('references') or [] if r.get('kind') == 'video'), None)
        if not video or not Path(video).is_file(): continue
        dur = (seg['out_frame'] - seg['in_frame']) / rate
        rows = ref_framing.lost_people(master, seg['in_frame'] / rate, dur, video, run/'qa'/'ref_framing'/seg['id'])
        if rows: out[seg['id']] = rows
    return out


def carton_ref_of(job):
    """The first carton image a job sent (carton_views), or None (only carton jobs get the carton geometry check)."""
    return next((r['path'] for r in (job.get('references') or []) if r.get('kind') == 'image' and 'carton' in Path(r['path']).name), None)


def recorded_defects(run, job_id, cid):
    """What the review found, kept with the selection (user 2026-11-11: each segment is generated once and the result is
    used whatever it looks like; the review is the defect record for the delivery notes, not a gate)."""
    qa = run/'qa'/f'{job_id}-{cid}.json'
    if not qa.is_file(): return {'review': 'missing: run swap.py review first if you want the defect record'}
    res = read(qa).get('result') or {}
    return {k: res[k] for k in ('verdict', 'carton_gate', 'carton_geometry', 'carton_findings', 'artifacts', 'findings', 'competitor_residue', 'lip_sync')
            if res.get(k) not in (None, [], '')}


REVIEW_WORKERS = 4   # segments reviewed at once (user 2026-11-19: one at a time took 5+ min per segment, 18 segments ~1 h)


def cmd_review(a):
    """Contact sheets for every candidate + Flash second opinion. The agent reads the sheets and then selects.
    Candidates are reviewed in parallel (REVIEW_WORKERS); every step is cached per file, so a re-run only redoes what
    is missing."""
    run, state = load(a.run)
    plan = {s['id']: s for s in read(run/'plan/segments.json')['segments']}
    fps = read(run/'plan/segments.json')['fps']
    out = {}
    todo = []
    # --segment G01,G02 works like generate's (2026-11-20: a comma list matched no file and review printed {})
    files = sorted({f for g in (a.segment.split(',') if a.segment else ['']) for f in (run/'jobs').glob(f'{g}-a*.json' if g else '*.json')})
    for job_file in files:
        job = read(job_file); seg = plan[job['job_id'].split('-a')[0]]
        ledger_path = run/GEN_DIR/'jobs'/(job['job_id'] + '.json')
        if not ledger_path.is_file(): continue
        for cid, cand in read(ledger_path).get('candidates', {}).items():
            if not cand.get('output_path') or not Path(cand['output_path']).is_file(): continue
            todo.append((job, seg, cid, cand))
    import caption_clean
    caption_clean.scanner(run/'qa'/'caption_scan')       # compile the OCR helper once, before the workers share it
    (run/'qa').mkdir(exist_ok=True)

    def one(item):
        job, seg, cid, cand = item
        sheet = run/'qa'/f"{job['job_id']}-{cid}.png"
        if not sheet.is_file(): script('shots.py', 'sheet', cand['output_path'], '--out', sheet, '--fps', a.fps)
        qa = run/'qa'/f"{job['job_id']}-{cid}.json"
        carton = carton_ref_of(job)
        if qa.is_file() and carton and 'carton_gate' not in (read(qa).get('result') or {}): qa.unlink()   # reviewed before the carton check existed
        flash_error = None
        use_flash = (not a.no_flash) and (not agent_native.is_native())
        if not qa.is_file() and use_flash:
            try:
                script('flash_screen.py', 'qa', '--source', reference_clip_hq(run, state, seg, fps), '--candidate', cand['output_path'], '--prompt', job['prompt_file'],
                       '--out', qa, '--run', run, '--task-kind', 'dialogue' if 'dialogue' in seg['kinds'] else 'visual', *(['--carton-ref', carton] if carton else []))
            except SystemExit as exc:
                # e.g. "Flash returned no JSON object" after its retries: the second opinion is missing for this
                # candidate only; the sheet, caption/overlay gates and word check still run. Re-run review to retry.
                flash_error = str(exc).strip().splitlines()[-1][:500] if str(exc).strip() else 'flash qa failed'
        n_cap, n_all, cap_texts = caption_clean.caption_frames(cand['output_path'], run/'qa'/'caption_scan', [x['text'] for x in seg.get('speech', [])] or None)
        words = None
        rew = seg['rewrites']['dialogue'] + seg['rewrites']['vo']
        if rew and (use_flash or agent_native.is_native()):
            import speech_map
            dur = float(media.probe(cand['output_path'])['video_duration'])
            words = speech_map.word_check(speech_map.transcribe(run, cand['output_path'], 0.0, dur, margin=0.0), rew)
            words['verdict'] = ('words wrong: record the failed word check and review local audio options'
                                if words['missing'] or words['leftover'] else 'words ok')
        ref_clip = next((Path(r['path']) for r in (job.get('references') or []) if r.get('kind') == 'video'), None)
        overlay = None
        if ref_clip and ref_clip.is_file():
            n_ov, n_all2, ov_texts = caption_clean.leftover_overlay_frames(cand['output_path'], ref_clip, run/'qa'/'caption_scan',
                                                                           pattern=comp.fuzzy_pattern(watermark_target(run)))
            overlay = {'frames': n_ov, 'of': n_all2, 'texts': ov_texts,
                       'verdict': 'watermark/title/label left: review local cleanup' if n_ov > max(2, 0.03*n_all2) else 'clean'}
        print(f"review: {job['job_id']}-{cid} done", file=sys.stderr, flush=True)
        return f"{job['job_id']}-{cid}", {'sheet': str(sheet), 'video': cand['output_path'], 'flash': read(qa) if qa.is_file() else None,
                                          **({'agent_watch_and_listen': cand['output_path']} if agent_native.is_native() else {}),
                                          **({'flash_error': flash_error} if flash_error else {}), 'words': words, 'overlay': overlay,
                                          'captions': {'frames': n_cap, 'of': n_all, 'texts': cap_texts,
                                                       'verdict': 'captions left: review local cleanup' if n_cap > max(3, 0.05*n_all) else 'clean'}}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(REVIEW_WORKERS, len(todo)))) as ex:
        for key, row in ex.map(one, todo):
            out[key] = row
    # Local splices (swap.py splice) have no RUN/jobs file: give them a contact sheet so they are looked at before select.
    for ledger_path in sorted((run/GEN_DIR/'jobs').glob(f'{a.segment}-a*.json' if a.segment else '*.json')):
        ledger = read(ledger_path)
        if ledger.get('provider') != 'local_splice': continue
        for cid, cand in ledger.get('candidates', {}).items():
            if not cand.get('output_path') or not Path(cand['output_path']).is_file(): continue
            sheet = run/'qa'/f"{ledger['job_id']}-{cid}.png"; sheet.parent.mkdir(exist_ok=True)
            if not sheet.is_file(): script('shots.py', 'sheet', cand['output_path'], '--out', sheet, '--fps', a.fps)
            out[f"{ledger['job_id']}-{cid}"] = {'sheet': str(sheet), 'video': cand['output_path'], 'flash': None, 'local_splice': ledger.get('note')}
    print(json.dumps(out, ensure_ascii=False, indent=2))


def has_onscreen_dialogue(seg):
    return ('dialogue' in (seg.get('kinds') or []) or bool((seg.get('rewrites') or {}).get('dialogue'))
            or any(line.get('kind') == 'on_screen_dialogue' for line in seg.get('speech', [])))


def generated_audio(seg):
    """Identify speech edits for which the planner can suggest a breath trim."""
    return has_onscreen_dialogue(seg) or bool(seg['rewrites']['vo'])


def spoken_in_window(plan, seg):
    """Every line heard during this segment. A line that starts in the last moments of a segment is listed under the
    NEXT one (with a negative start), and its caption is already on screen here (2026-11-11, Lymphoria G06: the caption
    "IF THAT'S YOU" stayed on the last 9 frames)."""
    from fractions import Fraction
    fps = float(Fraction(str(plan.get('fps') or 30))); a, b = seg['in_frame'] / fps, seg['out_frame'] / fps
    out = []
    for s in plan['segments']:
        base = s['in_frame'] / fps
        out += [x['text'] for x in s.get('speech', []) if x.get('text') and base + x['start'] < b + 0.3 and base + x['end'] > a - 0.3]
    return out


def segment_rewritten(seg):
    return bool((seg.get('rewrites') or {}).get('dialogue') or (seg.get('rewrites') or {}).get('vo'))


def source_sound_file(run, state, plan, seg, candidate):
    """<candidate>.srcaudio.mp4: the generated picture with the source sound of the same moments (sound_sync)."""
    import sound_sync
    from fractions import Fraction
    candidate = Path(candidate); out = candidate.with_name(candidate.stem + '.srcaudio.mp4')
    if not out.is_file() or out.stat().st_mtime < candidate.stat().st_mtime:
        sound_sync.with_source_sound(candidate, working_master(state), seg['in_frame'] / float(Fraction(str(plan.get('fps') or 30))), out)
    return out


def sound_for(run, state, plan, seg, ledger, chosen, a):
    """The selected picture keeps the sound it was generated with, changed line or not (user 2026-11-11; 2026-11-21:
    "这种带有口型同步的替换段，不论有没有改口播，都应该要用生成段自带的音频"). The generated mouths follow the generated voice,
    so any other sound under them comes out early or late. `sound_drift` (generated voice against the SOURCE's timing)
    is only a note: it says nothing about the lips, which follow the generated voice. A voice that does not match its
    own mouths is a lip-sync goof: redo that shot (shot_regen, sound lip_sync), never swap the sound.
    `--sound source` puts the source sound under the picture and is refused on a segment with on-screen dialogue unless
    the agent looked at the whole clip and passes --no-mouth (no speaking mouth visible anywhere in it)."""
    mode = getattr(a, 'sound', None) or 'candidate'
    if a.keep_source_picture or a.audio_only: return {}
    if mode == 'source' and has_onscreen_dialogue(seg) and not getattr(a, 'no_mouth', False):
        raise SystemExit('select --sound source: this segment has on-screen dialogue, and a speaking mouth keeps its own generated sound '
                         '(user 2026-11-21). If you looked at the whole clip and no mouth speaks anywhere, add --no-mouth; otherwise keep '
                         'the generated sound, and redo a shot whose voice does not match its mouth (shot_regen, lip_sync).')
    if ledger.get('provider') == 'local_splice':
        # a spliced attempt keeps the sound its splice gave it (the splice checked it against its pictures)
        return {'sound': 'source', 'path': str(source_sound_file(run, state, plan, seg, chosen))} if mode == 'source' else {}
    try: master = working_master(state)
    except (KeyError, SystemExit): return {}
    if not Path(str(master)).is_file() or not media.probe(str(master)).get('audio') or not media.probe(str(chosen)).get('video'): return {}
    import sound_sync
    from fractions import Fraction
    start = seg['in_frame'] / float(Fraction(str(plan.get('fps') or 30)))
    out = {'sound': mode}
    if seg.get('speech') and media.probe(str(chosen)).get('audio'):
        dur = min(float(media.probe(str(chosen))['video_duration']), float(seg.get('duration_s') or 1e9))
        out['sound_drift'] = sound_sync.drift(master, start, chosen, dur)
    if mode == 'source':
        out['path'] = str(source_sound_file(run, state, plan, seg, chosen))
    elif out.get('sound_drift') and not out['sound_drift']['ok']:
        out['sound_note'] = (f"generated speech is {abs(out['sound_drift']['ms'])} ms {'early' if out['sound_drift']['ms'] < 0 else 'late'} against the "
                             "source's timing: the lines moved, the lips follow the generated voice. Check the last line still ends inside the "
                             'segment, and watch the lips: a voice that does not match its own mouth is a goof to redo (shot_regen lip_sync), not a sound to swap')
        print('select: ' + out['sound_note'], file=sys.stderr)
    return out


def cmd_source_sound(a):
    """Make <candidate>.srcaudio.mp4 for a candidate, to splice from (the cuts then carry the source sound)."""
    run, state = load(a.run)
    plan = read(run/'plan/segments.json'); seg = {s['id']: s for s in plan['segments']}[a.job.split('-a')[0]]
    cand = read(run/GEN_DIR/'jobs'/(a.job + '.json'))['candidates'][a.candidate]
    print(json.dumps({'path': str(source_sound_file(run, state, plan, seg, cand['output_path']))}, ensure_ascii=False))


TAIL_SILENT_DB = -35.0          # a still tail quieter than this is silent whatever Whisper writes (11-22: talk -17 to -28 dB, the false "you" -40.6)
FROZEN_TALK_MINOR_S = 0.4        # voice past the still picture by this much or less: the last syllable, not a talking still


def trim_frozen_tail(clip, clip_in, clip_out, seg_s):
    """(clip_out, record or None). A generated clip copies the held last frame of its padded reference: the picture goes
    still for 0.3-2 s at the end (2026-11-22, user: "生成段的最后都会被定格一下，停顿了一秒多"; 16 of 17 delivered films).
    The still part past the segment's own length is cut off. When the clip's own voice still talks there, the picture
    cannot be cut without cutting the line: it stays, is recorded as a defect, and that shot goes to the next goof round
    (kind speech)."""
    still = media.frozen_tail(clip)
    if still is None or clip_out <= still + 0.04 or clip_out <= seg_s + 0.04: return clip_out, None
    import words as words_mod
    heard = [w for w in words_mod.transcribe(words_mod.load_model(), clip, still, clip_out - still) if w['b'] > w['a']]
    if heard:
        # Whisper's "you" on a quiet tail (2026-11-22 film 1429516565935964 G02: a 1.3 s still tail after "Tastes like
        # orange mango" was kept for a "you" nobody says): words over near-silence do not count
        import numpy as np
        pcm = words_mod.pcm(clip, still, clip_out - still)
        if not len(pcm) or 20 * np.log10(max(1e-6, float(np.sqrt(np.mean(pcm ** 2))))) < TAIL_SILENT_DB: heard = []
    floor = max(clip_in + 0.04, min(clip_out, seg_s))
    if not heard:
        new = round(max(floor, still), 3)
        print(f"select: {clip.name} is a still picture from {still:.2f} s; cut at {new:.2f} s (was {clip_out:.2f})", file=sys.stderr)
        return new, {'still_from': still, 'cut_from': round(clip_out, 3), 'clip_out': new}
    voice_to = round(float(still + max(w['b'] for w in heard)), 3)
    new = round(float(max(floor, min(clip_out, voice_to + 0.15))), 3)
    if voice_to - still <= FROZEN_TALK_MINOR_S:     # the last syllable over a few still frames: cut right after it
        print(f"select: {clip.name} is still from {still:.2f} s, the last word ends at {voice_to:.2f} s; cut at {new:.2f} s (was {clip_out:.2f})", file=sys.stderr)
        return new, {'still_from': still, 'voice_to': voice_to, 'cut_from': round(clip_out, 3), 'clip_out': new}
    print(f"select: {clip.name}: the picture is still from {still:.2f} s while the voice goes on to {voice_to:.2f} s "
          f"({' '.join(w['w'] for w in heard)}). Kept to {new:.2f} s and recorded as a defect: redo this last shot in the "
          f"next goof round (why speech)", file=sys.stderr)
    return new, {'still_from': still, 'voice_to': voice_to, 'talking': ' '.join(w['w'] for w in heard), 'clip_out': new, 'cut_from': round(clip_out, 3)}


def carton_gate(run, seg_id, a, chosen, clip_in, clip_out, prev, audit=None):
    """The measured carton check on a segment whose jobs sent carton images (scripts/carton_check.py; user 2026-11-24:
    "盒装产品比例变形的问题还是被放过了……我想要的是让agent自己能检查出来"). The agent's eyes judge what a thing is; the
    proportions are measured, because a vision model reads a ratio from a small, compressed picture poorly whatever the
    model (batch 62: fronts 1.9-2.0 tall passed as "scale clean", scale_ok=true). A flagged clip is refused until the
    agent has opened the side-by-side sheet (generated box next to the real carton at the same height) and either put the
    goof in goofs.json or written why it is not one (--carton-ok). scale_ok / front_face_ok=true cannot overrule a
    measured flag. A retrim reselect of the same picture keeps an earlier look."""
    import carton_check
    kind = carton_check.segment_kind(run, seg_id)
    if not kind: return None
    out = run/'qa'/f'{a.job}-{a.candidate}.carton.json'; out.parent.mkdir(parents=True, exist_ok=True)
    r = carton_check.check(chosen, clip_in, clip_out, kind == 'mixed', out, work=run/'qa'/'carton_work'/f'{a.job}-{a.candidate}')
    rec = {'kind': kind, 'json': str(out), 'flags': r['flags'], **({'sheet': r['sheet']} if r.get('sheet') else {})}
    if not r['flags']: return rec
    ok = (getattr(a, 'carton_ok', None) or '').strip()
    old = (prev.get('carton_check') or {}).get('ok')
    if not ok and old and (prev.get('job') == a.job or 'retrim' in (a.note or '')): ok = old       # same picture, already looked at
    if not ok:
        lines = '\n'.join(f"  {f['from']:.2f}-{f['to']:.2f} 秒 {f['kind']}（补做原因 {f['goof']}）：{f['what']}" for f in r['flags'])
        said = [k for k in ('scale_ok', 'front_face_ok') if (audit or {}).get(k) is True]
        claim = f"你在 --audit 里填了 {'、'.join(said)}=true，但量出来不对。" if said else ''
        raise SystemExit(f'select {a.job}: {claim}纸盒量出来比例不对（真盒正面高:宽 1.3、侧面 1.86；差四分之一以上算穿帮，用户 2026-11-20 的“大幅度偏差”）：\n'
                         f'{lines}\n先用 read_file 打开 {r.get("sheet")}（中间是生成的盒子，右边是同高的真盒）亲自看。'
                         f'是穿帮：--audit 里对应项填 false，把这几处写进 goofs.json 走人物卡补做（选片仍要放行时加 --carton-ok "穿帮，已列入补做"）；'
                         f'是量错了（开盖盖子立起、条包、手机屏、背景里别的青绿色东西、虚焦）：--carton-ok "看过：<为什么不是穿帮>"。不能不看就放行。')
    rec['ok'] = ok
    return rec


def cmd_select(a):
    run, state = load(a.run)
    sel_path = run/'plan/selections.json'; sel = read(sel_path) if sel_path.is_file() else {}
    plan = read(run/'plan/segments.json')
    seg = {s['id']: s for s in all_segments(plan)}[a.job.split('-a')[0]]
    ledger_path = run/GEN_DIR/'jobs'/(a.job + '.json')
    if a.keep_source_picture and not ledger_path.is_file():
        # A planned segment that was never submitted (Nuflos runs, 2026-11-12: the product was a few pixels in an
        # untouched shot, fixed for free on the master): the original picture and sound stay, nothing was paid.
        entry = {'job': a.job, 'candidate': None, 'picture': 'source', 'generated': False, 'note': a.note, 'selected_at': now()}
        sel[seg['id']] = entry; save(sel_path, sel)
        print(json.dumps(entry, ensure_ascii=False, indent=2)); return
    if not a.candidate: raise SystemExit('select: --candidate is required (only --keep-source-picture on a segment that was never generated goes without)')
    ledger = read(ledger_path); cand = ledger['candidates'][a.candidate]
    if a.audio_only and has_onscreen_dialogue(seg):
        raise SystemExit('select: on-screen dialogue requires the selected picture and its own audio')
    chosen = Path(cand['output_path'])
    sound = sound_for(run, state, plan, seg, ledger, chosen, a)
    if sound.get('path'): chosen = Path(sound['path'])
    info = media.probe(str(chosen))
    end = info['video_duration']
    if a.audio_only:
        if not info['audio']:
            raise SystemExit('select: the candidate has no narration track')
        end = float(media.stream_duration_exact(info, 'audio'))
    clip_in = 0.0 if a.clip_in is None else a.clip_in
    clip_out = end if a.clip_out is None else a.clip_out
    if a.clip_out is None and ledger.get('provider') != 'local_splice' and not a.audio_only \
            and end > float(seg.get('duration_s') or 1e9) + 0.02:
        # The output is generated at a whole number of seconds (the segment's length rounded up, at least 4): what comes
        # after the segment's own length is usually the held last frame of the padded reference. However, 100x-minimax-h3
        # often paces speech slightly slower (~0.4-0.9 s past seg duration_s), so auto-extend clip_out to protect any
        # trailing spoken word before trim_frozen_tail runs (2026-11-26 H3 test film 1807310670419734).
        seg_s = float(seg['duration_s'])
        clip_out = seg_s
        if not a.keep_source_picture and info.get('audio') and spoken_in_window(plan, seg) and end > seg_s + 0.05:
            try:
                import words as words_mod
                scan_from = max(clip_in, seg_s - 0.8)
                tail_w = [w for w in words_mod.transcribe(words_mod.load_model(), str(chosen), scan_from, end - scan_from) if w.get('b', 0) > w.get('a', 0)]
                if tail_w:
                    last_end = scan_from + float(tail_w[-1]['b'])
                    if last_end > seg_s:
                        ext = min(end, round(last_end + 0.18, 3))
                        if ext > clip_out:
                            print(f"select: {chosen.name} last word ends at {last_end:.2f} s (> seg {seg_s:.2f} s); auto-extended clip_out to {ext:.2f} s", file=sys.stderr)
                            clip_out = ext
            except Exception:
                pass
    if not 0 <= clip_in < clip_out <= end + 1e-6:
        raise SystemExit('select: the selected interval is outside the candidate')
    tail = None
    if not (a.audio_only or a.keep_source_picture or getattr(a, 'keep_frozen_tail', False)):
        clip_out, tail = trim_frozen_tail(chosen, clip_in, clip_out, float(seg.get('duration_s') or 0))
    entry = {'job': a.job, 'candidate': a.candidate, 'media': ref(chosen), 'clip_in': clip_in,
             'clip_out': clip_out, 'note': a.note, 'selected_at': now(), 'defects': recorded_defects(run, a.job, a.candidate)}
    if getattr(a, 'keep_frozen_tail', False): entry['keep_frozen_tail'] = True      # recut and retrim leave this one's still end
    if tail:
        entry['frozen_tail'] = tail
        if tail.get('talking'):
            entry['defects'] = list(entry['defects'] or []) + [f"frozen_tail_speech {tail['still_from']:.2f}-{tail['voice_to']:.2f}s"]
    entry.update({k: v for k, v in sound.items() if k != 'path'})
    if not (a.keep_source_picture or a.audio_only or a.no_caption_clean):
        # Seedance keeps burned-in captions now and then (11-10 G09, G14) and the reference is no longer cleaned: clean
        # the spoken-word caption boxes off the generated picture itself (free, deterministic; the packet print is kept).
        import caption_clean
        spoken = spoken_in_window(plan, seg) or None
        # No speech in the segment means no spoken-word captions: nothing is cleaned (2026-11-11, a silent segment had
        # 158 frames of packet and shelf print inpainted because every centred text box counted).
        n_cap, n_all, _ = caption_clean.caption_frames(chosen, run/'qa'/'caption_scan', spoken) if spoken else (0, 0, [])
        if n_cap > max(3, 0.05 * n_all):
            v = info['video']; out = chosen.with_name(chosen.stem + '.decaptioned.mp4')
            report = caption_clean.clean(chosen, out, run/'qa'/'caption_work', short_edge=min(int(v['width']), int(v['height'])),
                                         keep_audio=True, spoken=spoken)
            entry.update(media=ref(out), caption_clean={'from': str(chosen), 'frames_cleaned': report['frames_cleaned'],
                                                        'caption_frames_found': n_cap, 'texts': report['caption_texts'][:8]})
    if a.keep_source_picture: entry['picture'] = 'source'
    if a.audio_only: entry.update(picture='source', audio='candidate')  # narration-only rewrite: source picture, Seedance's re-voiced line
    audit = agent_native.parse_audit(getattr(a, 'audit', None))
    if not (a.keep_source_picture or a.audio_only):
        cc = carton_gate(run, seg['id'], a, chosen, clip_in, clip_out, sel.get(seg['id']) or {}, audit)
        if cc:
            entry['carton_check'] = cc
            if cc.get('flags'): entry['defects'] = list(entry['defects'] if isinstance(entry['defects'], list) else [entry['defects']]) + \
                [f"carton_{f['kind']} {f['from']:.2f}-{f['to']:.2f}s {f['worst']}（看过放行：{cc.get('ok')}）" for f in cc['flags']]
    if audit:
        fails = agent_native.audit_failures(audit)
        if fails and not getattr(a, 'allow_audit_fail', False):
            raise SystemExit(f"select: 看听 6 项穿帮自检未通过（{', '.join(fails)}）：不要放过，把穿帮写进 goofs.json 走 shot_regen.py round open 补做；若确需保留并写进交付说明，加 --allow-audit-fail")
        entry['audit'] = audit
    sel[a.job.split('-a')[0]] = entry; save(sel_path, sel)
    print(json.dumps(entry, ensure_ascii=False, indent=2))

# ---------------------------------------------------------------- captions back on the generated segments
CAPTION_WORKERS = 4
CAPTION_COPY_MIN_COVER = 0.7     # share of the said words a copied-captions result must show, else every line is drawn


def cmd_captions(a):
    """Captions on every selected generated segment, in the film's own caption style and place (user, 2026-11-13).
    Run after select (and after any splice), before recut; re-running starts again from the uncaptioned clip. A clip
    that already carries captions (a source piece spliced back in) and a segment whose source picture is kept are left
    alone. Look at RUN/captions/style-preview.jpg and the printed sheets before recut."""
    run, state = load(a.run)
    import caption_burn as cb, caption_clean
    style_path = run/'captions/style.json'
    sel_path = run/'plan/selections.json'; sel = read(sel_path); plan = read(run/'plan/segments.json')
    # only this film's own generated segments get captions here: segments reused from a donor film carry the donor's
    # captioned clip, and kept source pictures their own captions (2026-11-22 film 1106373148453803: all 7 generated
    # segments reused, 2 kept; captions stopped on "no captions to imitate" with nothing to caption)
    own = [g for g in plan['segments'] if (sel.get(g['id']) or {}).get('picture') != 'source' and (sel.get(g['id']) or {}).get('job')]
    if not own and not a.segment:
        commit(run, state, 'captions', segments=[], skipped='no own generated segment: reused segments keep the donor film\'s captions')
        print(json.dumps({'captions': 'skipped', 'why': 'no own generated segment (reused segments keep their captions, kept source pictures their own)'}, ensure_ascii=False)); return
    style = cb.measure(run, font_name=a.font) if (a.remeasure or a.font or not style_path.is_file()) else read(style_path)
    all_lines = [x['text'] for seg in plan['segments'] for x in seg.get('speech', [])] + [l for f in sorted((run/'prompts').glob('G*-a*.txt')) for l in cb.prompt_lines(f.read_text())]
    report = {}
    import caption_matte
    pattern = re.compile(comp.fuzzy_pattern(watermark_target(run)))
    drop_text = lambda t: bool(pattern.search(re.sub(r'[^A-Z]', '', t.upper())))
    brands = {w for b in ((comp.active(run) or {}).get('brand_names') or []) for w in caption_matte.norm_tokens(b)}

    def old_words(seg):
        out = set(brands)
        rw = seg.get('rewrites') or {}
        for r in (rw.get('dialogue') or []) + (rw.get('vo') or []):
            out |= set(caption_matte.norm_tokens(r['original'])) - set(caption_matte.norm_tokens(r['rewrite']))
        return out

    def brands_ok(seg):
        """Words of an old line that are also in its new line (kept words are not stale)."""
        rw = seg.get('rewrites') or {}
        return {w for r in (rw.get('dialogue') or []) + (rw.get('vo') or []) for w in caption_matte.norm_tokens(r['rewrite'])}

    def frames_match(entry, gid, job=None, depth=0):
        """The selected clip's frames are the generated frames of this segment from frame 0 (a splice that only muted
        or re-sounded them counts; one that moved pictures does not). A splice built on an earlier splice keeps the
        timing only if that one did too (2026-11-22 film 2896957363997251 G05: a3 refitted one shot into a2 in place,
        but a2 had put a redone shot back at its own length; the film's captions came ~2 s early)."""
        job = job or entry.get('job') or ''
        ledger = run/GEN_DIR/'jobs'/(job + '.json')
        if not ledger.is_file() or read(ledger).get('provider') != 'local_splice': return True
        pics = (read(ledger).get('recipe') or {}).get('pictures') or []
        if not splice_keeps_timing(pics, gid) or depth > 8: return False
        bases = {re.sub(r'-c\d+$', '', str(q.get('source', ''))) for q in pics if re.match(re.escape(gid) + r'-a\d+-c\d+$', str(q.get('source', '')))}
        return all(frames_match(entry, gid, b, depth + 1) for b in bases if b != job)
    names = set(cb.names_in([re.sub(r"(^|[.?!]\s+)[A-Z]", lambda m: m.group(0).lower(), t) for t in all_lines]))   # capitals that are not sentence starts

    # Only segments whose inputs changed are captioned again (user 2026-11-22: a round of goof redos changes a few
    # segments; the chain used to re-hear, re-copy and re-encode every segment each time). The key is what the result
    # depends on: the uncaptioned clip, the style, the lines, the replaced words, the window, the code.
    code_sha = hashlib.sha256(b''.join((HERE/f).read_bytes() for f in ('caption_burn.py', 'caption_matte.py'))).hexdigest()
    style_sha = hashlib.sha256(json.dumps(style, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    def inputs_sha(seg, s, base, spoken):
        return hashlib.sha256(json.dumps([sha(base), style_sha, spoken, sorted(old_words(seg)), sorted(brands), s.get('clip_in'), s.get('clip_out'),
                                          bool(getattr(a, 'draw_all', False)), frames_match(s, seg['id']), code_sha], ensure_ascii=False, default=str).encode()).hexdigest()

    def unchanged(s, sig):
        if a.segment or getattr(a, 'force', False): return False
        if s.get('captions_inputs_sha256') != sig: return False
        if not s.get('captions'): return True                      # checked before: nothing to draw on this clip
        m = s.get('media') or {}
        return bool(s.get('media_uncaptioned')) and Path(m.get('path', '')).is_file() and sha(m['path']) == m.get('sha256')

    def one(seg):
        """(gid, report entry) for one segment; mutates its own selection entry only."""
        gid = seg['id']; s = sel.get(gid)
        if not s: return gid, 'no selection yet'
        if s.get('picture') == 'source': return gid, 'source picture kept (it has its own captions)'
        base = Path((s.get('media_uncaptioned') or s['media'])['path'])
        jid = s.get('job') if str(s.get('job') or '').startswith(gid + '-a') else gid + '-a1'
        prompt = next((f for f in (run/'prompts'/f'{jid}.final.txt', run/'prompts'/f'{jid}.txt', run/'prompts'/f'{gid}-a1.final.txt') if f.is_file()), None)
        lines = cb.prompt_lines(prompt.read_text()) if prompt else []
        spoken = lines or [x['text'] for x in seg.get('speech', [])]
        sig = inputs_sha(seg, s, base, spoken)
        if unchanged(s, sig): return gid, {'unchanged': True, 'method': (s.get('captions') or {}).get('method') or 'no captions on this clip'}
        s['captions_inputs_sha256'] = sig
        if not spoken:                                          # no speech, but the film may show a title card here
            final = base.with_name(base.stem + '.captioned.mp4')
            out = final.with_name(final.stem + '.part.mp4')     # written aside, renamed when complete (11-19: a stopped run left an 8-frame file)
            rec = None if getattr(a, "draw_all", False) else caption_matte.apply(run, seg, s, base, out, style, [], old_words(seg), drop_text,
                                                              {'caps': [], 'heard': []}, frames_match=frames_match(s, gid), banned=frozenset(brands))
            if rec is None or not rec.get('frames_copied'):
                if out.is_file(): out.unlink()
                return gid, 'no lines in this segment'
            oscompat.replace(out, final); out = final
            s['media_uncaptioned'] = ref(base); s['media'] = ref(out); s['captions'] = {'style': str(style_path), **rec}
            return gid, {'captions': [x['text'] for x in rec['events']], 'method': rec['method']}
        n_cap, n_all, _ = caption_clean.caption_frames(base, run/'qa'/'caption_scan', spoken)
        if n_cap > 0.15 * n_all:
            s.pop('captions', None); s['media'] = s.get('media_uncaptioned') or s['media']; s.pop('media_uncaptioned', None)
            return gid, f'already carries captions ({n_cap} of {n_all} frames): left as it is'
        fallback_note = None
        caps, heard, unheard = cb.captions_for(base, spoken, {**style, 'proper_nouns': sorted(set(style.get('proper_nouns') or []) | names)},
                                               float(s['clip_in']), float(s['clip_out']))
        final = base.with_name(base.stem + '.captioned.mp4')
        out = final.with_name(final.stem + '.part.mp4')         # written aside, renamed when complete
        # First choice: the film's own captions and title cards, copied pixel for pixel (user 2026-11-19: every film's
        # captions look different); only lines with a replaced word are drawn new. Falls back to drawing every line.
        rec = None if getattr(a, "draw_all", False) else caption_matte.apply(run, seg, s, base, out, style, spoken, old_words(seg), drop_text,
                                                          {'caps': caps, 'heard': heard}, frames_match=frames_match(s, gid), banned=frozenset(brands))
        if rec is not None and rec.get('method') == 'copied from the film' and caps:
            # the copy only brings back the captions the reference's OCR found; when most of what is said is missing
            # (2026-11-22 film 1912538379721657 G01: "Four herbs" and "Cleavers sweeps it all out" never appeared), every
            # line is drawn in the measured style instead
            shown = caption_matte.norm_tokens(' '.join([str(e.get('text') or '') for e in rec.get('events', [])] + [str(d) for d in rec.get('drawn', [])]))
            said = caption_matte.norm_tokens(' '.join(' '.join(c['lines']) for c in caps))
            cover = sum(1 for w in said if w in set(shown)) / max(1, len(said))
            if cover < CAPTION_COPY_MIN_COVER:
                if out.is_file(): out.unlink()
                rec = None; fallback_note = f'copy showed {cover:.0%} of the words said; drawn instead'
        if rec is None:
            if not caps: return gid, 'nothing was heard in the selected clip'
            rec = cb.burn(base, out, caps, style, centre_y=(style.get('centre_y_by_segment') or {}).get(gid)); rec['method'] = 'drawn in the measured style'
            if fallback_note: rec['fallback'] = fallback_note
        oscompat.replace(out, final); out = final
        s['media_uncaptioned'] = ref(base); s['media'] = ref(out)
        s['captions'] = {'style': str(style_path), 'events': caps, 'heard': ' '.join(h['w'] for h in heard), 'in_prompt_but_not_said': unheard, **rec}
        # only what is on screen: lines drawn new, or every caption when drawn in the measured style (copied events keep the
        # film's own text of lines without a replaced word; their listed text is the original caption, not what is shown)
        drawn_text = ' '.join([str(d) for d in rec.get('drawn', [])] if rec.get('method') == 'copied from the film' else [' / '.join(c['lines']) for c in caps])
        stale = sorted(w for w in set(caption_matte.norm_tokens(drawn_text)) & (old_words(seg) - brands_ok(seg)) if len(w) >= 4)   # 'de', 'la' are in every line
        if stale:      # 2026-11-20 film 2210 G06: the captions said "unas gotas" while the clip said "unos sobres"
            print(f"{gid}: 字幕里还有被替换的旧词 {stale}：检查提示词台词（{{原词}}）和这一段的 rewrites", file=sys.stderr)
            s['captions_stale_words'] = stale
        sheet = run/'qa'/f"{gid}-captioned.png"
        script('shots.py', 'sheet', out, '--out', sheet, '--fps', '2')
        shown = ([f"{x['t']}s {x['text']} — {x['how']}" for x in rec['events']] + [f'drawn: {d}' for d in rec.get('drawn', [])]
                 if rec.get('method') == 'copied from the film' else [' / '.join(c['lines']) for c in caps])
        return gid, {'method': rec.get('method'), 'captions': shown, **({'in_prompt_but_not_said': unheard} if unheard else {}), 'sheet': str(sheet)}

    caption_clean.scanner(run/'qa'/'caption_scan')              # compile the OCR helper once, before the workers share it
    todo = [seg for seg in plan['segments'] if not a.segment or seg['id'] == a.segment]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=CAPTION_WORKERS) as ex:   # one segment after another took 35 min (11-19)
        for gid, entry in ex.map(one, todo): report[gid] = entry
    save(sel_path, sel)
    done = sorted(g for g, r in report.items() if isinstance(r, dict))
    commit(run, state, 'captions', segments=done, style=ref(style_path), redone=sorted(g for g in done if not report[g].get('unchanged')))
    print(json.dumps({'style': {k: style[k] for k in ('font', 'font_fit', 'size', 'centre_y', 'max_line_width', 'fill', 'stroke')},
                      'style_preview': str(run/'captions/style-preview.jpg'), 'segments': report,
                      'next': 'look at the style preview and each sheet (words, place, size), then swap.py recut'}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- splice (several attempts each right in part)
SPLICE_FADE = 0.02   # s: crossfade at the edges of a pasted audio window / ramp around a mute window


DERIVED_SUFFIXES = ('.decaptioned', '.captioned', '.srcaudio')   # files the skill itself writes next to a generated result


def splice_keeps_timing(pics, gid):
    """True when a splice keeps the timing of one generation of this segment, so the film's own captions can still be
    copied: a single picture from frame 0 of one of this segment's generations, or that generation with single shots
    replaced in place (user 2026-11-19: shots regenerated one by one and fitted back into their own frames), i.e.
    every piece of the base clip sits at its own frame position and each replacement exactly fills the gap."""
    own = lambda p: str(p.get('source', '')).startswith(gid + '-')
    if len(pics) == 1: return pics[0].get('frames_inclusive', [1])[0] == 0 and own(pics[0])
    base = [p for p in pics if re.match(re.escape(gid) + r'-a\d+-c\d+$', str(p.get('source', '')))]
    if not base or len({p['source'] for p in base}) != 1 or not all(own(p) for p in pics): return False
    pos = 0
    for i, p in enumerate(pics):
        f0, f1 = p['frames_inclusive']
        if p in base:
            if f0 != pos: return False
            pos = f1 + 1
        elif p.get('fit'): pos += int(p['fit'])
        else:
            # a piece at its own length (a redone shot put back ripple, 2026-11-22): the timing holds only when it happens
            # to fill the gap it replaced; otherwise what follows has moved and the film's captions no longer line up
            # (film 1603578481269031 G05: "Write this down" copied at 8.2 s, said at 9.9 s)
            nxt = next((q for q in pics[i + 1:] if q in base), None)
            n = f1 - f0 + 1
            if not nxt: return False                  # a redone tail at its own length: nothing tells it kept the timing (160 G05-a3)
            if abs(nxt['frames_inclusive'][0] - pos - n) > 2: return False
            pos = nxt['frames_inclusive'][0] if nxt else pos + n
    return True


def ledger_outputs(run):
    """{resolved output path: candidate} for every candidate in RUN/generation/jobs (paid results and local splices)."""
    out = {}
    for j in sorted((run/GEN_DIR/'jobs').glob('*.json')):
        try: cands = read(j).get('candidates', {})
        except Exception: continue
        for c in cands.values():
            if c.get('output_path'): out[str(Path(c['output_path']).resolve())] = c
    return out


def picture_origin(run, path, outputs=None):
    """The ledger candidate output a selected picture comes from, following only the skill's own derivatives
    (select's .decaptioned, captions' .captioned, source-sound's .srcaudio, written in the same folder), else None.
    A file Codex built itself (2026-11-14 NV0560: original picture zoomed 6x to hide the bottle, another segment's
    generated picture cropped in) has no origin."""
    outputs = ledger_outputs(run) if outputs is None else outputs
    p = Path(path).resolve()
    while True:
        c = outputs.get(str(p))
        if c is not None:
            return p if not c.get('output_sha256') or sha(p) == c['output_sha256'] else None
        for suf in DERIVED_SUFFIXES:
            if p.stem.endswith(suf): p = p.with_name(p.stem[:-len(suf)] + p.suffix); break
        else: return None


def original_masters(state):
    """The original film as the skill prepared it (full frame, untouched picture): what a splice may borrow from."""
    st = state.get('stages', {})
    return {str(Path(x['path']).resolve()) for x in (st.get('prepare', {}).get('cfr_master'), st.get('prepare', {}).get('working_master'),
                                                    st.get('watermark', {}).get('working_master')) if x and x.get('path')}


def splice_source(run, name):
    """A splice input: a generated result of this run (by path, RUN/generation/results/<name>.mp4, or <job>-<cid> through
    its ledger) or the original film itself. Any other file is refused (user 2026-11-14: no patches, no zoom-crops on
    generated segments; NV0560 G01/G05 spliced in a 6x zoom of the original and a crop of another segment's picture)."""
    p = Path(name).expanduser()
    if p.suffix and p.is_file():
        p = p.resolve()
        _, state = load(run)
        if picture_origin(run, p) is None and str(p) not in original_masters(state):
            raise SystemExit(f'splice source {p} is not a generated result of this run or the original film: '
                             '自己做的放大、裁切、贴图、重取景的片子不能接进生成段（用户 2026-11-14 禁止贴图和放大裁切补救）。'
                             '只能用本段生成结果的整帧、原片整帧；救不了就报告用户决定是否付费重生成。')
        return p
    q = run/GEN_DIR/'results'/(name + '.mp4')
    if q.is_file(): return q
    m = re.fullmatch(r'(.+-a\d+)-(c\d+)', name)
    if m and (run/GEN_DIR/'jobs'/(m[1] + '.json')).is_file():
        c = read(run/GEN_DIR/'jobs'/(m[1] + '.json')).get('candidates', {}).get(m[2], {})
        if c.get('output_path') and Path(c['output_path']).is_file(): return Path(c['output_path'])
    raise SystemExit(f'splice source not found: {name} (tried the path, {q}, and the ledger {m[1] if m else name})')


def _span(text, what):
    try:
        a, b = (float(x) for x in text.split('-', 1))
    except ValueError:
        raise SystemExit(f'{what}: expected A-B, got {text!r}') from None
    if not b > a >= 0: raise SystemExit(f'{what}: need 0 <= A < B, got {text!r}')
    return a, b


def _gain_expr(windows, inside):
    """Volume expression that is 0 inside each window and 1 outside, with SPLICE_FADE linear ramps (inside=True: the
    ramps sit inside the window, for a crossfade with a pasted piece; False: outside it, so [a,b] is fully silent)."""
    f = SPLICE_FADE; terms = []
    for a, b in windows:
        lo, hi = (a, b) if inside else (a - f, b + f)
        terms.append(f"(1-clip(min((t-{lo:.4f})/{f},({hi:.4f}-t)/{f}),0,1))")
    return '*'.join(terms) or '1'


def _same_media(p, q):
    """A clip and its captioned / decaptioned copies carry the same picture timing and sound."""
    norm = lambda x: re.sub(r'\.(de)?captioned$', '', Path(str(x)).stem)
    return Path(str(p)).parent == Path(str(q)).parent and norm(p) == norm(q) or Path(str(p)).resolve() == Path(str(q)).resolve()


def foreign_sound(pieces, rate, hold, audio, windows):
    """Output-time spans [t0, t1) where the sound is not the picture's own sound of the same moment (user 2026-11-21:
    "带有口型同步的替换段……都应该要用生成段自带的音频"; Codex 2026-11-20 batches: 40 of 59 audio windows were laid over
    another picture or shifted 0.2-1.5 s against their own mouths). pieces: [{'path', 'start_s', 'out_frames', 'fit'}] in
    order; audio: path of a sound laid from 0 s, or None (each piece with its own); windows: [{'path', 'from': [a, b], 'at'}].
    A retimed piece (fit) has no own sound, so any sound over it is foreign; the held tail is foreign when a laid sound
    runs under it. Muted windows are not checked here."""
    rate = float(rate); tol = 1.5 / rate
    spans, t = [], 0.0
    for pc in pieces:
        d = pc['out_frames'] / rate; spans.append((t, t + d, pc)); t += d
    total = t + hold / rate
    cuts = {0.0, total, *[x for a0, b0, _ in spans for x in (a0, b0)]}
    for w in windows: cuts |= {w['at'], w['at'] + w['from'][1] - w['from'][0]}
    cuts = sorted(c for c in cuts if 0 <= c <= total)
    bad = []
    for a0, b0 in zip(cuts, cuts[1:]):
        if b0 - a0 < 1e-6: continue
        m = (a0 + b0) / 2
        pic = next((x for x in spans if x[0] <= m < x[1]), None)
        layer = [(w['path'], w['from'][0] - w['at']) for w in windows if w['at'] <= m < w['at'] + w['from'][1] - w['from'][0]]
        if not layer:
            if audio is None:
                continue                                   # each piece with its own sound (a held tail: silence)
            layer = [(audio, 0.0)]
        if pic is None: bad.append([a0, b0]); continue     # the held last frame under a laid sound
        t0, _, pc = pic
        own = not pc.get('fit') and all(_same_media(path, pc['path']) and abs(off - (pc['start_s'] - t0)) <= tol for path, off in layer)
        if not own: bad.append([a0, b0])
    merged = []
    for x in bad:
        if merged and x[0] - merged[-1][1] <= tol: merged[-1][1] = x[1]
        else: merged.append(list(x))
    return [[round(a0, 3), round(b0, 3)] for a0, b0 in merged if b0 - a0 > tol]


def cmd_splice(a):
    """Join selected frame ranges and their matching sound. Explicit audio overrides are for off-screen coverage."""
    import math
    run, state = load(a.run)
    segs = {x['id']: x for x in read(run/'plan/segments.json')['segments']} if (run/'plan/segments.json').is_file() else {}
    if a.segment not in segs: raise SystemExit(f'unknown segment {a.segment} (plan/segments.json)')
    n = a.out_attempt.lstrip('a')
    if not n.isdigit(): raise SystemExit('--out-attempt is aN (e.g. a7)')
    job_id = f'{a.segment}-a{int(n)}'
    ledger_path = run/GEN_DIR/'jobs'/(job_id + '.json'); out = run/GEN_DIR/'results'/(job_id + '-c1.mp4')
    taken = [x for x in (ledger_path, out, run/'jobs'/(job_id + '.json')) if x.exists()]
    if taken and not a.force: raise SystemExit(f'{job_id} already exists ({", ".join(map(str, taken))}); pick another --out-attempt')
    if a.force and ((run/'jobs'/(job_id + '.json')).exists() or (ledger_path.is_file() and read(ledger_path).get('provider') != 'local_splice')):
        raise SystemExit(f'{job_id} belongs to a real generation; never overwrite it')
    pieces = []
    for spec in a.picture:
        name, _, rng = spec.rpartition(':')
        if not name: raise SystemExit(f'--picture SRC:A-B, got {spec!r}')
        rng, _, fit = rng.partition('@')     # SRC:A-B@N: those frames fitted into N output frames (a shot generated from a slowed reference, 11-19)
        try: f0, f1 = (int(x) for x in rng.split('-', 1)); fit = int(fit) if fit else None
        except ValueError: raise SystemExit(f'--picture frames must be integers A-B, got {spec!r}') from None
        path = splice_source(run, name); info = media.probe(str(path), include_frames=True)
        if not 0 <= f0 <= f1 < info['frame_count']:
            raise SystemExit(f'{spec}: frames {f0}-{f1} outside {path.name} (0-{info["frame_count"] - 1})')
        pieces.append({'source': name, 'path': path, 'frames': [f0, f1], 'info': info, 'fit': fit})
    first = pieces[0]['info']; fps = first['fps']; W, H = int(first['video']['width']), int(first['video']['height'])
    rate = media.ratio(fps)
    hold = max(0, a.hold_last or 0)
    for pc in pieces:
        source_rate = media.ratio(pc['info']['fps'])
        first_frame, last_frame = pc['frames']
        pc['start_s'], pc['end_s'] = first_frame/source_rate, (last_frame+1)/source_rate
        pc['out_frames'] = pc['fit'] or math.ceil((pc['end_s'] - pc['start_s']) * rate)
    if any(pc['fit'] for pc in pieces) and not (a.audio or a.audio_window):
        raise SystemExit('a retimed piece (SRC:A-B@N) has no usable sound of its own: give --audio (the segment\'s clip) or --audio-window covering it')
    total = sum(p['out_frames'] for p in pieces) + hold
    dur = float(total / rate)
    audio_path = splice_source(run, a.audio) if a.audio else None
    windows = []
    for spec in a.audio_window or []:
        name, _, rest = spec.rpartition(':')
        if not name or '@' not in rest: raise SystemExit(f'--audio-window SRC:A-B@T, got {spec!r}')
        rng, _, at = rest.partition('@'); w0, w1 = _span(rng, '--audio-window'); t = float(at)
        if t + (w1 - w0) > dur + 1e-6: raise SystemExit(f'{spec}: window ends at {t + w1 - w0:.3f} s, after the splice ({dur:.3f} s)')
        windows.append({'source': name, 'path': splice_source(run, name), 'from': [w0, w1], 'at': t})
    mutes = [_span(x, '--mute-window') for spec in (a.mute_window or []) for x in spec.split(',')]
    foreign = foreign_sound([{'path': pc['path'], 'start_s': float(pc['start_s']), 'out_frames': pc['out_frames'], 'fit': pc['fit']} for pc in pieces],
                            float(rate), hold, audio_path, windows)
    no_mouth = [list(_span(x, '--no-mouth')) for spec in (getattr(a, 'no_mouth', None) or []) for x in spec.split(',')]
    # touching windows count as one (2026-11-22 film 2114607189446748 round 3: 1.5-3.0, 3.0-6.0, 6.0-6.208 were refused for
    # the foreign span 1.50-6.21 that runs across all three)
    merged = []
    for n0, n1 in sorted(no_mouth):
        if merged and n0 <= merged[-1][1] + 1.5 / float(rate): merged[-1][1] = max(merged[-1][1], n1)
        else: merged.append([n0, n1])
    uncovered = [f for f in foreign if not any(n0 - 1.5 / float(rate) <= f[0] and f[1] <= n1 + 1.5 / float(rate) for n0, n1 in merged)]
    if uncovered:
        raise SystemExit('splice: at ' + ', '.join(f'{x:.2f}-{y:.2f}s' for x, y in uncovered) + ' of the new clip the sound is not the picture\'s own '
                         'sound of the same moment (another clip, the source, or shifted). A speaking mouth keeps its own generated sound '
                         '(user 2026-11-21): redo that shot with lip sync (shot_regen, sound lip_sync) or splice each picture with its own '
                         'audio. Only where you looked and no mouth speaks (narration, back of a head, an insert), pass --no-mouth A-B (seconds of the new clip).')
    inputs = []
    def inp(path):
        path = str(path)
        if path not in inputs: inputs.append(path)
        return inputs.index(path)
    graph = []; afmt = 'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo'
    for i, pc in enumerate(pieces):
        k = inp(pc['path']); v = pc['info']['video']; chain = [f"trim=start_frame={pc['frames'][0]}:end_frame={pc['frames'][1] + 1}", 'setpts=PTS-STARTPTS']
        if (int(v['width']), int(v['height'])) != (W, H): chain.append(f'scale={W}:{H}:flags=lanczos')
        if pc['fit']:
            nk = pc['frames'][1] - pc['frames'][0] + 1
            factor = (pc['fit'] / float(rate)) / (nk / float(media.ratio(pc['info']['fps'])))
            chain.extend([f'setpts=PTS*{factor:.6f}', f'fps={fps}:start_time=0', 'tpad=stop_mode=clone:stop=2', f'trim=end_frame={pc["fit"]}'])
        elif media.ratio(pc['info']['fps']) != rate:
            chain.extend([f'fps={fps}:start_time=0', 'tpad=stop_mode=clone:stop=1', f'trim=end_frame={pc["out_frames"]}'])
        graph.append(f"[{k}:v:0]{','.join(chain + ['setsar=1', 'format=yuv420p'])}[v{i}]")
    # Normalize concat timestamps/rate before frame-count padding (FFmpeg 8).
    graph.append(''.join(f'[v{i}]' for i in range(len(pieces))) + f'concat=n={len(pieces)}:v=1:a=0,setpts=N/({rate}*TB),fps={fps}' + (f',tpad=stop_mode=clone:stop={hold}' if hold else '') + '[vout]')
    has_audio = lambda path: bool(media.probe(str(path))['audio'])
    # (adelay keeps the delayed samples' timestamps: apad=whole_dur after asetpts=N/SR/TB, never apad+atrim, measured)
    if audio_path:
        if not has_audio(audio_path):
            raise SystemExit(f'--audio source has no audio: {a.audio}')
        graph.append(f'[{inp(audio_path)}:a:0]asetpts=PTS-STARTPTS,{afmt},apad=whole_dur={dur:.6f},atrim=end={dur:.6f},asetpts=N/SR/TB[base]')
    else:
        samples_so_far = frames_so_far = 0
        for i, pc in enumerate(pieces):
            frames_so_far += pc['out_frames']
            end_samples = media.rounded(frames_so_far * 48000 / rate)
            length = end_samples - samples_so_far
            samples_so_far = end_samples
            if pc['info']['audio']:
                stream = (f'[{inp(pc["path"])}:a:0]aresample=48000:async=1:first_pts=0,'
                          f'atrim=start={float(pc["start_s"]):.9f}:end={float(pc["end_s"]):.9f},'
                          'asetpts=PTS-STARTPTS,apad,')
            else:
                stream = 'anullsrc=r=48000:cl=stereo,'
            graph.append(f'{stream}atrim=end_sample={length}[a{i}]')
        graph.append(''.join(f'[a{i}]' for i in range(len(pieces)))
                     + f'concat=n={len(pieces)}:v=0:a=1,apad=whole_dur={dur:.9f},atrim=end={dur:.9f}[base]')
    mix = '[base]'
    if windows:
        graph.append(f"[base]asetnsamples=n=64:p=0,volume=volume='{_gain_expr([(w['at'], w['at'] + w['from'][1] - w['from'][0]) for w in windows], True)}':eval=frame[bg]")
        for j, w in enumerate(windows):
            if not has_audio(w['path']): raise SystemExit(f"--audio-window source has no audio: {w['source']}")
            L = w['from'][1] - w['from'][0]; f = min(SPLICE_FADE, L / 2)
            graph.append(f"[{inp(w['path'])}:a:0]atrim=start={w['from'][0]:.6f}:end={w['from'][1]:.6f},asetpts=PTS-STARTPTS,{afmt},"
                         f"afade=t=in:d={f}:curve=tri,afade=t=out:st={L - f:.6f}:d={f}:curve=tri,adelay=delays={int(round(w['at'] * 1000))}:all=1,"
                         f"asetpts=N/SR/TB,apad=whole_dur={dur:.6f},atrim=end={dur:.6f}[w{j}]")
        graph.append('[bg]' + ''.join(f'[w{j}]' for j in range(len(windows))) + f'amix=inputs={len(windows) + 1}:normalize=0:duration=first[mx]'); mix = '[mx]'
    if mutes:
        graph.append(f"{mix}asetnsamples=n=64:p=0,volume=volume='{_gain_expr(mutes, False)}':eval=frame[aout]")
    else:
        graph.append(f'{mix}anull[aout]')
    out.parent.mkdir(parents=True, exist_ok=True); ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name('.' + out.name + '.part.mp4')
    args = ['-y']
    for path in inputs: args += ['-i', path]
    media.ff([*args, '-filter_complex', ';'.join(graph), '-map', '[vout]', '-map', '[aout]', '-r', str(fps), '-c:v', 'libx264', '-preset', 'medium', '-crf', '12',
              '-pix_fmt', 'yuv420p', '-frames:v', str(total), '-c:a', 'aac', '-b:a', '256k', '-t', f'{dur:.6f}', '-movflags', '+faststart', str(tmp)])
    got = media.probe(str(tmp), include_frames=True)
    if got['frame_count'] != total: tmp.unlink(); raise SystemExit(f'splice produced {got["frame_count"]} frames, expected {total}')
    oscompat.replace(tmp, out)
    ledger = {'type': 'apimart_job_ledger', 'provider': 'local_splice', 'job_id': job_id, 'job': {'job_id': job_id, 'model': 'local_splice', 'segment': a.segment},
              'note': a.note, 'created_at': api.utc(),
              'recipe': {'pictures': [{'source': pc['source'], 'path': str(pc['path']), 'sha256': sha(pc['path']), 'frames_inclusive': pc['frames'],
                                       **({'fit': pc['fit']} if pc.get('fit') else {})} for pc in pieces],
                         'hold_last_frames': hold, 'fps': fps, 'size': [W, H],
                         'audio': ({'source': a.audio, 'path': str(audio_path), 'sha256': sha(audio_path)} if audio_path
                                   else {'mode': 'each_picture_with_its_matching_audio'}),
                         'audio_windows': [{'source': w['source'], 'path': str(w['path']), 'sha256': sha(w['path']), 'from_s': w['from'], 'at_s': w['at']} for w in windows],
                         'mute_windows': [list(m) for m in mutes], 'foreign_sound': foreign, 'no_mouth': no_mouth, 'fade_s': SPLICE_FADE, 'encode': 'libx264 crf 12 yuv420p, aac 256k'},
              'candidates': {'c1': {'status': 'downloaded', 'output_path': str(out), 'output_sha256': sha(out), 'cost_usd': 0,
                                    'duration_seconds': got['video_duration'], 'frame_count': got['frame_count'], 'fps': got['fps'], 'has_audio': bool(got['audio'])}}}
    save(ledger_path, ledger)
    print(json.dumps({'job': job_id, 'output': str(out), 'frames': total, 'duration_s': round(dur, 3),
                      'next': f'swap.py select --run {run} --job {job_id} --candidate c1 --note "..."'}, ensure_ascii=False, indent=2))



# ---------------------------------------------------------------- reuse: a film that shares footage with a finished one
def all_segments(plan):
    """Planned segments plus the ones taken over from a donor film (`swap.py reuse`), in film order."""
    return sorted(list(plan.get('segments') or []) + list(plan.get('reused_segments') or []), key=lambda x: x['in_frame'])


REUSE_SEL_KEYS = ('picture', 'audio', 'manifest_kind', 'captions', 'sound', 'sound_note', 'no_mouth', 'caption_clean', 'defects')


def reuse_clear(run, plan, sel, write=True):
    """Undo an earlier `reuse` of this RUN: its segments, selections, ledgers and copied clips go, the planned segments
    it had set aside come back."""
    plan['segments'] = sorted(list(plan.get('segments') or []) + [{k: v for k, v in x.items() if k != 'dropped_because'} for x in plan.pop('dropped_for_reuse', [])],
                              key=lambda x: x['in_frame'])
    gone = [x['id'] for x in plan.pop('reused_segments', [])]
    for led in sorted((run/GEN_DIR/'jobs').glob('G*-a1.json')):
        try: is_reuse = read(led).get('provider') == 'reused_generation'
        except Exception: is_reuse = False
        if is_reuse and led.stem.split('-a')[0] not in gone: gone.append(led.stem.split('-a')[0])
    for gid in gone:
        if (sel.get(gid) or {}).get('reused_from'): sel.pop(gid, None)
        if not write: continue
        for f in (run/GEN_DIR/'jobs'/f'{gid}-a1.json', run/GEN_DIR/'results'/f'{gid}-a1-c1.mp4', run/GEN_DIR/'results'/f'{gid}-a1-c1.captioned.mp4',
                  run/'prompts'/f'{gid}-a1.final.txt'):
            if f.is_file() and (f.suffix != '.json' or read(f).get('provider') == 'reused_generation'): f.unlink()
    return gone


def cmd_reuse(a):
    """Give this film the reviewed results of a finished film that shares its footage (user 2026-11-22; three batches:
    15 of 26 films were re-edits of footage another film of the batch had already generated, and were matched by hand).

    The target is planned as usual first. Every generated segment of each --from RUN is then searched in the target,
    frame for frame (reuse.py); where the same frames are found, the donor's selected clip, its captions, defects and
    notes are imported as segment G9nn (ledger provider `reused_generation`, no API call), and the target's own planned
    segments over that stretch are set aside. A planned segment with footage the donor film does not have stays and is
    generated as usual. Run it again after the donor changed (a goof fixed there): it starts from scratch each time."""
    run, state = load(a.run)
    import reuse as rz
    rz.TOLERANCE = float(getattr(a, 'tolerance', None) or 1.0)
    plan_path = run/'plan/segments.json'
    if not plan_path.is_file(): raise SystemExit('reuse: 先把这条片照常做到 plan（识别、切段），再 reuse：只有画面和代表片逐帧相同的段才共用，这条片独有的内容照常生成')
    plan = read(plan_path); fps = float(media.ratio(str(plan['fps'])))
    sel_path = run/'plan/selections.json'; sel = read(sel_path) if sel_path.is_file() else {}
    reuse_clear(run, plan, sel, write=not a.dry_run)
    target_master = Path(state['stages']['prepare']['cfr_master']['path'])
    cache = run/'reuse'/'prints'; tp = rz.prints(target_master, cache)
    generated_here = {f.stem.split('-a')[0] for f in (run/GEN_DIR/'jobs').glob('G*-a*.json')}
    notes, mapped = [], []
    for d in a.donor:
        drun = Path(d).expanduser().resolve()
        if drun == run: raise SystemExit('reuse: --from is another RUN (the finished film), not this one')
        if not (drun/'state.json').is_file() or not (drun/'plan/segments.json').is_file():
            raise SystemExit(f'reuse: {drun} is not a planned RUN')
        dstate = read(drun/'state.json'); dst = dstate.get('stages', {})
        if not a.unfinished_donor and not ('recut' in dst and 'final_screen' in dst):
            raise SystemExit(f'reuse: {drun.name} 还没做完（没有 recut / final-screen）。代表片先审完、终检过了再共用；确实要先用它现在的选片加 --unfinished-donor，代表片改了之后重跑 reuse')
        dplan = read(drun/'plan/segments.json'); dsel = read(drun/'plan/selections.json') if (drun/'plan/selections.json').is_file() else {}
        if abs(float(media.ratio(str(dplan['fps']))) - fps) > 1e-6:
            notes.append({'donor': drun.name, 'skipped': f"帧率不同（{dplan['fps']} / {plan['fps']}），不共用"}); continue
        dp = rz.prints(dstate['stages']['prepare']['cfr_master']['path'], cache)
        for dseg in all_segments(dplan):
            s = dsel.get(dseg['id']) or {}
            where = {'donor': drun.name, 'segment': dseg['id']}
            if not s.get('job'): notes.append({**where, 'skipped': '代表片这段还没有选片'}); continue
            if s.get('picture') == 'source' and s.get('audio') != 'candidate':
                notes.append({**where, 'skipped': '代表片这段保留了原画面（没有生成结果可共用）：这条片对应的段也照它处理（select --keep-source-picture，成片上的定点修补要再做一遍）',
                              'note': s.get('note')}); continue
            media_file = Path((s.get('media') or {}).get('path', ''))
            if not media_file.is_file(): notes.append({**where, 'skipped': f'选用的文件不在了：{media_file}'}); continue
            m = rz.locate(dp, tp, dseg['in_frame'], dseg['out_frame'])
            if not m or not m['ok']:
                notes.append({**where, 'skipped': '这条片里没有这段画面（或只有一部分）', **({'best': m} if m else {})}); continue
            mapped.append({'drun': drun, 'dseg': dseg, 'sel': s, 'off': m['offset'], 'a': dseg['in_frame'] + m['offset'], 'b': min(len(tp), dseg['out_frame'] + m['offset']), 'match': m, 'dp': dp})
    order = [str(Path(x).expanduser().resolve()) for x in a.donor]
    chosen = []
    for m in sorted(mapped, key=lambda x: (order.index(str(x['drun'])), x['a'])):     # two donors offering the same stretch: the one given first wins
        clash = next((c for c in chosen if c['a'] < m['b'] and m['a'] < c['b']), None)
        if clash:
            notes.append({'donor': m['drun'].name, 'segment': m['dseg']['id'], 'skipped': f"和 {clash['drun'].name} {clash['dseg']['id']} 重叠，用排在前面的代表片"}); continue
        chosen.append(m)
    chosen.sort(key=lambda x: x['a'])
    # a planned segment of this film is set aside only when all of it is the donor film's footage at one offset: the
    # parts the donor regenerated come in as reused segments, the parts the donor left original stay original here too
    status, blocked, changed = {}, set(), True
    while changed:
        changed = False
        for T in plan['segments']:
            over = [i for i, c in enumerate(chosen) if c['a'] < T['out_frame'] and T['in_frame'] < c['b']]
            if not over or (T['id'] in status and not status[T['id']]['drop']): continue      # decided to stay: its reason stands
            reason = None; first = chosen[over[0]]
            if set(over) & blocked: reason = '和一个不能共用的代表片段重叠'
            elif T['id'] in generated_here: reason = '这条片已经自己生成过这一段'
            elif any(chosen[i]['drun'] != first['drun'] or chosen[i]['off'] != first['off'] for i in over): reason = '跨了两条代表片或两个对位'
            else:
                gaps, cur = [], T['in_frame']
                for i in over:
                    c = chosen[i]
                    if c['a'] > cur: gaps.append((cur, min(c['a'], T['out_frame'])))
                    cur = max(cur, c['b'])
                if cur < T['out_frame']: gaps.append((cur, T['out_frame']))
                unique = [g for g in gaps if not rz.same_at(first['dp'], tp, g[0], g[1], first['off'])]
                if unique: reason = '有代表片里没有的画面：' + ', '.join(f'{x / fps:.2f}-{y / fps:.2f}s' for x, y in unique)
                else: status[T['id']] = {'drop': True, 'donor_left_original': [[round(x / fps, 2), round(y / fps, 2)] for x, y in gaps]}
            if reason:
                status[T['id']] = {'drop': False, 'reason': reason}
                if not set(over) <= blocked: blocked |= set(over); changed = True
    adopt = [c for i, c in enumerate(chosen) if i not in blocked]
    for i in sorted(blocked): notes.append({'donor': chosen[i]['drun'].name, 'segment': chosen[i]['dseg']['id'], 'skipped': '这条片对应的计划段不能整段共用（见 kept）'})
    shots = read(run/'shots/shots.json')['shots'] if (run/'shots/shots.json').is_file() else []
    report = {'adopted': [], 'set_aside': [{'segment': k, **{x: y for x, y in v.items() if x != 'drop'}} for k, v in status.items() if v['drop']],
              'kept': [{'segment': k, 'reason': v['reason']} for k, v in status.items() if not v['drop']]
                      + [{'segment': T['id'], 'reason': '没有共用段覆盖，按本片自己的计划做（生成或 override 保留原片）'}
                         for T in plan['segments'] if T['id'] not in status], 'notes': notes}
    reused, board_notes = [], {}
    for k, c in enumerate(adopt, 1):
        gid = f'G9{k:02d}'; s = c['sel']; dseg = c['dseg']; drun = c['drun']
        origin = {'run': str(drun), 'film': drun.name, 'segment': dseg['id'], 'job': s['job'], 'donor_frames': [dseg['in_frame'], dseg['out_frame']],
                  'offset': c['off'], 'match': {x: c['match'][x] for x in ('mean', 'worst', 'share_same')}}
        row = {'segment': gid, 'from': f"{drun.name} {dseg['id']} ({s['job']})", 'source_s': [round(c['a'] / fps, 2), round(c['b'] / fps, 2)],
               'covers': [T['id'] for T in plan['segments'] if c['a'] < T['out_frame'] and T['in_frame'] < c['b']] or '计划里没有（代表片换了，这里照它换）',
               'match': origin['match']}
        report['adopted'].append(row)
        if a.dry_run: continue
        base_src = Path((s.get('media_uncaptioned') or s['media'])['path']); media_src = Path(s['media']['path'])
        for f, rec in ((base_src, s.get('media_uncaptioned') or s['media']), (media_src, s['media'])):
            if not f.is_file() or (rec.get('sha256') and sha(f) != rec['sha256']):
                raise SystemExit(f"reuse: {drun.name} {dseg['id']} 的选用文件和它的选片记录对不上（{f}）：在代表片里重新 select 后再共用")
        res = run/GEN_DIR/'results'; res.mkdir(parents=True, exist_ok=True); (run/GEN_DIR/'jobs').mkdir(parents=True, exist_ok=True)
        base = res/f'{gid}-a1-c1.mp4'; shutil.copyfile(base_src, base)
        chosen_file = base
        if media_src != base_src:
            chosen_file = res/f'{gid}-a1-c1.captioned.mp4'; shutil.copyfile(media_src, chosen_file)
        info = media.probe(str(base), include_frames=True)
        save(run/GEN_DIR/'jobs'/f'{gid}-a1.json', {
            'type': 'apimart_job_ledger', 'provider': 'reused_generation', 'job_id': f'{gid}-a1', 'job': {'job_id': f'{gid}-a1', 'model': 'reused_generation', 'segment': gid},
            'note': f"共用 {drun.name} {dseg['id']}（{s['job']}）的审定结果，零次 API 提交", 'created_at': api.utc(), 'reused_from': origin,
            'joins': splice_joins(drun, base_src),
            'candidates': {'c1': {'status': 'downloaded', 'output_path': str(base), 'output_sha256': sha(base), 'cost_usd': 0,
                                  'duration_seconds': info['video_duration'], 'frame_count': info['frame_count'], 'fps': info['fps'], 'has_audio': bool(info['audio'])}}})
        entry = {'job': f'{gid}-a1', 'candidate': 'c1', 'media': ref(chosen_file), 'clip_in': s.get('clip_in'), 'clip_out': s.get('clip_out'),
                 'note': f"共用 {drun.name} {dseg['id']} 的审定结果" + (f"：{s['note']}" if s.get('note') else ''), 'selected_at': now(), 'reused_from': origin,
                 **{k2: s[k2] for k2 in REUSE_SEL_KEYS if s.get(k2) is not None}}
        if chosen_file != base: entry['media_uncaptioned'] = ref(base)
        sel[gid] = entry
        prompt = next((f for f in (drun/'prompts'/f"{s['job']}.final.txt", drun/'prompts'/f"{dseg['id']}-a1.final.txt") if f.is_file()), None)
        if prompt: (run/'prompts').mkdir(exist_ok=True); shutil.copyfile(prompt, run/'prompts'/f'{gid}-a1.final.txt')
        dn = drun/'plan/board_notes.json'
        if dn.is_file() and (read(dn).get(dseg['id']) or {}): board_notes[gid] = read(dn)[dseg['id']]
        seg = {k2: v for k2, v in dseg.items() if k2 not in ('prompt_file', 'product_shots', 'reused_from')}
        seg.update(id=gid, in_frame=c['a'], out_frame=c['b'], product_shots=[], reused_from=origin)
        mine = [x for x in shots if x['in_frame'] < c['b'] and x['out_frame'] > c['a']]
        if mine:
            seg['shot_ids'] = [x['id'] for x in mine]
            seg['shot_times'] = [[x['id'], round(max(0, x['in_frame'] - c['a']) / fps, 2), round((min(c['b'], x['out_frame']) - c['a']) / fps, 2)] for x in mine]
        reused.append(seg)
    nxt = ['没共用上的段（kept、计划里其余的段）照常写提示词、生成、审片',
           'finish.py 会把共用段和本片生成段一起回剪、盖水印、终检；共用段和本片画面的接缝在闪回帧板里，照常逐张看',
           '共用段里发现穿帮：回代表片的 RUN 修（round），修完在这里重跑同一条 reuse 命令']
    if a.dry_run:
        print(json.dumps({'dry_run': True, **report, 'next': nxt}, ensure_ascii=False, indent=1)); return
    dropped = [{**T, 'dropped_because': status[T['id']]} for T in plan['segments'] if (status.get(T['id']) or {}).get('drop')]
    plan['segments'] = [T for T in plan['segments'] if not (status.get(T['id']) or {}).get('drop')]
    if reused: plan['reused_segments'] = reused
    if dropped: plan['dropped_for_reuse'] = dropped
    save(plan_path, plan); save(sel_path, sel)
    if board_notes:
        bn = run/'plan/board_notes.json'; cur = read(bn) if bn.is_file() else {}
        cur.update(board_notes); save(bn, cur)
    commit(run, state, 'reuse', donors=[str(Path(x).expanduser().resolve()) for x in a.donor], adopted=[x['segment'] for x in report['adopted']],
           set_aside=[x['segment'] for x in report['set_aside']], kept=[x['segment'] for x in report['kept']])
    print(json.dumps({**report, 'left_to_generate': [T['id'] for T in plan['segments']], 'next': nxt}, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- recut / final
CUT_TOLERANCE_S = 0.5   # a selected clip may be this much shorter than its source segment (a glitch frame, a tail)


def content_cuts(plan, sel, fps, approved=()):
    """Segments whose selected picture is shorter than the source segment by more than CUT_TOLERANCE_S: original
    content (shots, sentences) would disappear from the film. User 2026-11-14: dropping original content is not
    allowed; only an explicit user-approved cut (`--user-approved-cut G##`) passes."""
    from fractions import Fraction
    rate = float(Fraction(str(fps))) if fps else 30.0
    out = []
    for gid, seg in plan.items():
        s = sel.get(gid)
        if not s or gid in approved or s.get('picture') == 'source': continue
        if s.get('clip_in') is None or s.get('clip_out') is None: continue
        src = (seg['out_frame'] - seg['in_frame']) / rate
        kept = float(s['clip_out']) - float(s['clip_in'])
        if src - kept > CUT_TOLERANCE_S: out.append((gid, src, kept))
    return out


def cmd_sound_audit(a):
    """Read-only (user 2026-11-21): for every selected local splice of the RUN, the seconds of the clip where the sound is
    not the picture's own sound of the same moment (foreign_sound), and the master seconds they land on. Each one is to
    be looked at: a speaking mouth there is a lip-sync goof to fix (re-splice with each picture's own sound, or redo the
    shot with lip sync); no mouth there is fine and goes in --no-mouth when the splice is redone."""
    run = Path(a.run).resolve()
    sel = read(run/'plan/selections.json') if (run/'plan/selections.json').is_file() else {}
    out = []
    for gid, e in sorted(sel.items()):
        if not isinstance(e, dict) or not e.get('job'): continue
        lp = run/GEN_DIR/'jobs'/(e['job'] + '.json')
        if not lp.is_file(): continue
        led = read(lp)
        if led.get('provider') != 'local_splice': continue
        rec = led.get('recipe') or {}; rate = float(media.ratio(rec.get('fps') or 30))
        pieces = []
        for pc in rec.get('pictures') or []:
            fr = pc.get('frames_inclusive') or pc.get('frames')
            if not fr: continue
            path = pc.get('path', ''); src_rate = rate
            try: src_rate = float(media.ratio(media.probe(path)['fps']))
            except Exception: pass
            n = int(fr[1]) - int(fr[0]) + 1
            pieces.append({'path': path, 'start_s': int(fr[0]) / src_rate, 'out_frames': pc.get('fit') or round(n / src_rate * rate), 'fit': pc.get('fit')})
        aud = rec.get('audio') or {}
        windows = [{'path': w.get('path'), 'from': w['from_s'], 'at': w['at_s']} for w in rec.get('audio_windows') or []]
        foreign = foreign_sound(pieces, rate, int(rec.get('hold_last_frames') or 0), aud.get('path'), windows)
        declared = rec.get('no_mouth') or []
        left = [f for f in foreign if not any(n0 - 0.05 <= f[0] and f[1] <= n1 + 0.05 for n0, n1 in declared)]
        if left:
            clip_in = float(e.get('clip_in') or 0)
            out.append({'segment': gid, 'job': e['job'], 'clip_in': clip_in, 'foreign_s': left,
                        'windows': [{'source': Path(w['path'] or '').name, 'from_s': w['from'], 'at_s': w['at']} for w in windows],
                        'base_audio': Path(aud['path']).name if aud.get('path') else 'each picture its own'})
    print(json.dumps({'run': str(run), 'segments_to_check': out,
                      'next': 'open each span in the selected clip (seconds of the clip, before clip_in): a speaking mouth there -> re-splice that '
                              'part with the picture\'s own sound, or redo the shot with lip sync; no mouth -> fine (declare it with --no-mouth when you redo the splice)'},
                     ensure_ascii=False, indent=2))


def content_preserved_reviews(run, plan, sel, master, fps, review_path):
    """Exact human review of a duration heuristic false positive, never permission to drop content.

    A native splice can be shorter after removing AI-added repetitions while retaining every source shot and
    sentence. Bind that determination to the final selected bytes, native recipe, source and reviewed evidence.
    No proof means the existing duration guard remains unchanged; this is separate from user-approved deletion.
    """
    if not review_path: return set(), None
    proof = read(review_path)
    if proof.get('type') != 'content-preserved-effective-ripple-review' or proof.get('reviewer') != 'Codex':
        raise SystemExit('invalid content-preserved review type/reviewer')
    if Path(proof.get('run', '')).resolve() != run.resolve() or proof.get('source_sha256') != sha(master):
        raise SystemExit('content-preserved review belongs to another RUN/source')
    if proof.get('plan_sha256') != sha(run/'plan/segments.json'):
        raise SystemExit('content-preserved review plan changed')
    accepted = set()
    shortened = {g: (src, kept) for g, src, kept in content_cuts(plan, sel, fps)}
    for item in proof.get('items', []):
        gid = item.get('segment'); s = sel.get(gid)
        if gid in accepted or gid not in shortened or not s or s.get('picture') == 'source':
            raise SystemExit('content-preserved review must name one currently shortened generated selection')
        if item.get('selection') != s or sha(s['media']['path']) != s['media']['sha256']:
            raise SystemExit('content-preserved reviewed selection/media changed')
        ledger_path = run/GEN_DIR/'jobs'/(s['job'] + '.json')
        ledger = read(ledger_path); recipe = ledger.get('recipe', {})
        candidate = ledger.get('candidates', {}).get(s['candidate'], {})
        if ledger.get('provider') != 'local_splice' or item.get('ledger_sha256') != sha(ledger_path):
            raise SystemExit('content-preserved review requires its exact native splice ledger')
        if recipe.get('hold_last_frames') or any(p.get('fit') is not None or 'fit_frames' in p or 'output_frames' in p
                                                for p in recipe.get('pictures', [])):
            raise SystemExit('content-preserved review does not permit fitted/held picture')
        if sha(candidate['output_path']) != candidate.get('output_sha256') or any(
                sha(p['path']) != p.get('sha256') for p in recipe.get('pictures', []) + recipe.get('audio_windows', [])):
            raise SystemExit('content-preserved native splice inputs/output changed')
        if float(s['clip_in']) != 0 or abs(float(s['clip_out']) - float(candidate['duration_seconds'])) > 1e-6:
            raise SystemExit('content-preserved review must retain the entire native splice')
        required = ('all_original_shots_preserved', 'all_original_sentences_preserved',
                    'actual_joins_viewed', 'onscreen_same_window_av', 'no_fit_speed_or_picture_hold')
        if not all(item.get(k) is True for k in required) or not item.get('removed_ai_only'):
            raise SystemExit('content-preserved review lacks complete shot/sentence/AV determination')
        shots = item.get('shots', [])
        if [v.get('source_shot') for v in shots] != plan[gid]['shot_ids'] or any(not v.get('retained_as') for v in shots):
            raise SystemExit('content-preserved review must account for every original shot in order')
        if item.get('source_speech') != plan[gid].get('speech', []):
            raise SystemExit('content-preserved review must account for exact original dialogue')
        if abs(item.get('shorter_seconds', -1) - (shortened[gid][0] - shortened[gid][1])) > 1e-6:
            raise SystemExit('content-preserved reviewed duration differs from selection')
        evidence = item.get('evidence', [])
        if not evidence: raise SystemExit('content-preserved review needs actual image and speech evidence')
        for e in evidence:
            p = Path(e['path']).resolve()
            if run.resolve() not in p.parents or not p.is_file() or sha(p) != e.get('sha256'):
                raise SystemExit('content-preserved review evidence changed or is foreign')
        accepted.add(gid)
    if not accepted: raise SystemExit('empty content-preserved review')
    return accepted, ref(review_path)


def still_left(run, plan, sel):
    """Segments whose selected picture still holds still frames retrim would cut (shot_regen.pending_still)."""
    import shot_regen
    out = []
    for gid, s in sorted(sel.items()):
        if gid not in plan: continue
        try: left = shot_regen.pending_still(run, gid, s, float(plan[gid].get('duration_s') or 0), [])
        except (OSError, ValueError, KeyError) as e:
            print(f'recut: {gid}: still-frame check skipped ({e})', file=sys.stderr); continue
        if not left: continue
        if 'ranges' in left: out.append(f"{gid}: 补做镜头的定住帧 {left['ranges']}（{s.get('job')}）")
        else: out.append(f"{gid}: 段尾从 {left['tail'][1]['still_from']:.2f} 秒起静止，选到 {float(s['clip_out']):.2f} 秒，应裁到 {left['tail'][0]:.2f} 秒")
    return out


def cmd_recut(a):
    run, state = load(a.run)
    plan = {s['id']: s for s in all_segments(read(run/'plan/segments.json'))}; sel = read(run/'plan/selections.json')     # with the segments reused from a donor film
    master = working_master(state); info = media.probe(str(master))
    pictures = []; audios = []
    preserved, preserved_ref = content_preserved_reviews(run, plan, sel, master, info['fps'],
                                                        getattr(a, 'content_preserved_review', None))
    shortened = content_cuts(plan, sel, info['fps'], set(a.user_approved_cut or []) | preserved)
    if shortened:
        raise SystemExit('不许删掉原片内容（用户 2026-11-14："擅自把原片的一些段落给删掉，直接不做，这是不允许的"）。这些段选用的画面比原段短了：\n- '
                         + '\n- '.join(f'{g}: 原段 {d:.2f} 秒，选用 {k:.2f} 秒，少了 {d - k:.2f} 秒' for g, d, k in shortened)
                         + f'\n每段最多只能比原段短 {CUT_TOLERANCE_S} 秒（去掉闪帧、补尾那种）。生成结果有穿帮、缺句时不剪内容，'
                         '写进交付说明报告用户，由用户决定是否付费重生成；用户明确同意删某段时才加 --user-approved-cut G##。')
    still = still_left(run, plan, sel)
    if still:
        raise SystemExit('回剪前要把定格帧裁掉（用户 2026-11-23："回剪的时候把定格帧裁掉"；2114 G03 两个接点前各留了 1.4 秒、1.7 秒近乎静止的画面）。'
                         '这些段的选片里还有静止画面：\n- ' + '\n- '.join(still)
                         + '\n跑 finish.py（第一步 retrim 自动裁，免费，画面和声音一起裁，不短于原镜头，说话处不裁），或 shot_regen.py retrim --run RUN --all 后 finish.py --from captions。'
                         '共用段（G9nn）跟代表片走：代表片 retrim + finish 后在这条片重跑 swap.py reuse。原片本来就是静止镜头时，select 加 --keep-frozen-tail。')
    outputs = ledger_outputs(run)
    foreign = [(gid, s['media']['path']) for gid, s in sel.items() if gid in plan and s.get('picture') != 'source'
               and s.get('media') and picture_origin(run, s['media']['path'], outputs) is None]
    if foreign:
        raise SystemExit('生成段的画面必须来自本段的生成结果（账本里登记的付费结果或 swap.py splice 的结果，及 select / captions 自动做的去字幕、加字幕版本）。'
                         '这些段用的是自己做的片子（放大裁切、贴图、重取景、拿原片或别段画面顶替）：\n- '
                         + '\n- '.join(f'{g}: {p}' for g, p in foreign)
                         + '\n用户 2026-11-14 禁止贴图和放大裁切补救生成段，也不许删原片内容。用 swap.py select 选本段生成结果，'
                         '必要时 swap.py splice 只裁帧或借整帧；救不了就写进交付说明，报告用户决定是否付费重生成。')
    for gid, seg in plan.items():
        if gid not in sel: raise SystemExit('segment without selection: ' + gid)
        s = sel[gid]
        if s.get('picture') == 'source':
            # Agent decided the original picture stays (e.g. a product-free reaction shot). With audio='candidate' (a
            # narration-only rewrite) the edit candidate's re-voiced soundtrack replaces the source audio here.
            if s.get('audio') == 'candidate':
                if has_onscreen_dialogue(seg):
                    raise SystemExit(f'{gid}: on-screen dialogue requires picture and audio from the same selected clip')
                audios.append({'id': gid + '-vo', 'kind': 'VO', 'source_in': seg['in_frame'], 'source_out': seg['out_frame'], 'path': s['media']['path'],
                               'clip_in': s['clip_in'], 'clip_out': s['clip_out'], 'speed': 1.0,
                               'evidence': 'Seedance native edit re-voiced line, source picture kept: ' + (s.get('note') or 'selected')})
            continue
        if (run/'captions/style.json').is_file() and not s.get('captions') and seg.get('speech'):
            print(f'recut: {gid} goes in without captions (swap.py captions puts them back in the film\'s style)', file=sys.stderr)
        kind = 'Dialogue' if has_onscreen_dialogue(seg) else (s.get('manifest_kind') or 'Visual')
        pictures.append({'id': gid, 'kind': kind, 'source_in': seg['in_frame'], 'source_out': seg['out_frame'], 'path': s['media']['path'],
                         'clip_in': s['clip_in'], 'clip_out': s['clip_out'], 'evidence': 'agent reviewed sheets: ' + (s.get('note') or 'selected')})
    manifest = {'schema_version': 1, 'source': str(master), 'fps': info['fps'], 'timing': 'ripple',
                'max_speed': 1.0, 'picture_replacements': pictures, 'audio_replacements': audios}
    if preserved_ref: manifest['content_preserved_review'] = preserved_ref
    mpath = run/'recut/edit.json'; save(mpath, manifest)
    index = run/'recut/generated_index.json'; speech = run/'recut/speech_review.json'
    save(index, {'type': 'lymphloop_generated_index', 'items': [{'task_id': p['id'], 'media': ref(p['path']), 'clip_in': p['clip_in'], 'clip_out': p['clip_out']} for p in pictures]})
    save(speech, {'type': 'lymphloop_speech_review', 'items': []})
    frozen = run/'recut/edit.frozen.json'
    media.freeze(str(mpath), str(frozen), [str(index), str(speech)])
    out = Path(state['output_dir'])/'master.mp4'
    media.render(str(mpath), str(out), str(run/'recut/render'), frozen_plan=str(frozen))
    commit(run, state, 'recut', master=ref(out), manifest=ref(mpath), timeline=str(out) + '.timeline.json', verification=str(out) + '.verification.json')
    print(json.dumps(state['stages']['recut'], ensure_ascii=False, indent=2))


def cmd_brand(a):
    """White opaque LYMPHLOOP over every frame of the original floating watermark (droplet icon + LYMPHORIA), same position,
    size and orientation (user, 2026-10-25). Reads the recut master, writes OUT/master.mp4 (the unbranded file is kept as
    OUT/master.unbranded.mp4), verifies the stamps in the encoded file and records a receipt. Re-running starts again from
    the unbranded master, so it never stamps twice."""
    run, state = load(a.run)
    import watermark_brand as wb
    if 'recut' not in state.get('stages', {}): raise SystemExit('brand: run swap.py recut first')
    scan = read(run/'watermark/scan.json'); wb.set_target(scan.get('target') or 'LYMPHORIA')
    if not scan.get('overlay_classification_reviewed'): raise SystemExit('brand: the watermark scan has not been reviewed (swap.py watermark apply)')
    out_dir = Path(state['output_dir']); master = out_dir/'master.mp4'; unbranded = out_dir/'master.unbranded.mp4'
    recut_sha = state['stages']['recut']['master']['sha256']
    if sha(master) == recut_sha: shutil.copyfile(master, unbranded)
    elif not (unbranded.is_file() and sha(unbranded) == recut_sha): raise SystemExit('brand: OUT/master.mp4 is neither the recut master nor a branded copy of it; re-run recut')
    cfr = Path(state['stages']['prepare']['cfr_master']['path'])
    src_info, out_info = media.probe(str(cfr), include_frames=True), media.probe(str(unbranded), include_frames=True)
    timeline = read(state['stages']['recut']['timeline'])
    if (timeline['source_frames'], timeline['output_frames'], timeline['source_fps'], timeline['output_fps']) != (
            src_info['frame_count'], out_info['frame_count'], str(src_info['fps']), str(out_info['fps'])):
        raise SystemExit('brand: timeline does not match the source and recut masters')
    v = out_info['video']
    width, height = int(v['width']), int(v['height'])
    source_plan = wb.plan(scan, width, height)
    direction_cache = {}  # one RUN and one brand call, never shared across films
    if source_plan['frames']:
        wb.orient_tracks(source_plan, cfr, run, places=scan.get('positions'), direction_cache=direction_cache)
    detections = wb.remap_source_stamps(source_plan, timeline)
    fixes = run/'watermark'/f'final-overlays-{recut_sha}.json'
    if fixes.is_file():
        detections.extend(read(fixes)['detections'])
    plan = wb.plan({'detections': detections}, width, height, smooth=0)
    if fixes.is_file():
        wb.orient_tracks(plan, unbranded, run, places=scan.get('positions'), direction_cache=direction_cache)
    # one box per place for the whole film (user 2026-11-20: "lymphloop 字样会忽大忽小，很丑，所以我想要固定住它")
    places = scan.get('positions')
    if places is None:
        places = wb.find_places([d for d in scan.get('detections', []) if d.get('classification') == 'floating_overlay'], cfr, width, height)
    wb.fix_stamps(plan, places)
    # Explicit agent-measured manual footprints must not be narrowed to an older
    # source place. Keep the one fixed letter box/direction; clean the full verified
    # text/icon footprint as well, only on its explicitly requested frames.
    if fixes.is_file():
        for d in read(fixes)['detections']:
            if d.get('pass') != 'manual': continue
            for st in plan['stamps'].get(str(d['frame']), []):
                if wb._overlap(st.get('mark', st['box']), d['bbox']) < 0.5: continue
                x, y, w, h = st.get('mask_box') or st['box']; a, b, c, e = d['bbox']
                left, top = max(0, min(x, a)), max(0, min(y, b))
                st['mask_box'] = [left, top, min(width, max(x+w, a+c))-left, min(height, max(y+h, b+e))-top]
    save(run/'watermark/brand_plan.json', plan)
    receipt = out_dir/'master.brand.json'
    if not plan['frames']:
        commit(run, state, 'brand', skipped='no floating watermark in the reviewed scan', master=ref(master)); print(json.dumps({'skipped': True})); return
    tmp = out_dir/'.master.branding.mp4'
    # by recut atom (user 2026-11-22): only atoms with watermark frames whose content changed are stamped and encoded
    # again; the rest go in as recut encoded them. Older recuts (no per-atom files) stamp the whole film as before.
    if wb.atoms_usable(timeline): rec = wb.stamp_atoms(unbranded, timeline, plan, tmp, receipt, run/'recut/brand-atoms')
    else: rec = wb.stamp(unbranded, plan, tmp, receipt)
    oscompat.replace(tmp, master)
    rec['output']['path'] = str(master); save(receipt, rec)
    commit(run, state, 'brand', unbranded=ref(unbranded), master=ref(master), receipt=ref(receipt), plan=ref(run/'watermark/brand_plan.json'),
           stamped_frames=rec['stamped_frames'], tracks=rec['tracks'], verification=rec['verification'], **({'by_atom': rec['by_atom']} if rec.get('by_atom') else {}))
    print(json.dumps({'master': str(master), 'stamped_frames': rec['stamped_frames'], **({'by_atom': rec['by_atom']} if rec.get('by_atom') else {}), 'tracks': rec['tracks'],
                      'orientations': [(t['id'], t['orient'], t['orient_source']) for t in plan['track_list']],
                      'places': [(p['box'], p['orient'], p['stamped_frames']) for p in plan.get('positions', [])], 'verification': rec['verification']}, ensure_ascii=False, indent=2))


def screened_master(state, run):
    """The file final-screen must look at: the branded master when the scan found a watermark (brand is required then),
    otherwise the recut master."""
    recut = state['stages']['recut']['master']; b = state.get('stages', {}).get('brand')
    if b and b.get('unbranded', {}).get('sha256') == recut['sha256']: return Path(b['master']['path'])
    scan = run/'watermark/scan.json'
    overlays = scan.is_file() and any(d.get('classification') == 'floating_overlay' for d in read(scan).get('detections', []))
    if overlays and not (b and b.get('skipped') and b['master']['sha256'] == recut['sha256']):
        raise SystemExit('final-screen: this video had a floating watermark; run swap.py brand after recut first')
    return Path(recut['path'])


def watermark_left(video, work, target='LYMPHORIA', ranges=None, places=None):
    """Frames of the screened master that still show the competitor watermark. With the mark's fixed places known
    (brand_plan positions): only those places, every 4th frame of the WHOLE master and every frame around a hit (user
    2026-11-20; Codex 30542865505302802: the full-frame + edge-strip re-scan took ~10 min a round, this ~1 min, and the
    whole master is read, so a burst the source scan missed shows up too). Otherwise, or with --full-watermark: fuzzy
    full-frame OCR + the edge pass over `ranges` (master frames, end exclusive; default the whole master)."""
    import watermark_brand as wb
    pattern = wb.set_target(target)
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    if places:
        pos = wb.position_scan(video, places, work/'position_scan')
        dets = [{**d, 'pass': 'final_position'} for d in pos['detections']]
        save(Path(work)/'left.json', {'type': 'lymphloop_watermark_left', 'video': str(video), 'detections': dets,
                                      'coverage': pos['coverage'], 'places': places})
        return sorted({d['frame'] for d in dets}), None
    import vision_scan
    binary = vision_scan.watermark_cmd(work)
    n = int(media.probe(str(video), include_frames=True)['frame_count'])
    full = wb.fast_scan(binary, video, work/'scan_parts', sha(video), pattern, target, n, ranges=ranges)
    dets = [{'frame': d['frame'], 'bbox': wb.with_icon(d), 'text': d['text'], 'pass': 'final_full'} for d in full['detections'] if wb.is_watermark_text(d['text'])]
    edge = wb.edge_scan(video, work/'edge_scan.json', work/'edge_work', ranges=wb.merge_ranges(ranges, n) if ranges else None)
    dets += [{**d, 'pass': 'final_' + d['pass']} for d in edge['detections']]
    save(Path(work)/'left.json', {'type': 'lymphloop_watermark_left', 'video': str(video), 'detections': dets})
    return sorted({d['frame'] for d in dets}), full.get('frame_count')


def watermark_left_template(video, work, target, brand_plan):
    """final-screen with the stored template: every frame of the master matched at the six places (about half a minute,
    no OCR); a frame showing the mark at a place brand did not stamp in that frame is left over."""
    import watermark_template as wtpl
    meta = wtpl.load(wtpl.available(target))
    v = media.probe(str(video))['video']; W, H = int(v['width']), int(v['height'])

    def place_of(box):
        cx, cy = (box[0] + box[2] / 2) / W * wtpl.REF_W, (box[1] + box[3] / 2) / H * wtpl.REF_H
        return min(range(len(meta['places'])), key=lambda i: (meta['places'][i]['box'][0] + meta['places'][i]['box'][2] / 2 - cx) ** 2
                   + (meta['places'][i]['box'][1] + meta['places'][i]['box'][3] / 2 - cy) ** 2)
    stamped = {}
    for f, stamps in (brand_plan.get('stamps') or {}).items():
        for s in stamps: stamped.setdefault(place_of(s['box']), set()).add(int(f))
    dets = wtpl.left(video, target, stamped, Path(work)) or []
    Path(work).mkdir(parents=True, exist_ok=True)
    save(Path(work)/'left.json', {'type': 'lymphloop_watermark_left', 'video': str(video), 'detections': dets, 'coverage': 'every_frame_template'})
    return sorted({d['frame'] for d in dets})


def changed_ranges(run, master, pad=30):
    """Master frame ranges (end exclusive, padded) that are not the already-screened original: generated pictures,
    generated or re-voiced sound, planned segments whose source picture was kept (their leftovers were fixed on the
    master), and the frames of the original floating watermark (where brand stamped). Everything else is the original
    film frame for frame, screened once before planning (user 2026-11-19: final-screen re-screened all 281 shots, 45 min)."""
    import watermark_brand as wb
    tl = read(str(master) + '.timeline.json')['segments']
    n = int(media.probe(str(master), include_frames=True)['frame_count'])
    out = [(s['out_in'], s['out_out']) for s in tl if s.get('picture_id') or s.get('audio_replacement_id')]

    def to_master(a, b):        # source frames [a, b) -> master frames through the untouched timeline pieces
        return [(s['out_in'] + max(a, s['source_in']) - s['source_in'], s['out_in'] + min(b, s['source_out']) - s['source_in'])
                for s in tl if not s.get('picture_id') and s.get('source_in') is not None and max(a, s['source_in']) < min(b, s['source_out'])]
    plan = {x['id']: x for x in all_segments(read(run/'plan/segments.json'))} if (run/'plan/segments.json').is_file() else {}
    sel = read(run/'plan/selections.json') if (run/'plan/selections.json').is_file() else {}
    for gid, e in sel.items():
        if e.get('picture') == 'source' and gid in plan: out += to_master(plan[gid]['in_frame'], plan[gid]['out_frame'])
    scan = run/'watermark/scan.json'
    if scan.is_file():
        frames = sorted({d['frame'] for d in read(scan).get('detections', []) if d.get('classification') == 'floating_overlay'})
        for f in frames: out += to_master(f, f + 1)
    return wb.merge_ranges([(a - pad, b + pad) for a, b in out], n)


def master_content(state, master, master_sha):
    """What each stretch of the screened master IS, independent of where it sits (user 2026-11-22: "改，按内容缓存"):
    one signature per recut atom (its encoded content + the LYMPHLOOP stamps brand put on it, relative to the atom) and per
    audio replacement (file bytes, the seconds taken, the length). None for a recut without per-atom keys."""
    tl = read(str(master) + '.timeline.json')
    segs = tl.get('segments') or []
    if not segs or not all(x.get('atom_key') for x in segs): return None
    stamps = {}
    b = state.get('stages', {}).get('brand') or {}
    if b.get('master', {}).get('sha256') == master_sha and b.get('plan') and Path(b['plan']['path']).is_file():
        stamps = {int(k): v for k, v in read(b['plan']['path']).get('stamps', {}).items()}
    h = lambda o: hashlib.sha256(json.dumps(o, sort_keys=True, default=str).encode()).hexdigest()
    pics = [{'sig': h([x['atom_key'], sorted((f - x['out_in'], stamps[f]) for f in range(x['out_in'], x['out_out']) if f in stamps)]),
             'out_in': x['out_in'], 'out_out': x['out_out'], 'picture_id': x.get('picture_id')} for x in segs]
    files = {}
    def fsha(q):
        if q and q not in files: files[q] = sha(q) if Path(q).is_file() else 'missing:' + q
        return files.get(q)
    auds = [{'sig': h([fsha(r['path']), r.get('clip_in'), r.get('clip_out'), r.get('speed'), r['end_sample'] - r['start_sample'],
                       fsha(r.get('background_path')), r.get('background_clip_in'), r.get('background_gain')]),
             'out_in': r['out_in'], 'out_out': r['out_out'], 'id': r.get('id')} for r in tl.get('audio_replacements') or []]
    return {'master_sha256': master_sha, 'fps': tl.get('output_fps'), 'pictures': pics, 'audio': auds}


def screened_baseline(run, state, content, master_sha, pad=30):
    """Against the last screened master: master frame ranges whose content is new, or whose content was already there
    but carried an open finding (a flagged/unsure/error shot, a watermark left, a competitor word heard, a flashback hit).
    Everything else was screened and looked at before, frame for frame the same picture and sound, only moved.
    Returns None when there is no usable earlier screen."""
    prev = state.get('stages', {}).get('final_screen') or {}
    if not content or not prev.get('dir') or prev.get('master_sha256') == master_sha: return None
    pdir = Path(prev['dir']); pc = pdir/'content.json'
    if not pc.is_file(): return None
    old = read(pc); fps = float(media.ratio(old.get('fps') or content.get('fps') or 30))
    dirty = []
    ids = set(prev.get('replace_ids', [])) | set(prev.get('unsure_ids', [])) | set(prev.get('error_ids', []))
    if ids and (pdir/'shots/shots.json').is_file():
        dirty += [(x['in_frame'], x['out_frame']) for x in read(pdir/'shots/shots.json')['shots'] if x['id'] in ids]
    if (pdir/'watermark/left.json').is_file():
        dirty += [(d['frame'], d['frame'] + 1) for d in read(pdir/'watermark/left.json').get('detections', [])]
    if (pdir/'speech/report.json').is_file():
        dirty += [(int(x['t'] * fps) - 15, int(x['t'] * fps) + 15) for x in read(pdir/'speech/report.json').get('left', [])]
    fr = state.get('stages', {}).get('flashback_review') or {}
    flash_seen = fr.get('master_sha256') == old['master_sha256']
    if flash_seen: dirty += [(h['frames'][0], h['frames'][1] + 1) for h in fr.get('hits', [])]
    hit = lambda x: any(max(x['out_in'], a) < min(x['out_out'], b) for a, b in dirty)
    clean_p = {x['sig'] for x in old['pictures'] if not hit(x)}; clean_a = {x['sig'] for x in old['audio'] if not hit(x)}
    new_p = [x for x in content['pictures'] if x['sig'] not in clean_p]
    new_a = [x for x in content['audio'] if x['sig'] not in clean_a]
    import watermark_brand as wb
    changed = [tuple(r) for r in wb.merge_ranges([(x['out_in'] - pad, x['out_out'] + pad) for x in new_p + new_a])]
    return {'previous_master_sha256': old['master_sha256'], 'previous_dir': str(pdir), 'flashback_seen': flash_seen,
            'changed': changed, 'new_pictures': [x['picture_id'] or 'original' for x in new_p], 'new_audio': [x['id'] for x in new_a],
            'unchanged_atoms': len(content['pictures']) - len(new_p)}


def intersect_ranges(a, b):
    import watermark_brand as wb
    b = wb.merge_ranges(b)
    return [(max(x0, y0), min(x1, y1)) for x0, x1 in a for y0, y1 in b if max(x0, y0) < min(x1, y1)]


def cmd_final_screen(a):
    """Re-run shot detection + Flash screen on the rendered master. Anything flagged goes back to plan."""
    run, state = load(a.run)
    master = screened_master(state, run); master_sha = sha(master)
    # Keyed on the master's bytes: shot sheets, frames and clips are cached by file name (S001...), so a re-rendered
    # master screened into the same folder silently reused the old pictures. Every master gets its own folder.
    fdir = run/'final'/master_sha[:16]
    script('shots.py', 'detect', master, '--out', fdir/'shots', '--threshold', a.threshold)
    ranges = None if a.full else changed_ranges(run, master)
    # content baseline (user 2026-11-22: "改，按内容缓存"): after a goof redo only what is new since the last screened
    # master is screened again; the rest is the same picture and sound at a new place. --full / --no-baseline turn it off.
    content = master_content(state, master, master_sha)
    if content: save(fdir/'content.json', content)
    base = None if ranges is None or getattr(a, 'no_baseline', False) else screened_baseline(run, state, content, master_sha)
    flash_ranges = ranges
    if base:
        ranges = intersect_ranges(ranges, base['changed'])
        if base['flashback_seen']: flash_ranges = ranges
    shots_all = read(fdir/'shots/shots.json')['shots']
    ids = None if ranges is None else [x['id'] for x in shots_all if any(max(x['in_frame'], ra) < min(x['out_frame'], rb) for ra, rb in ranges)]
    changed_grids = []
    if ids == []:
        (fdir/'screen').mkdir(parents=True, exist_ok=True)
        save(fdir/'screen/summary.json', {'replace_ids': [], 'unsure_ids': [], 'error_ids': [], 'note': 'no changed shot to screen'})
    elif agent_native.is_native():
        check_ids = ids if ids is not None else [x['id'] for x in shots_all]
        (fdir/'screen').mkdir(parents=True, exist_ok=True)
        save(fdir/'screen/summary.json', {'replace_ids': [], 'unsure_ids': [], 'error_ids': [],
                                          'mode': 'antigravity_native', 'agent_check_ids': check_ids})
        sheet_paths = [fdir/'shots/sheets'/f'{sid}.png' for sid in check_ids if (fdir/'shots/sheets'/f'{sid}.png').is_file()]
        if sheet_paths:
            changed_grids = agent_native.tile_sheets(sheet_paths, fdir/'agent_review', prefix='changed_shots')
    else:
        script('flash_screen.py', 'screen', '--shots', fdir/'shots/shots.json', '--run', fdir, '--clip-dir', fdir/'clips', '--competitor-run', run,
               *(['--ids', ','.join(ids)] if ids else []))
    data = read(fdir/'shots/shots.json'); summary = read(fdir/'screen/summary.json')
    if data.get('source_sha256') != master_sha: raise SystemExit('final-screen: shot list does not belong to the current master')
    target = watermark_target(run)
    scan_file = run/'watermark/scan.json'
    # The agent confirmed the source has no floating watermark (watermark none): there is no word to look for, and the
    # brand name itself may be printed on our own pack (yesnap creatine -> LymphLoop, 2026-11-11).
    no_mark = scan_file.is_file() and read(scan_file).get('overlay_classification_reviewed') and not any(
        d.get('classification') == 'floating_overlay' for d in read(scan_file).get('detections', []))
    bp = run/'watermark/brand_plan.json'
    places = None if a.full_watermark or not bp.is_file() else (read(bp).get('positions') or None)
    templated = not a.full_watermark and scan_file.is_file() and read(scan_file).get('method') == 'template' and bp.is_file()
    if a.no_watermark_check or no_mark or not target: left, frames = [], None
    elif templated: left, frames = watermark_left_template(master, fdir/'watermark', target, read(bp)), None
    else: left, frames = watermark_left(master, fdir/'watermark', target, ranges=ranges, places=places)
    fps = float(media.probe(str(master))['fps'])
    windows = None if ranges is None else [(ra / fps, rb / fps) for ra, rb in ranges]
    runs = []
    for f in left:
        if runs and f - runs[-1][1] <= 6: runs[-1][1] = f
        else: runs.append([f, f])
    left_times = [f'{x / fps:.1f}-{y / fps:.1f}s' for x, y in runs]
    brand = state.get('stages', {}).get('brand') or {}
    speech = final_speech(run, master, fdir/'speech', a, windows) if windows != [] else {'left': [], 'check': [], 'skipped': 'nothing heard changed since the last screened master'}
    fb = final_flashback(run, master, fdir/'flashback', a, flash_ranges)
    pc = final_product_crops(run, master, fdir/'product', flash_ranges)
    flashback_grids = []
    if agent_native.is_native() and fb.get('dir') and Path(fb['dir']).is_dir():
        fb_files = sorted(Path(fb['dir']).glob('spot_*.jpg'))
        if fb_files:
            flashback_grids = agent_native.stack_flashback_sheets(fb_files, fdir/'agent_review', prefix='flashback')
    if base: save(fdir/'baseline.json', base)
    commit(run, state, 'final_screen', flashback_sheets=len(fb.get('sheets', [])), flashback_frames=fb.get('frames'), product_sheets=len(pc.get('sheets', [])),
           **({'flashback_skipped': fb['skipped']} if fb.get('skipped') else {}), scope=('whole master' if ranges is None else {'ranges': ranges, 'shots': len(ids or []),
                  **({'baseline': {k: base[k] for k in ('previous_master_sha256', 'unchanged_atoms', 'new_pictures', 'new_audio', 'flashback_seen')}} if base else {})}), speech_left=len(speech.get('left', [])), speech_check=len(speech.get('check', [])), shots=len(data['shots']), replace_ids=summary.get('replace_ids', []), unsure_ids=summary.get('unsure_ids', []),
           error_ids=summary.get('error_ids', []), master_sha256=master_sha, dir=str(fdir), watermark_target=target, watermark_frames=len(left), watermark_times=left_times,
           brand_verified=(brand.get('verification') or {}).get('ok') if brand.get('master', {}).get('sha256') == master_sha else None)
    print(json.dumps({'master': str(master), 'master_sha256': master_sha,
                      **({'native_mode': True, 'agent_review_grids': {'changed_shots': changed_grids, 'flashback': flashback_grids}} if agent_native.is_native() else {}),
                      **({'baseline': {k: base[k] for k in ('previous_master_sha256', 'unchanged_atoms', 'new_pictures', 'new_audio', 'flashback_seen')}} if base else {}),
                      'flagged': summary.get('replace_ids', []), 'unsure': summary.get('unsure_ids', []),
                      'errors': summary.get('error_ids', []), 'watermark_target': target, 'watermark_left': left_times,
                      'speech_left': speech.get('left', []), 'speech_check': speech.get('check', []), **({'speech_gate': speech['skipped']} if speech.get('skipped') else {}),
                      **({'flashback': fb['skipped']} if fb.get('skipped') else {'flashback_sheets': len(fb.get('sheets', [])), 'flashback_dir': fb.get('dir')}),
                      'product_sheets': [x['sheet'] for x in pc.get('sheets', [])],
                      'next': '; '.join(x for x in (
                          'watermark left at these times: swap.py watermark add-final, then brand, then final-screen again' if left else 'no watermark left',
                          ('open EVERY sheet in flashback_dir yourself (seams, splice joins, short shots; red frame = the join) and look '
                           'for the competitor product in hands, bags, pockets, on tables and at the edges; then swap.py flashback-review '
                           '--clean, or --hit A-B --note "..." and fix it' if fb.get('sheets') else None),
                          ('open EVERY product sheet (product_sheets: each appearance of the carton or packet cut out at full size) and '
                           'look at its shape: a front taller than wide by more than a quarter, a box thin as a card or a bottle, the narrow '
                           'logo side standing as the front, a packet standing on its own; then swap.py product-review --clean, or --hit '
                           '"秒-秒:看到了什么" for each goof and put it in goofs.json' if pc.get('sheets') else None)) if x),
                      'sheets': str(fdir/'shots/sheets')}, ensure_ascii=False, indent=2))


def splice_joins(run, clip_path, depth=0):
    """Frame offsets (in the clip) where a local_splice recipe joins two pieces, recursively through pieces that are
    themselves splices. [] for a clip that is not a splice result."""
    name = Path(clip_path).name
    m = re.match(r'(G\d+-[^.]+?)-c\d+', name)
    if not m or depth > 4: return []
    lp = run/GEN_DIR/'jobs'/(m.group(1) + '.json')
    if not lp.is_file(): return []
    led = read(lp)
    if led.get('provider') == 'reused_generation': return [int(j) for j in led.get('joins') or []]     # the donor's splice joins travel with the clip
    if led.get('provider') != 'local_splice': return []
    out, pos = [], 0
    for k, pc in enumerate((led.get('recipe') or {}).get('pictures') or []):
        fr = pc.get('frames_inclusive') or pc.get('frames')
        if not fr: continue
        a0, b0 = int(fr[0]), int(fr[1])
        if k: out.append(pos)
        out += [pos + j - a0 for j in splice_joins(run, pc.get('path', ''), depth + 1) if a0 < j <= b0]
        pos += b0 - a0 + 1
    return sorted(set(out))


def master_splice_joins(run, master, fps):
    """Splice joins of every generated picture in the master, as master frames."""
    tl = read(str(master) + '.timeline.json')['segments']; out = []
    for e in tl:
        if not e.get('picture_id') or not e.get('path'): continue
        cfps = fps
        try: cfps = float(media.ratio(media.probe(e['path'])['fps']))
        except Exception: pass
        speed = float(e.get('picture_speed') or 1.0)
        for j in splice_joins(run, e['path']):
            t = j / cfps
            if e.get('clip_in', 0) <= t < e.get('clip_out', 1e9):
                out.append(e['out_in'] + int(round((t - e.get('clip_in', 0)) / speed * fps)))
    return out


def final_flashback(run, master, out_dir, a, ranges=None):
    """Competitor-flashback sheets for the AGENT to look at (user 2026-11-21: "杜绝滴剂瓶闪回"; "我不想过度依赖外部gemini，
    且让gemini看帧版不如agent自己看"). Spots: each seam where a generated picture meets anything, each splice join (from
    the splice recipes: a same-camera join is no cut), each shot of at most 0.5 s, on frame-exact cuts (cutscan; shots.py
    folds such shots into a neighbour and the Flash screen samples 2-3 frames a second). No model call: the sheets are
    written and `swap.py flashback-review` records what the agent saw."""
    if getattr(a, 'no_flashback_check', False): return {'skipped': 'turned off (--no-flashback-check)'}
    import cutscan
    tl_file = Path(str(master) + '.timeline.json')
    if not tl_file.is_file(): return {'skipped': 'no timeline next to the master'}
    raw = cutscan.cuts(master, cache=out_dir/'raw_cuts.json'); fps = raw['fps']
    spots = cutscan.flashback_spots(read(tl_file)['segments'], raw['cuts'], raw['frames'], fps, ranges=ranges,
                                    joins=master_splice_joins(run, master, fps))
    if (out_dir/'sheets').is_dir():
        for old in (out_dir/'sheets').glob('spot_*.jpg'): old.unlink()
    sheets = cutscan.spot_sheets(master, spots, out_dir/'sheets', fps) if spots else []
    res = {'master_sha256': sha(master), 'fps': fps, 'spots': len(spots), 'frames': sum(len(x['frames']) for x in sheets),
           'sheets': sheets, 'dir': str(out_dir/'sheets')}
    save(out_dir/'sheets.json', res)
    return res


def final_product_crops(run, master, out_dir, ranges=None):
    """Product crop sheets of the master for the AGENT (scripts/carton_check.product_crops; user 2026-11-24: "可是我看着还是
    有产品穿帮镜头"): every generated segment that sent carton images (a reused segment: its donor's), limited to the
    changed frames when the screen is a partial one; each appearance of the product cut out at full size, three a second.
    `swap.py product-review` records what the agent saw; finish.py does not reach the board without it."""
    import carton_check
    tl_file = Path(str(master) + '.timeline.json')
    if not tl_file.is_file(): return {'skipped': 'no timeline next to the master'}
    tl = read(tl_file); fps = float(media.ratio(tl.get('output_fps') or 30))
    sel = read(run/'plan/selections.json') if (run/'plan/selections.json').is_file() else {}
    if out_dir.is_dir():
        for old in out_dir.glob('*.jpg'): old.unlink()
    sheets = []
    for e in tl['segments']:
        g = e.get('picture_id')
        if not g: continue
        rf = (sel.get(g) or {}).get('reused_from') or {}
        kind = carton_check.segment_kind(Path(rf['run']), rf['segment']) if rf.get('run') else carton_check.segment_kind(run, g)
        if not kind: continue
        wins = [(e['out_in'], e['out_out'])]
        if ranges is not None:
            wins = [(max(e['out_in'], a), min(e['out_out'], b)) for a, b in ranges if max(e['out_in'], a) < min(e['out_out'], b)]
        for a0, b0 in wins:
            for sh in carton_check.product_crops(master, a0 / fps, b0 / fps, out_dir, name=f'{g}_{a0}'):
                sheets.append({'segment': g, 'from_s': round(a0 / fps, 2), 'to_s': round(b0 / fps, 2), 'sheet': sh})
    res = {'master_sha256': sha(master), 'sheets': sheets, 'dir': str(out_dir)}
    out_dir.mkdir(parents=True, exist_ok=True); save(out_dir/'sheets.json', res)
    return res


def cmd_product_review(a):
    """Record what the agent saw on the product crop sheets of the current final-screen: --clean after opening every
    sheet, or --hit "A-B:what" (master seconds, repeatable). A hit is a goof: put it in goofs.json and redo it."""
    run, state = load(a.run)
    fs = state.get('stages', {}).get('final_screen') or {}
    master = screened_master(state, run)
    sj = Path(fs.get('dir', ''))/'product/sheets.json'
    if not sj.is_file(): raise SystemExit('product-review: run swap.py final-screen first (no product sheets)')
    info = read(sj)
    if info.get('master_sha256') != sha(master): raise SystemExit('product-review: the sheets belong to another master; run final-screen again')
    if bool(a.clean) == bool(a.hit): raise SystemExit('product-review: give --clean or --hit "A-B:看到了什么"')
    hits = []
    for spec in a.hit or []:
        rng, _, what = spec.partition(':')
        if not what.strip(): raise SystemExit(f'product-review: --hit "A-B:看到了什么"，got {spec!r}')
        x, _, y = rng.strip().partition('-'); hits.append({'from_s': float(x), 'to_s': float(y or x), 'what': what.strip()})
    accepted = (getattr(a, 'user_accepted', None) or '').strip()
    if accepted and not hits: raise SystemExit('product-review: --user-accepted goes with --hit (the goofs the user accepted)')
    commit(run, state, 'product_review', master_sha256=info['master_sha256'], sheets=len(info['sheets']),
           verdict=('hit_accepted' if accepted else 'hit') if hits else 'clean', hits=hits, **({'user_accepted': accepted} if accepted else {}))
    print(json.dumps({'verdict': 'hit' if hits else 'clean', 'hits': hits, 'sheets': len(info['sheets']),
                      'next': ('each hit is a goof: list it in goofs.json (why shape / side / physics / count), round open, and run finish again'
                               if hits else 'product check done for this master')}, ensure_ascii=False, indent=2))


def reviews_pending(run, state):
    """What the agent still has to look at for the screened master: flashback sheets and product sheets (finish.py stops
    before the board until both are recorded, user 2026-11-24)."""
    fs = state.get('stages', {}).get('final_screen') or {}
    if not fs.get('dir'): return []
    out = []
    for name, sub in (('flashback_review', 'flashback'), ('product_review', 'product')):
        sj = Path(fs['dir'])/sub/'sheets.json'
        if not sj.is_file() or not read(sj).get('sheets'): continue
        done = state.get('stages', {}).get(name) or {}
        if done.get('master_sha256') != read(sj).get('master_sha256'): out.append({'review': name.replace('_', '-'), 'sheets_dir': str(Path(fs['dir'])/sub)})
        elif name == 'product_review' and done.get('verdict') == 'hit':
            out.append({'review': 'product-review', 'open_hits': done.get('hits'), 'sheets_dir': str(Path(fs['dir'])/sub),
                        'note': 'fix these goofs (goofs.json, round open) or, only with the user\'s own OK, product-review --hit … --user-accepted "用户原话"'})
    return out


def cmd_reviews_pending(a):
    run, state = load(a.run)
    pend = reviews_pending(run, state)
    print(json.dumps({'pending': pend}, ensure_ascii=False, indent=2))
    if pend: raise SystemExit(3)


def cmd_flashback_review(a):
    """Record what the agent saw on the flashback sheets of the current final-screen: --clean after opening every sheet,
    or --hit A-B[,C-D] (master frames) with --note. A hit is a goof: fix it and run final-screen again."""
    run, state = load(a.run)
    fs = state.get('stages', {}).get('final_screen') or {}
    master = screened_master(state, run)
    sj = Path(fs.get('dir', ''))/'flashback/sheets.json'
    if not sj.is_file(): raise SystemExit('flashback-review: run swap.py final-screen first (no flashback sheets)')
    info = read(sj)
    if info.get('master_sha256') != sha(master): raise SystemExit('flashback-review: the sheets belong to another master; run final-screen again')
    if bool(a.clean) == bool(a.hit): raise SystemExit('flashback-review: give --clean or --hit A-B[,C-D]')
    hits = []
    for spec in (a.hit or '').split(','):
        if not spec.strip(): continue
        x, _, y = spec.strip().partition('-'); hits.append([int(x), int(y or x)])
    if hits and not (a.note or '').strip(): raise SystemExit('flashback-review: say what you saw (--note)')
    fps = info['fps']
    commit(run, state, 'flashback_review', master_sha256=info['master_sha256'], sheets=len(info['sheets']), verdict='hit' if hits else 'clean',
           hits=[{'frames': h, 'times': f'{h[0] / fps:.2f}-{(h[1] + 1) / fps:.2f}s'} for h in hits], note=a.note or '')
    print(json.dumps({'verdict': 'hit' if hits else 'clean', 'hits': hits, 'sheets': len(info['sheets']),
                      'next': ('a flashback is a goof: fix that seam or splice (plan edges, shot_regen, splice) and run final-screen again'
                               if hits else 'flashback check done for this master')}, ensure_ascii=False, indent=2))


def final_speech(run, master, out_dir, a, windows=None):
    """Whole-film speech check (speech_gate): the competitor's name or an ingredient we do not have must not be heard
    anywhere in the master; words replaced elsewhere in the film are listed to look at. Offline (faster-whisper)."""
    if getattr(a, 'no_speech_check', False): return {'skipped': 'turned off (--no-speech-check)'}
    try:
        import speech_gate
        return speech_gate.gate(run, master, out_dir, windows=windows)
    except (SystemExit, ImportError) as exc:
        return {'skipped': f'speech check not run: {str(exc)[:200]}'}


def cmd_status(a):
    run, state = load(a.run)
    print(json.dumps({'source': state['source'], 'stages': {k: {kk: vv for kk, vv in v.items() if kk in ('done_at', 'count', 'replace', 'unsure', 'nodes', 'jobs', 'replace_ids', 'modified_frames')} for k, v in state.get('stages', {}).items()}}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- RUN lock
READ_ONLY = {'status', 'sound-audit', 'reviews-pending'}   # plus `review --no-flash` (sheets only); every other command may write into the RUN


def shared_stage(a):
    """Stages that read the prepared master and write only their own folder and state record: they may run at the
    same time (user 2026-11-19: watermark scan 15 min, then detect-pro, then screen, one after another for no reason).
    Each holds the RUN lock shared plus its own stage lock; every other command needs the RUN lock alone."""
    if a.cmd in ('detect-pro', 'screen'): return a.cmd
    if a.cmd == 'merge':
        # Completed detections are immutable while scan holds SH. Merge writes
        # only plan/decisions and its own atomic state record, so it need not
        # wait for the independent full-film watermark OCR. Plan/apply remain EX.
        state_file = Path(a.run)/'state.json'
        stages = read(state_file).get('stages', {}) if state_file.is_file() else {}
        if stages.get('screen') and stages.get('detect_pro'): return 'merge'
    if a.cmd == 'plan':
        # Planning reads the unchanged prepared master; apply/generate are EX,
        # so they cannot change that master or consume a half-written plan.
        state_file = Path(a.run)/'state.json'
        stages = read(state_file).get('stages', {}) if state_file.is_file() else {}
        if stages.get('merge') and not list((Path(a.run)/GEN_DIR/'jobs').glob('*.json')): return 'plan'
    if a.cmd == 'watermark' and getattr(a, 'action', None) == 'scan': return 'watermark-scan'
    if a.cmd == 'generate' and getattr(a, 'refs_only', False): return 'refs'
    return None


@contextlib.contextmanager
def flock(path, shared, wait, what):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as f:
        deadline = time.monotonic() + max(0.0, float(wait or 0))
        while True:
            try:
                oscompat.lock_file(f, exclusive=not shared, blocking=False); break
            except OSError:
                if time.monotonic() >= deadline: raise api.AdapterError(f'Another worker owns {what}') from None
                time.sleep(0.5)
        try: yield
        finally: oscompat.unlock_file(f)


def lock_needed(a):
    return a.cmd not in READ_ONLY and not (a.cmd == 'review' and a.no_flash)


@contextlib.contextmanager
def run_lock(a):
    """One writer per RUN (two agents once wrote into the same RUN: extra attempts, changed selections). flock on
    RUN/.run.lock is released by the kernel when the holder exits or dies, so it can never go stale."""
    run = Path(a.run).resolve()
    if not lock_needed(a) or not (run.is_dir() or a.cmd == 'init'):
        yield; return
    run.mkdir(parents=True, exist_ok=True)
    stack = contextlib.ExitStack()
    stage = shared_stage(a)
    try:
        if stage:
            stack.enter_context(flock(run/'.run.lock', True, a.lock_wait, f'RUN {run}'))
            stack.enter_context(flock(run/f'.run.{stage}.lock', False, a.lock_wait, f'{stage} in RUN {run}'))
        else:
            stack.enter_context(api.lock(run/'.run.lock', wait=a.lock_wait, what=f'RUN {run}'))
    except api.AdapterError as exc:
        raise SystemExit(f'RUN is busy, not touching it: {exc}. One agent/process per RUN; wait for it '
                         f'(or pass --lock-wait SECONDS), never point a second agent at the same RUN.') from None
    with stack:
        yield


# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description=__doc__); s = p.add_subparsers(dest='cmd', required=True)
    def sub(name, fn, **kw):
        q = s.add_parser(name, **kw); q.add_argument('--run', required=True); q.set_defaults(fn=fn)
        q.add_argument('--lock-wait', type=float, default=0, help='seconds to wait for another process holding this RUN (default: fail at once)'); return q
    q = sub('init', cmd_init); q.add_argument('--video', required=True); q.add_argument('--out', required=True)
    q = sub('prepare', cmd_prepare); q.add_argument('--fps', type=int)
    q = sub('competitor', cmd_competitor); q.add_argument('--accept', action='store_true'); q.add_argument('--note'); q.add_argument('--force', action='store_true', help='identify again')
    q.add_argument('--out-of-scope-ok', action='store_true', help='accept a form outside drops/capsules/softgels/tablets/gummies/powders (only on the user\'s word)')
    q = sub('watermark', cmd_watermark); q.add_argument('action', choices=['scan', 'apply', 'none', 'add-final']); q.add_argument('--manual', action='append', help='add-final: A-B:X,Y,W,H master frames and box of a mark OCR missed; repeatable'); q.add_argument('--scan-json'); q.add_argument('--target', help='watermark word (default: from the accepted competitor profile)'); q.add_argument('--note'); q.add_argument('--force', action='store_true'); q.add_argument('--no-edge-pass', action='store_true', help='skip the rotated/enlarged edge-strip OCR (only when the watermark is plainly horizontal and large)'); q.add_argument('--no-position-pass', action='store_true', help='scan: do not re-read the fixed places on every frame'); q.add_argument('--no-place-check', action='store_true', help='scan: keep every place without asking Flash whether it is a watermark or product print'); q.add_argument('--ocr-scan', action='store_true', help='scan: OCR the film even when a stored template of this watermark exists (assets/watermarks)')
    q.add_argument('--fade-pad', type=int, help='scan: frames to hold each run of OCR hits before/after it (fade-in/out the OCR cannot read; default 0.8 s worth); 0 = off')
    q = sub('shots', cmd_shots); q.add_argument('--threshold', default='0.3'); q.add_argument('--min-shot-seconds', default='0.4'); q.add_argument('--fps-sample', default='2')
    q = sub('detect-pro', cmd_detect_pro); q.add_argument('--fps', type=float, default=5.0); q.add_argument('--wait', type=int, default=1800)
    q.add_argument('--import-nodes', help='native mode: import agent-written pro_nodes.json and commit detect_pro')
    q = sub('screen', cmd_screen); q.add_argument('--ids')
    q.add_argument('--import-verdicts', help='native mode: import compact verdicts JSON {replace_ids, unsure_ids, notes} and commit screen')
    sub('merge', cmd_merge)
    q = sub('plan', cmd_plan); q.add_argument('--dry-run', action='store_true', help='print the segmentation and its speech, write nothing'); q.add_argument('--no-speech', action='store_true', help='skip transcription (shot-boundary packing only)')
    q.add_argument('--runs-only', action='store_true', help='old rule: segment consecutive replace shots only (no paragraph bridging, no scenes)'); q.add_argument('--no-scenes', action='store_true', help='no lock labelling: the whole video is one block')
    q.add_argument('--no-edge-snap', action='store_true', help='keep segment edges where the shot list put them (default: onto frame-exact cuts, pulling pieces under 0.5 s next to an edge into the segment)')
    q = sub('generate', cmd_generate); q.add_argument('--segment'); q.add_argument('--attempt', type=int, default=1)
    q.add_argument('--candidates', type=int, default=1, choices=[1])
    q.add_argument('--prepare-only', action='store_true', help='write and preview the exact upload references locally, without calling APIMART')
    q.add_argument('--views', help="override the prompt file's '# views:' line, comma separated, order = @图片1, @图片2 ...")
    q.add_argument('--aspect-ratio', choices=[seedance.ASPECT_RATIO], help='fixed 9:16, inherited from the reference video')
    q.add_argument('--ignore-uplink', action='store_true', help='submit although the tunnel looks too slow for APIMART to fetch the references')
    q.add_argument('--refs-only', action='store_true', help='build the cleaned upload references only (run right after plan, while the prompts are written; shares the RUN with other work)')
    q.add_argument('--caption-box', action='append', metavar='G##:x,y,w,h@a-b', help='with --prepare-only: caption text the cleaning left in that segment\'s reference '
                   '(box as fractions of the frame, at most one caption line high; seconds from the segment start, b may be end); inpainted with the captions. Never a product, person or hand');     q.add_argument('--erase', action='append', metavar='G##:x,y,w,h@a-b', help='remove a competitor object from that segment\'s reference with LaMa (box as fractions of the frame, seconds from the segment start, b may be end); people and hands stay; repeatable')
    q.add_argument('--allow-reference-competitor', action='append', metavar='G##', help='submit although Flash still sees a competitor object in the reference (only a false alarm); say why in the delivery notes')
    q.add_argument('--allow-reference-crop', action='append', metavar='G##', help='submit although the reference crops a face out of the original (only when that face is not a person of the scene: a poster, a packaging photo); say why in the delivery notes')
    q.add_argument('--user-approved-regen', action='append', metavar='G##', help='the user asked in chat to regenerate this already generated segment: prepares/submits G##-a2 (next number) from prompts/G##-a2.txt; needs --user-quote')
    q.add_argument('--user-quote', help="the user's own words asking for the regeneration, stored in the job ledger")
    q.add_argument('--mute', action='append', help='SEG:A-B[,A-B] silence only the conflict word(s) in that segment\'s reference (seconds from segment start) for Seedance to re-voice them; the picture is never repaired; repeatable')
    q = sub('review', cmd_review); q.add_argument('--segment'); q.add_argument('--fps', default='2'); q.add_argument('--no-flash', action='store_true')
    q = sub('select', cmd_select); q.add_argument('--job', required=True); q.add_argument('--candidate', help='c1; not needed with --keep-source-picture on a segment that was never generated'); q.add_argument('--clip-in', type=float, help='default: first frame; choose a reviewed picture-and-sound cut'); q.add_argument('--clip-out', type=float, help='default: end of the candidate'); q.add_argument('--note'); q.add_argument('--keep-source-picture', action='store_true', help='leave the original picture and audio in place for this segment (also for a planned segment that was never submitted: no ledger needed)'); q.add_argument('--audio-only', action='store_true', help='narration-only rewrite: keep the source picture, take the candidate\'s re-voiced soundtrack (no TTS)'); q.add_argument('--no-caption-clean', action='store_true', help='keep the candidate as generated even if burned-in captions survived'); q.add_argument('--keep-frozen-tail', action='store_true', help='keep a still picture at the end of the clip (only when the agent looked and the original shot is itself still there)')
    q.add_argument('--sound', choices=['candidate', 'source'], default=None, help='candidate (default): the picture with its own generated sound; source: the source sound under the picture, only where no mouth speaks (needs --no-mouth on a segment with on-screen dialogue)')
    q.add_argument('--no-mouth', action='store_true', help='you looked at the whole clip: no speaking mouth is visible anywhere in it')
    q.add_argument('--audit', help='structured 6-item goof check (JSON string, file path, or k=v list: competitor_clean,front_face_ok,scale_ok,physics_ok,speech_lip_ok,text_clean)')
    q.add_argument('--carton-ok', help='after opening the carton check sheet with read_file: why the flagged frames are not a goof, or that they went into goofs.json (scripts/carton_check.py)')
    q.add_argument('--allow-audit-fail', action='store_true', help='allow selecting even when an --audit field is false (must be noted in delivery notes)')
    q = sub('source-sound', cmd_source_sound); q.add_argument('--job', required=True); q.add_argument('--candidate', default='c1')
    q = sub('splice', cmd_splice, help='cut the right parts of several attempts into one new candidate G##-aN-c1 (local_splice ledger)')
    q.add_argument('--segment', required=True); q.add_argument('--out-attempt', required=True, help='aN: new attempt id, e.g. a7')
    q.add_argument('--picture', action='append', required=True, help='SRC:A-B inclusive frames of SRC (G05-a1-c1, a results/ name, or a path); SRC:A-B@N fits those frames into N output frames (a shot generated from a slowed reference); repeat in order')
    q.add_argument('--audio', help='optional off-screen audio from 0 s; default: each picture piece with its own audio')
    q.add_argument('--no-mouth', action='append', help='A-B[,C-D] seconds of the new clip where you looked and no mouth speaks: only there may the sound be another clip\'s, the source\'s or shifted')
    q.add_argument('--audio-window', action='append', help='SRC:A-B@T paste SRC audio A-B s at T s of the splice (short crossfades); repeatable')
    q.add_argument('--mute-window', action='append', help='A-B[,A-B] silence these seconds of the splice audio (short ramps); repeatable')
    q.add_argument('--hold-last', type=int, default=0, help='intentional hold on a non-speaking picture'); q.add_argument('--note')
    q.add_argument('--force', action='store_true', help='overwrite an earlier splice with the same id (never a real generation)')
    q = sub('recut', cmd_recut)
    q.add_argument('--user-approved-cut', action='append', metavar='G##',
                   help='only when the USER explicitly agreed that this segment may lose original content (shots, sentences)')
    q.add_argument('--content-preserved-review', metavar='JSON',
                   help='SHA-bound owner review that a native splice retains every original shot/sentence despite shorter duration; no deletion permission')
    q = sub('brand', cmd_brand)
    q = sub('reuse', cmd_reuse); q.add_argument('--from', dest='donor', action='append', required=True, metavar='RUN', help='a finished RUN whose film shares footage with this one (repeat; the first given wins where two overlap)')
    q.add_argument('--dry-run', action='store_true', help='only report what would be reused'); q.add_argument('--unfinished-donor', action='store_true', help='take the donor\'s current picks although it has no recut / final-screen yet'); q.add_argument('--tolerance', type=float, default=1.0, help='scale the same-frame limits (default 1; 2 for a heavily re-encoded copy: then look at every reused seam)')
    q = sub('captions', cmd_captions); q.add_argument('--draw-all', action='store_true', help='draw every line in the measured style instead of copying the film\'s own captions'); q.add_argument('--segment', help='only this segment, e.g. G03'); q.add_argument('--font', help='use this installed face (a name from font_alternatives, e.g. "Avenir Next Demi Bold") and measure its size again'); q.add_argument('--remeasure', action='store_true', help='measure the caption style again (default: reuse RUN/captions/style.json, which may have been edited)'); q.add_argument('--force', action='store_true', help='caption every segment again (default: only segments whose clip, lines or style changed)')
    q = sub('sound-audit', cmd_sound_audit, help='read-only: selected splices whose sound is not their picture\'s own')
    q = sub('flashback-review', cmd_flashback_review); q.add_argument('--clean', action='store_true', help='every flashback sheet opened and looked at: no competitor object'); q.add_argument('--hit', default='', help='master frames A-B[,C-D] where the competitor product shows'); q.add_argument('--note', default='')
    q = sub('product-review', cmd_product_review); q.add_argument('--clean', action='store_true', help='every product sheet opened and looked at: no goof'); q.add_argument('--hit', action='append', help='"A-B:看到了什么" in master seconds (repeat)'); q.add_argument('--user-accepted', help="the user's own words allowing delivery with these goofs")
    q = sub('reviews-pending', cmd_reviews_pending, help='read-only: flashback / product sheets of the screened master the agent has not reviewed (exit 3 if any)')
    q = sub('final-screen', cmd_final_screen); q.add_argument('--threshold', default='0.3'); q.add_argument('--no-watermark-check', action='store_true'); q.add_argument('--no-speech-check', action='store_true', help='skip the whole-film speech check (speech_gate.py)'); q.add_argument('--full', action='store_true', help='screen, OCR and hear the whole master (default: only what was changed, plus the original watermark frames)'); q.add_argument('--no-baseline', action='store_true', help='screen every generated part again instead of only what is new since the last screened master'); q.add_argument('--full-watermark', action='store_true', help='re-scan the watermark with full-frame + edge-strip OCR instead of only its fixed places')
    q.add_argument('--no-flashback-check', action='store_true', help='skip the competitor-flashback check at seams and short shots')
    sub('status', cmd_status)
    a = p.parse_args()
    import words as words_mod
    words_mod.set_run(Path(a.run).resolve())          # whisper hears the film's own language (detected once per RUN)
    with run_lock(a): a.fn(a)


if __name__ == '__main__': main()
