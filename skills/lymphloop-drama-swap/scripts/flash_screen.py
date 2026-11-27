#!/usr/bin/env python3
"""Gemini 3.8 Flash as a recall-biased per-shot screener and a second-opinion QA reviewer. Cached, JSON-only."""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse
import json
import sys
from pathlib import Path
import competitor
import google_api as api
import media_pipeline as media
import shots

MODEL = 'gemini-3.8-flash'
SCREEN_WORKERS = 12  # parallel per-shot Flash calls (user 2026-11-19: 281 shots took 21 min at 6)
SKILL = Path(__file__).resolve().parents[1]
DEFAULT_FPS = 3
# How the shot's person takes a supplement. Anything but other/none is the competitor's way of taking it (2026-11-11:
# any competitor form, not only the dropper): drinking plain water or coffee is 'other'.
INGESTION = ['dropper', 'bottle', 'capsule_or_tablet', 'softgel', 'gummy', 'powder_or_drink_mix', 'other', 'none']
COMPETITOR_INGESTION = set(INGESTION) - {'other', 'none'}

SCREEN_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'competitor_visible': {'type': 'STRING', 'enum': ['yes', 'no', 'unsure']},
        'visible_evidence': {'type': 'STRING'},
        'brand_text': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'ingestion_action': {'type': 'STRING', 'enum': INGESTION},
        'speech_present': {'type': 'BOOLEAN'},
        'speech_mentions_product': {'type': 'STRING', 'enum': ['yes', 'no', 'unsure']},
        'speech_transcript': {'type': 'STRING'},
        'captions_present': {'type': 'BOOLEAN'},
        'floating_watermark': {'type': 'BOOLEAN'},
        'confidence': {'type': 'NUMBER'},
    },
    'required': ['competitor_visible', 'visible_evidence', 'brand_text', 'ingestion_action', 'speech_present',
                 'speech_mentions_product', 'speech_transcript', 'captions_present', 'floating_watermark', 'confidence'],
}

QA_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'product_consistency': {'type': 'INTEGER'},
        'action_fidelity': {'type': 'INTEGER'},
        'identity_scene_consistency': {'type': 'INTEGER'},
        'competitor_residue': {'type': 'BOOLEAN'},
        'subtitles_present': {'type': 'BOOLEAN'},
        'watermark_present': {'type': 'BOOLEAN'},
        'artifacts': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'lip_sync': {'type': 'STRING', 'enum': ['n/a', 'good', 'off', 'missing']},
        'speech_transcript': {'type': 'STRING'},
        'verdict': {'type': 'STRING', 'enum': ['accept', 'revise', 'reject']},
        'goofs': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'findings': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
    },
    'required': ['product_consistency', 'action_fidelity', 'identity_scene_consistency', 'competitor_residue',
                 'subtitles_present', 'watermark_present', 'artifacts', 'lip_sync', 'speech_transcript', 'verdict', 'findings'],
}


# Segments that show the carton get a dedicated geometry check (2026-10-25): with only the product described in words,
# Flash scored stretched, sliver-thin, fingertip-sized and multiplied cartons as fine (3 of 356 batch reviews mentioned
# proportions). Wan copies the replaced object's outline, count and grip: three dropper bottles in a row became three
# slim cartons (302 G06), a vial pinched in the fingertips became a carton pinched in the fingertips (301 G13).
GOOFS_EN = ('GOOFS (the only things that fail a candidate, user 2026-11-20: "only look for goofs; proportions that are about right are not a defect"): (1) any competitor container, dropper, dose unit, label or brand text visible, even small, in the background or for a few frames (also flashing back at a cut); (2) a different number of products than the prompt asks for, or a product appearing / vanishing / turning into another object mid-shot; (3) product shape grossly wrong: a carton that looks like a bottle, a thin card, a matchbox, a shoebox or a flat brick, bent or melted, or a packet that is rigid like a stick or swallowed whole like a capsule; (4) the narrow side or the back presented as the front for a whole showing shot; (5) physically impossible handling: a packet standing upright with nothing holding it, a packet torn open on screen, an ingestion shot with an empty hand; (6) a person, face, wardrobe or setting changed; (7) a changed line not spoken, a competitor name or a removed ingredient spoken, or speech clearly out of sync with the lips; (8) captions, a watermark or the competitor watermark in the picture. NOT goofs (record at most, never a reason to revise or reject): proportions within about a quarter of the true ratio, a tilted carton showing its top or looking a little squat or tall, blurry or garbled small printing, slight stiffness or finger smear, a drip at the corner of the mouth, pouring from above, a mirrored picture that stays consistent across the segment.')
CARTON_GEOMETRY = ['ok', 'too_slim', 'too_squat', 'too_small', 'too_large', 'inconsistent', 'duplicated', 'warped', 'n/a']
CARTON_SCHEMA = {
    'type': 'OBJECT',
    'properties': {**QA_SCHEMA['properties'],
                   'carton_geometry': {'type': 'STRING', 'enum': CARTON_GEOMETRY},
                   'carton_count_max': {'type': 'INTEGER'},
                   'carton_findings': {'type': 'ARRAY', 'items': {'type': 'STRING'}}},
    'required': QA_SCHEMA['required'] + ['carton_geometry', 'carton_count_max', 'carton_findings'],
}
CARTON_BAD = set(CARTON_GEOMETRY) - {'ok', 'n/a'}


def carton_prompt():
    return (
        'CARTON CHECK (the attached CARTON REFERENCE is ONE carton shown in four views on an open palm). The real carton is '
        '10 cm wide, 13 cm tall and 7 cm deep: its front is only slightly taller than wide (height about 1.3 x width), its '
        'depth is about 0.7 x its width, and its front is about as wide as an adult palm. It is about three times wider '
        'than a typical supplement bottle and about as tall. Judge every carton in the GENERATED candidate, every shot:\n'
        '- too_slim: grossly tall and narrow, like a bottle: front narrower than about half its height, or a thin sliver/card;\n'
        '- too_squat: grossly flat: a brick, a pizza box or a tile, front clearly wider than tall even after allowing for tilt;\n'
        '- too_small: smaller than a palm, matchbox-like, pinched between fingertips like a small vial. Size anchors: the '
        '13 cm height is clearly LONGER than an adult finger (about 8 cm) and the 10 cm front is about a palm wide, so a '
        'hand can only grip its two sides with the fingers spread or cradle it in the palm; a carton no longer than the '
        'fingers holding it, or held like a card between thumb and fingertips, is too_small;\n'
        '- too_large: wider than the hand holding it by far, or dwarfing nearby objects;\n'
        '- inconsistent: its proportions or size change between shots or frames;\n'
        '- duplicated: more cartons than the prompt asks for (the prompt states the count), or copies spawning;\n'
        '- warped: bent, melted, non-rectangular edges;\n'
        '- ok: right proportions in every shot; n/a: no carton appears.\n'
        'Perspective is allowed: a carton turned to the side shows its narrower 7 cm side, so judge the face you see by '
        'its own true ratio (side face height about 1.9 x its width); a carton tilted back shows its top and its front looks '
        'shorter -- that is ok. Proportions that are about right (within about a quarter of the true ratio) are ok: only a '
        'gross error is a fail (user 2026-11-20: "只要比例差不多正确，不要大幅度偏差都不算瑕疵"). '
        'carton_count_max = most cartons visible at once. '
        'carton_findings: shot time and what is wrong.')


def product_facts():
    profile = api.read(SKILL / 'assets/product/profile.json')
    return json.dumps({k: profile.get(k) for k in ('name', 'product_id', 'packaging', 'use', 'positive_relative_scale_prompt')
                       if profile.get(k) is not None}, ensure_ascii=False)


def competitor_facts(run=None):
    """The competitor as detection sees it: the accepted RUN/detect/competitor.json, else the built-in Lymphoria profile."""
    return competitor.facts_text(competitor.active(run))


def competitor_anchor_parts(run=None):
    """Competitor anchor images attached to detection prompts only (never to generation)."""
    return [('image', p) for p in competitor.anchor_images(competitor.active(run))]


def screen_prompt(run=None):
    return (
        'You are screening one shot of a short advertisement for a product-replacement job.\n'
        f'TARGET PRODUCT (the only product allowed to appear): {product_facts()}\n'
        f'COMPETITOR in this ad (the attached still images are frames of it): {competitor_facts(run)}\n'
        'Report anything that CONFLICTS with the target product: the competitor container (bottle, jar, tub, pouch, '
        'blister, sachet, box) or ANY of its dose units (drops, dropper, pipette, capsules, softgels, tablets, gummies, '
        'powder, scoop), even loose in a palm, on a table or in a bowl without the pack; any supplement packaging that is '
        'not the target; any brand name, logo or label text that is not the target brand; the competitor being taken or '
        'prepared (ingestion_action: how, with which dose form; drinking plain water or coffee is "other"); ingredient '
        'close-ups; the speaker or voice-over naming a competitor or describing its form or dose. Also report whether '
        'burned-in captions exist, whether a floating watermark text drifts over the picture, and whether anyone speaks.\n'
        'Err on the side of recall: if a product-like object, dose unit or label is partly visible, blurred, reflected or '
        'uncertain, answer "unsure" rather than "no". A hand closed around the competitor container counts as '
        'competitor_visible="yes" even when no label or text can be read; a cap, neck, pipette, capsule or gummy '
        'peeking out of a fist is enough. Transcribe speech verbatim when present. '
        'Answer strictly in the JSON schema.')


def qa_prompt(task_kind, run=None):
    lip = ('This is a DIALOGUE task: judge whether the mouth of the speaking person moves in sync with the new speech '
           '(good/off/missing).' if task_kind == 'dialogue' else 'This is a VISUAL task: set lip_sync to "n/a".')
    return (
        'You compare an ORIGINAL source segment with a GENERATED candidate that should replace the competitor product '
        f'with the target product while keeping everything else. TARGET PRODUCT: {product_facts()}\n'
        f'COMPETITOR that must be gone (container AND dose units): {competitor_facts(run)}\n'
        'Score 0-100: product_consistency (shape, colour, printing, scale relative to the hand, flexible film behaviour, '
        'no leftover competitor container or dose-unit geometry); action_fidelity (the action stages match the prompt and the intent of the '
        'source: a sealed packet shown/held; or an already-opened packet raised to closed lips and sipped (a hard cut may '
        'replace the opening); tearing on screen is a defect); '
        'identity_scene_consistency (same person, wardrobe, setting, framing, camera motion and rhythm as the source). '
        'Flag competitor_residue when any competitor object, label or text survives; flag subtitles_present and '
        f'watermark_present when they appear in the candidate. {lip} List concrete artifacts (extra fingers, warped '
        'label, flicker, morphing, duplicate product, open mouth or tongue during the sip). Two defects always go in '
        'artifacts with their time and rule out accept: a packet that stands upright on a surface, a shelf or in a drawer '
        'with nothing holding or supporting it ("packet stands unsupported at MM:SS"); and a shot where the source shows '
        'the product being taken (hand to the mouth, swallowing, a drink right after) while the candidate hand holds no '
        'target packet ("no packet in the ingestion shot at MM:SS": an empty hand is a failed replacement, not a clean one). '
        f'{GOOFS_EN} List every goof in goofs with its time ("MM:SS what"); minor issues go to artifacts only. '
        'verdict: reject when a goof stays visible in the film, revise when a goof can be cut out by a trim, otherwise '
        'accept -- the scores are a record and never decide the verdict on their own. '
        'Give short, specific findings. Answer strictly in the JSON schema.')


REFERENCE_RESIDUE_SCHEMA = {
    'type': 'object',
    'properties': {
        'items': {'type': 'array', 'items': {'type': 'object', 'properties': {
            't': {'type': 'number'},
            'kind': {'type': 'string', 'enum': ['container', 'dose_unit', 'spoon', 'dropper', 'cap', 'label_text', 'other']},
            'size': {'type': 'string', 'enum': ['large', 'medium', 'small']},
            'state': {'type': 'string', 'enum': ['held', 'standing', 'lying', 'pouring', 'opening', 'spooning', 'other']},
            'where': {'type': 'string'}},
            'required': ['t', 'kind', 'size', 'state', 'where']}},
        'notes': {'type': 'string'}},
    'required': ['items'],
}


def reference_residue_prompt(run=None):
    return (
        'This clip is the REFERENCE VIDEO that a video generator will redraw, replacing the competitor product with the '
        f'target product. TARGET PRODUCT: {product_facts()}\n'
        f'COMPETITOR (the attached stills are frames of it): {competitor_facts(run)}\n'
        'The generator copies whatever it can see in the reference: a competitor bottle left in a hand, at the frame edge, '
        'on a counter or in a wide shot comes back in the output, and so do its actions. List EVERY competitor object '
        'still visible anywhere in this clip, however small, blurred or partly hidden: the container, its cap or dropper, '
        'its dose units, a spoon or scoop used with it, its label text. For each give the time in seconds, kind, size '
        '(large = the object is the subject of a close-up and at least a quarter of the frame height; medium = clearly '
        'visible but not the subject; small = a few dozen pixels, at the edge, in the background or in a wide shot), state '
        '(held in a hand, standing on a surface, lying, pouring, being opened or unscrewed, spooning) and where it is in '
        'the frame. Objects of the TARGET product are not competitor objects. If nothing is left, return an empty list. '
        'Answer strictly in the JSON schema.')


def reference_residue(video, run_dir, competitor_run=None, transport=None, fps=DEFAULT_FPS):
    """Competitor objects still visible in a reference video before it is submitted (cached Flash call)."""
    crun = Path(competitor_run).resolve() if competitor_run else Path(run_dir).resolve()
    state = call_flash([('text', reference_residue_prompt(crun)), *competitor_anchor_parts(crun), ('text', 'REFERENCE VIDEO:'), ('video', str(video))],
                       REFERENCE_RESIDUE_SCHEMA, run_dir, transport, fps)
    return state['result'].get('items') or []


def _part_identity(part):
    kind, value = part
    if kind == 'text':
        return {'text': value}
    return {kind: api.file_sha(value)}


def _build_parts(parts, run_dir, transport, fps):
    built = []; budget = [api.REQUEST_INLINE_BUDGET]
    for kind, value in parts:
        if kind == 'text':
            built.append({'text': value})
        elif kind == 'video':
            # small clips inline, large ones through the Files store (2026-11-22: upload 429 with 25 films at once)
            built.append(api.media_part(value, run_dir, transport, budget, video_fps=fps))
        elif kind == 'image':
            img = api.file_description(value); budget[0] -= img['bytes']
            built.append(api.inline_image(img, interactions=False))
        else:
            raise api.AdapterError('Unknown part kind: ' + str(kind))
    return built


NO_JSON_RETRIES = 2   # extra calls with a stricter instruction when Flash answers without a parseable JSON object
STRICT_JSON = ('Your previous answer was not one valid JSON object. Answer again with exactly ONE JSON object that matches '
               'the response schema: no prose, no markdown code fences, nothing before or after the object.')


def parse_json_object(text):
    """The JSON object in a Flash answer: plain, inside ```json fences, or between the first '{' and the last '}'."""
    text = (text or '').strip()
    candidates = [text]
    if text.startswith('```'):
        candidates.append(text.strip('`').split('\n', 1)[-1].rsplit('```', 1)[0])
    if '{' in text and '}' in text:
        candidates.append(text[text.index('{'):text.rindex('}') + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def call_flash(parts, schema, run_dir, transport=None, fps=DEFAULT_FPS, retries=NO_JSON_RETRIES):
    """One cached generateContent call. Disabled by default in Antigravity native zero-Gemini mode."""
    import agent_native
    if agent_native.is_native() and transport is None:
        raise api.AdapterError('External Gemini Flash is disabled in Antigravity native mode (set LYMPHLOOP_ALLOW_EXTERNAL_GEMINI=1 to override)')
    fps = max(2, min(5, int(fps)))
    identity = api.sha(api.json_bytes({'model': MODEL, 'fps': fps, 'schema': schema, 'parts': [_part_identity(p) for p in parts]}))
    directory = Path(run_dir).resolve() / 'flash' / identity
    directory.mkdir(parents=True, exist_ok=True)
    state_file = directory / 'result.json'
    with api.lock(directory / '.lock', wait=900, what='this Flash request'):
        if state_file.exists():
            state = api.read(state_file)
            if state.get('status') == 'completed':
                state['cache_hit'] = True
                return state
            if state.get('status') == 'known_failed':
                raise api.AdapterError(f"Flash request {identity[:12]} failed earlier with HTTP {state.get('http_status')}; fix the input before resubmitting")
        transport = transport or api.Transport(api.required_key())
        built = _build_parts(parts, run_dir, transport, fps)
        state = {'type': 'lymphloop_flash_call', 'model': MODEL, 'request_id': identity, 'status': 'submitting',
                 'inputs': [_part_identity(p) for p in parts if p[0] != 'text']}
        no_json = []
        for attempt in range(1 + max(0, int(retries))):
            extra = [{'text': STRICT_JSON}] if attempt else []
            payload = {'contents': [{'role': 'user', 'parts': built + extra}],
                       'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json', 'responseSchema': schema}}
            api.save(directory / 'request.json', api.safe(payload, transport.key))
            state.update(status='submitting', attempts=attempt + 1)
            api.save(state_file, state)
            try:
                response = transport.json(api.API + '/v1beta/models/' + MODEL + ':generateContent', method='POST', payload=payload, timeout=300)
            except api.HTTPFailure as exc:
                state.update(status='known_failed' if 400 <= exc.code < 500 else 'outcome_unknown', http_status=exc.code)
                api.save(state_file, state)
                raise
            except Exception as exc:
                state.update(status='outcome_unknown', error_type=type(exc).__name__)
                api.save(state_file, state)
                raise
            api.save(directory / 'response.json', api.safe(response, transport.key))
            text = '\n'.join(p.get('text', '') for c in response.get('candidates', []) for p in c.get('content', {}).get('parts', []) if not p.get('thought'))
            result = parse_json_object(text)
            if result is not None:
                state.update(status='completed', response_id=response.get('responseId'), usage=api.safe(response.get('usageMetadata')),
                             result=result, cache_hit=False, **({'no_json_attempts': no_json} if no_json else {}))
                api.save(state_file, state)
                return state
            finish = next((c.get('finishReason') for c in response.get('candidates', []) if c.get('finishReason')), None)
            no_json.append({'finish_reason': finish, 'raw_text': text[:500]})
        state.update(status='no_json_output', no_json_attempts=no_json, raw_text=text[:2000])
        api.save(state_file, state)
        raise api.AdapterError(f'Flash returned no JSON object ({len(no_json)} tries, last finish reason {no_json[-1]["finish_reason"]})')


def verdict_for(raw, run=None):
    reasons = []
    if raw.get('competitor_visible') in {'yes', 'unsure'}:
        reasons.append('competitor_visible=' + raw['competitor_visible'])
    if raw.get('ingestion_action') in COMPETITOR_INGESTION:
        reasons.append('ingestion_action=' + raw['ingestion_action'])
    brands = [b for b in raw.get('brand_text') or [] if isinstance(b, str) and b.strip()]
    foreign = [b for b in brands if 'lymphloop' not in b.lower().replace(' ', '')]
    if raw.get('speech_mentions_product') in {'yes', 'unsure'}:
        if _target_speech(raw.get('speech_transcript'), run):
            # Speech that names only the target product (e.g. in a rendered master or a dialogue
            # candidate) is the swap working, not a conflict.
            notes_extra = ['speech_target_product']
        else:
            reasons.append('speech_mentions_product=' + raw['speech_mentions_product'])
            notes_extra = []
    else:
        notes_extra = []
    # Brand text on its own (no object, no ingestion, no speech) is usually the model reading the
    # attached competitor stills or a faint overlay ghost; it cannot justify a paid regeneration.
    # It is kept as a note for the agent's grid review, which already covers these shots.
    if foreign and reasons:
        reasons.append('brand_text=' + ','.join(foreign[:4]))
    notes = ['brand_text_only=' + ','.join(foreign[:4])] if foreign and not reasons else []
    return ('replace' if reasons else 'keep'), reasons + notes + notes_extra


def _competitor_names(run=None):
    return competitor.names(competitor.active(run)) or {'lymphoria'}


def _target_speech(transcript, run=None):
    """True when the transcript names the target product and no competitor brand."""
    text = (transcript or '').lower().replace(' ', '').replace('-', '')
    if not text or not any(k in text for k in ('lymphloop', 'yesnap')):
        return False
    return not any(name in text for name in _competitor_names(run))


def screen(shots_json, run_dir, ids=None, clip_dir=None, transport=None, fps=DEFAULT_FPS, competitor_run=None):
    """competitor_run: the RUN whose detect/competitor.json describes the competitor (final-screen caches its calls in
    RUN/final/<sha>, which has no profile of its own)."""
    record = api.read(shots_json)
    run = Path(run_dir).resolve()
    crun = Path(competitor_run).resolve() if competitor_run else run
    clips = Path(clip_dir).resolve() if clip_dir else run / 'clips'
    clips.mkdir(parents=True, exist_ok=True)
    (run / 'screen').mkdir(parents=True, exist_ok=True)
    wanted = set(ids) if ids else None
    todo = [shot for shot in record['shots'] if not wanted or shot['id'] in wanted]

    failures = []

    def one(shot):
        clip = clips / (shot['id'] + '.mp4'); meta = clips / (shot['id'] + '.clip.json')
        # A clip is reused only when it was cut from these exact source bytes and frames (a re-rendered master keeps
        # the shot ids S001... but not the pictures).
        key = {'source_sha256': record.get('source_sha256') or api.file_sha(record['source']), 'in_frame': shot['in_frame'], 'out_frame': shot['out_frame']}
        if not (clip.is_file() and meta.is_file() and api.read(meta) == key):
            shots.cut(record['source'], shot['in_frame'], shot['out_frame'], clip)
            api.save(meta, key)
        try:
            state = call_flash([('text', screen_prompt(crun)), *competitor_anchor_parts(crun), ('text', 'SHOT TO SCREEN:'), ('video', str(clip))], SCREEN_SCHEMA, run, transport, fps)
        except api.AdapterError as exc:
            failures.append(exc)
            # One unanswerable shot must not sink the whole screen: record it (listed as unsure + error) and go on;
            # re-running screen retries it (a no-JSON answer is not cached as final).
            row = {'id': shot['id'], 'in_frame': shot['in_frame'], 'out_frame': shot['out_frame'], 'clip': str(clip),
                   'verdict': 'error', 'reasons': ['flash_error: ' + str(exc)[:300]], 'raw': {}, 'error': str(exc)[:500]}
            api.save(run / 'screen' / (shot['id'] + '.json'), row)
            return row
        raw = state['result']
        verdict, reasons = verdict_for(raw, crun)
        row = {'id': shot['id'], 'in_frame': shot['in_frame'], 'out_frame': shot['out_frame'], 'clip': str(clip),
               'verdict': verdict, 'reasons': reasons, 'raw': raw, 'request_id': state['request_id'], 'cache_hit': state.get('cache_hit', False)}
        api.save(run / 'screen' / (shot['id'] + '.json'), row)
        return row

    # Shots are independent and every Flash call is cached and locked on its own: screen them in parallel
    # (one at a time took ~32 s per shot, ~100 min for a 188-shot ad; 2026-10-24).
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1 if transport is not None else SCREEN_WORKERS) as ex:   # an injected transport may not be thread-safe
        rows = list(ex.map(one, todo))
    errors = [r['id'] for r in rows if r['verdict'] == 'error']
    if rows and len(errors) == len(rows):
        raise failures[0]          # nothing screened at all (credentials, quota, a rejected input): fail loudly
    summary = {'type': 'lymphloop_flash_screen_summary', 'model': MODEL, 'shots_json': str(Path(shots_json).resolve()),
               'source_sha256': record.get('source_sha256'), 'shots': rows,
               'replace_ids': [r['id'] for r in rows if r['verdict'] == 'replace'],
               'unsure_ids': [r['id'] for r in rows if r['verdict'] == 'error' or r['raw'].get('competitor_visible') == 'unsure' or r['raw'].get('speech_mentions_product') == 'unsure'],
               'keep_ids': [r['id'] for r in rows if r['verdict'] == 'keep'], 'error_ids': errors}
    api.save(run / 'screen' / 'summary.json', summary)
    return summary


def qa(source, candidate, prompt_path, out, run_dir, task_kind='visual', transport=None, fps=DEFAULT_FPS, carton_ref=None):
    """Second opinion on one candidate, recorded as defects (every segment is generated once and used, user 2026-11-11).
    carton_ref (the carton_views image the job sent) adds the carton geometry check; a bad carton is recorded as carton_gate=fail."""
    prompt_text = Path(prompt_path).read_text()
    parts = [('text', qa_prompt(task_kind, run_dir)), ('text', 'ORIGINAL source segment:'), ('video', str(source)),
             ('text', 'GENERATED candidate:'), ('video', str(candidate)),
             ('text', 'Prompt that produced the candidate:\n' + prompt_text)]
    if carton_ref:
        parts += [('text', carton_prompt()), ('text', 'CARTON REFERENCE:'), ('image', str(carton_ref))]
    state = call_flash(parts, CARTON_SCHEMA if carton_ref else QA_SCHEMA, run_dir, transport, fps)
    result = dict(state['result'])
    if carton_ref:
        bad = result.get('carton_geometry') in CARTON_BAD
        result['carton_gate'] = 'fail' if bad else 'pass'
    state = {**state, 'result': result}
    record = {'type': 'lymphloop_flash_qa', 'model': MODEL, 'task_kind': task_kind, **({'carton_ref': str(carton_ref)} if carton_ref else {}),
              'source': api.file_description(source), 'candidate': api.file_description(candidate),
              'prompt_sha256': api.sha(prompt_text.encode()), 'request_id': state['request_id'],
              'cache_hit': state.get('cache_hit', False), 'result': state['result'],
              'note': 'Second opinion only; the agent reviews contact sheets and frames before deciding.'}
    api.save(out, record)
    return record


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest='command', required=True)
    a = s.add_parser('screen')
    a.add_argument('--shots', required=True); a.add_argument('--run', required=True); a.add_argument('--ids')
    a.add_argument('--clip-dir'); a.add_argument('--fps', type=int, default=DEFAULT_FPS)
    a.add_argument('--competitor-run', help='RUN holding detect/competitor.json (default: --run)')
    q = s.add_parser('qa')
    q.add_argument('--source', required=True); q.add_argument('--candidate', required=True); q.add_argument('--prompt', required=True)
    q.add_argument('--out', required=True); q.add_argument('--run', required=True)
    q.add_argument('--task-kind', choices=['visual', 'dialogue'], default='visual'); q.add_argument('--fps', type=int, default=DEFAULT_FPS)
    q.add_argument('--carton-ref', help='carton_views image the job sent: adds the carton geometry check')
    s.add_parser('probe-only')
    args = p.parse_args(argv)
    if args.command == 'screen':
        result = screen(args.shots, args.run, args.ids.split(',') if args.ids else None, args.clip_dir, fps=args.fps, competitor_run=args.competitor_run)
        result = {k: v for k, v in result.items() if k != 'shots'} | {'count': len(result['shots'])}
    elif args.command == 'qa':
        result = qa(args.source, args.candidate, args.prompt, args.out, args.run, args.task_kind, fps=args.fps, carton_ref=args.carton_ref)
    else:
        result = {'model': MODEL, 'screen_prompt': screen_prompt(args.run if hasattr(args, 'run') else None), 'screen_schema': SCREEN_SCHEMA,
                  'qa_prompt_visual': qa_prompt('visual'), 'qa_schema': QA_SCHEMA, 'network': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
