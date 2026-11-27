#!/usr/bin/env python3
"""Per-run competitor profile (user, 2026-11-11: swap ANY competitor supplement, not only the Lymphoria dropper bottle).

Flash watches the whole working master once and describes the product the ad sells: brand names, physical form,
container and dose-unit look, how people take it, the partial views that are still the product, where it is clearly
visible, and the floating watermark text. The agent checks it against the shot sheets, fixes the JSON by hand when
needed and accepts it (`swap.py competitor --accept`). From then on screening, QA, the final screen and the watermark
scan all read RUN/detect/competitor.json; a RUN without one falls back to the built-in Lymphoria profile.

Forms in scope (user, 2026-11-11: "滴剂/硬胶囊/软胶囊/软糖/粉末，管好这些形态，基本就都能管住了"): drops, hard capsules,
softgels, gummies and powders; plain tablets are handled like hard capsules, and liquids taken by the spoonful or from a
small bottle like drops (Nuflos run, 2026-11-11). Topical products and devices are out of scope.
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse
import json
import re
from pathlib import Path
import google_api as api
import media_pipeline as media

SKILL = Path(__file__).resolve().parents[1]
FORMS = ['drops', 'hard_capsule', 'softgel', 'tablet', 'gummy', 'powder', 'liquid_shot', 'other']
IN_SCOPE = {'drops', 'hard_capsule', 'softgel', 'tablet', 'gummy', 'powder', 'liquid_shot'}   # liquid_shot: Nuflos run 2026-11-11
OWN_BRAND = ('LYMPHLOOP', 'YESNAP')
MAX_ANCHORS = 8
FILE = 'detect/competitor.json'

SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'brand_names': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'product_name': {'type': 'STRING'},
        'form': {'type': 'STRING', 'enum': FORMS},
        'secondary_forms': {'type': 'ARRAY', 'items': {'type': 'STRING', 'enum': FORMS}},
        'container_look': {'type': 'STRING'},
        'dose_unit_look': {'type': 'STRING'},
        'ingestion_actions': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'hard_cases': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'ingredients_named': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'watermark_text': {'type': 'STRING'},
        'anchors': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
            'time': {'type': 'STRING'}, 'what': {'type': 'STRING'}}, 'required': ['time', 'what']}},
        'scope_note': {'type': 'STRING'},
    },
    'required': ['brand_names', 'product_name', 'form', 'secondary_forms', 'container_look', 'dose_unit_look',
                 'ingestion_actions', 'hard_cases', 'ingredients_named', 'watermark_text', 'anchors', 'scope_note'],
}

PROMPT = (
    'This is a US direct-response video ad for a dietary supplement. A later step will replace the ADVERTISED product '
    'everywhere in it, so describe that product precisely enough that a person who never saw the ad could spot it in any '
    'frame, including partial, blurred and background views.\n'
    '- brand_names: every brand / product name as printed on the pack or spoken (exact spelling, e.g. "Lymphoria").\n'
    '- product_name: what it is, e.g. "lymphatic drainage herbal drops, 2 fl oz".\n'
    '- form: the dose form people take. drops = liquid from a dropper bottle; hard_capsule = two-piece capsules; softgel = '
    'glossy gel capsules; tablet = pressed pills; gummy = chewable gummies; powder = powder scooped or poured into a '
    'drink; liquid_shot = a liquid poured onto a spoon or into a cap, or drunk from a small bottle/vial; other = creams, patches, devices, sprays, anything not taken '
    'by mouth. secondary_forms: other forms of the SAME product shown (e.g. powder also sold in sachets).\n'
    '- container_look: the container(s) as they look on screen: material, colour, shape, cap, label colours and layout, '
    'size relative to a hand (bottle, jar, tub, pouch, blister, sachet, box).\n'
    '- dose_unit_look: the dose itself: drop/liquid colour, capsule colour and size, gummy shape and colour, powder colour, '
    'scoop.\n'
    '- ingestion_actions: every way the ad shows it being taken or prepared (e.g. "drops under the tongue from the '
    'pipette", "tips two capsules into the palm and swallows them with water", "scoops powder into a shaker").\n'
    '- hard_cases: partial views that are still the product and are easy to miss: a cap or neck sticking out of a fist, '
    'capsules in an open palm without the bottle, a gummy between fingertips, a scoop of powder, the pack in a mirror, on '
    'a shelf, out of focus in the background, half out of frame.\n'
    '- ingredients_named: ingredient names the ad shows or says (each plant or compound by name; not counts like "4 herbs").\n'
    '- watermark_text: the text of a floating watermark or handle that drifts over the picture (e.g. "@LYMPHORIA"), '
    'or "" when there is none. Not burned-in captions.\n'
    '- anchors: up to 8 moments where the product is seen clearly, covering each look: the packshot, in a hand, the '
    'dose unit, the ingestion. time is the video timestamp written MM:SS (e.g. "02:59"), never a decimal number.\n'
    '- scope_note: one sentence on anything unusual (several products, a product that is not taken by mouth), and '
    'always say what the ad promises (lymphatic drainage, blood sugar, joints, sleep ...): the target product is a '
    'lymphatic drainage drink, so an ad about anything else needs its ingredients and claims rewritten.\n'
    'Answer strictly in the JSON schema.')


def path(run): return Path(run) / FILE


def fallback():
    """The built-in profile (Lymphoria drops) shaped like a run profile: RUNs made before 2026-11-11 and tests."""
    known = (api.read(SKILL / 'assets/product/profile.json').get('competitors') or {}).get('known') or []
    c = known[0] if known else {}
    return {'type': 'lymphloop_competitor_profile', 'source': 'builtin', 'reviewed': True,
            'brand_names': [c.get('brand', 'Lymphoria')], 'product_name': c.get('product', ''), 'form': 'drops',
            'secondary_forms': [], 'container_look': c.get('look', 'brown glass dropper bottles with amber liquid'),
            'dose_unit_look': 'pale amber liquid in a glass pipette', 'ingestion_actions': ['drops into the mouth from the pipette'],
            'hard_cases': c.get('hard_cases', []), 'ingredients_named': [], 'watermark_text': 'LYMPHORIA',
            'anchor_images': [str(SKILL / f) for f in (api.read(SKILL / 'assets/product/profile.json').get('competitors') or {}).get('screen_anchor_images', [])
                              if (SKILL / f).is_file()]}


def load(run):
    """The accepted run profile, or None."""
    p = path(run) if run else None
    if not p or not p.is_file(): return None
    data = api.read(p)
    return data if data.get('reviewed') else None


def active(run):
    """What detection uses: the accepted run profile, else the built-in Lymphoria profile."""
    return (load(run) if run else None) or fallback()


def names(profile):
    """Lower-case, space-free competitor names for matching speech and printed text."""
    out = {re.sub(r'[^a-z0-9]', '', n.lower()) for n in profile.get('brand_names') or []}
    return {n for n in out if len(n) >= 3 and n not in ('yesnap', 'lymphloop')}


def watermark_target(profile):
    """The floating-watermark word to scan for: the watermark text's longest word, else the first brand name."""
    own = lambda w: w.upper() in OWN_BRAND      # a sister product of our own brand (yesnap creatine): never scan for YESNAP
    words = [w for w in re.findall(r'[A-Za-z]{4,}', profile.get('watermark_text') or '') if not own(w)] \
        or [w for w in re.findall(r'[A-Za-z]{4,}', ' '.join(profile.get('brand_names') or [])) if not own(w)]
    return max(words, key=len).upper() if words else ''


def fuzzy_pattern(target):
    """OCR reads faint watermarks wrong (LYMPHORTA, LYMRHORIA, MPHORIA): match any 4-letter piece of the target that
    is not also a piece of our own brand, on the upper-cased letters of an OCR line. LYMPHORIA gives the 2026-10-25
    pattern (MPHO PHOR HORI ORIA); RHOR covers its common misread."""
    t = re.sub(r'[^A-Z]', '', (target or '').upper())
    if not t: return r'(?!x)x'
    if t == 'LYMPHORIA': return r'(PHOR|HORI|RHOR|MPHO(?!O))'
    own = ' '.join(OWN_BRAND)
    grams = [t[i:i + 4] for i in range(max(1, len(t) - 3))] if len(t) >= 4 else [t]
    grams = [g for g in dict.fromkeys(grams) if g not in own]
    return '(' + '|'.join(grams or [t]) + ')'


def facts_text(profile):
    """One paragraph for Flash prompts."""
    parts = [f"{', '.join(profile.get('brand_names') or ['unknown brand'])} ({profile.get('product_name') or profile.get('form')})",
             f"form: {profile.get('form')}" + (f" (also {', '.join(profile['secondary_forms'])})" if profile.get('secondary_forms') else ''),
             f"container: {profile.get('container_look')}", f"dose: {profile.get('dose_unit_look')}"]
    if profile.get('ingestion_actions'): parts.append('taken by: ' + ' | '.join(profile['ingestion_actions']))
    if profile.get('hard_cases'): parts.append('HARD CASES that are still conflicts: ' + ' | '.join(profile['hard_cases']))
    return '; '.join(p for p in parts if p)


def anchor_seconds(value):
    """'02:59' / '1:02:59' / '179' / 179.0 -> seconds. Flash was asked for seconds as a number once and wrote 2:59 as
    2.99 (2026-11-11, Metabolae), so anchors are MM:SS strings now."""
    if isinstance(value, (int, float)): return float(value)
    parts = [x for x in re.split(r'[:：]', str(value).strip()) if x != '']
    try: nums = [float(x) for x in parts]
    except ValueError: return None
    if not nums or len(nums) > 3: return None
    return sum(n * 60 ** i for i, n in enumerate(reversed(nums)))


def anchor_images(profile):
    return [p for p in profile.get('anchor_images') or [] if Path(p).is_file()]


def extract_anchors(video, anchors, out_dir):
    """Full frames at the anchor moments, for Flash screening only (never sent to generation)."""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    dur = float(media.probe(str(video))['video_duration'])
    paths = []
    for i, a in enumerate(sorted(anchors, key=lambda x: x['t'])[:MAX_ANCHORS]):
        t = min(max(0.0, float(a['t'])), max(0.0, dur - 0.05))
        out = out_dir / f'anchor-{i + 1:02d}-{t:07.2f}s.jpg'
        if not out.is_file():
            media.ff(['-y', '-ss', f'{t:.3f}', '-i', str(video), '-frames:v', '1', '-q:v', '2', str(out)])
        paths.append(str(out))
    return paths


def identify(run, video, transport=None, fps=2):
    """Build the competitor profile draft. In Antigravity native mode, exports overview_grid,
    local Whisper transcript and voice samples for the agent to inspect via read_file (zero Flash)."""
    import agent_native
    if agent_native.is_native() and transport is None:
        run_p = Path(run).resolve()
        overview = agent_native.build_overview_grid(video, run_p / 'agent_review' / 'overview_grid.jpg')
        sents, tr_path = agent_native.cached_full_transcript(run_p, video)
        voice_samples = agent_native.export_voice_samples(run_p, video, sents)
        profile = {
            'type': 'lymphloop_competitor_profile',
            'source': 'antigravity_native',
            'video': api.file_description(str(video)),
            'brand_names': [], 'product_name': '', 'form': 'drops', 'secondary_forms': [],
            'container_look': '', 'dose_unit_look': '', 'ingestion_actions': [], 'hard_cases': [],
            'watermark_text': '', 'in_scope': True, 'scope_note': '',
            'anchor_images': [str(overview)] if overview else [],
            'voice_samples': voice_samples,
            'transcript': str(tr_path),
            'reviewed': False,
        }
        api.save(path(run_p), profile)
        return profile
    import flash_screen
    state = flash_screen.call_flash([('text', PROMPT), ('video', str(video))], SCHEMA, run, transport, fps)
    res = dict(state['result'])
    res['anchors'] = [{**a, 't': anchor_seconds(a.get('time', a.get('t')))} for a in res.get('anchors') or []]
    res['anchors'] = [a for a in res['anchors'] if a['t'] is not None]
    profile = {'type': 'lymphloop_competitor_profile', 'source': 'flash', 'video': api.file_description(str(video)),
               'request_id': state['request_id'], **res,
               'in_scope': res.get('form') in IN_SCOPE,
               'anchor_images': extract_anchors(video, res['anchors'], path(run).parent / 'competitor'),
               'reviewed': False}
    api.save(path(run), profile)
    return profile


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True); p.add_argument('--video', required=True)
    a = p.parse_args(argv)
    print(json.dumps(identify(a.run, a.video), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
