#!/usr/bin/env python3
"""Whole-film speech check for the finished master (user, 2026-11-12: as few slips in the spoken lines as possible).

`review` listens to each generated segment; nothing listened to the film as a whole. A line the detection never flagged,
or a source piece spliced back in, can still say the competitor's name, an ingredient our product does not have, or a
word that was rewritten elsewhere in the same film. This transcribes the master once (faster-whisper, offline) and lists:

  left   the competitor's name or one of its ingredients that LymphLoop does not have: must be empty
  check  a word that a rewrite replaced somewhere in this film, heard at another moment: look at each one

  speech_gate.py RUN [MASTER]     prints the report; swap.py final-screen runs it and stores it next to the screen
"""
from __future__ import annotations
import argparse, hashlib, difflib, json, re, sys
from pathlib import Path
import competitor as comp
import google_api as api

OWN = ('lymphloop', 'yesnap')
ORDINARY_WORDS = {'lymph', 'lymphatic', 'lymphatics', 'lymphedema'}
NUMBERS = {'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'twelve', 'twice', 'once', 'half'}
GENERIC_INGREDIENT_WORDS = {'herb', 'herbs', 'herbal', 'ingredient', 'ingredients', 'botanical', 'botanicals', 'plant', 'plants',
                            'extract', 'extracts', 'blend', 'powerful', 'natural', 'organic'}
STOP = {'this', 'that', 'these', 'those', 'your', 'with', 'from', 'into', 'every', 'each', 'little', 'small', 'just', 'take', 'takes',
        'under', 'tongue', 'water', 'coffee', 'morning', 'night', 'daily', 'day'} | NUMBERS


def norm(word): return re.sub(r'[^a-z0-9]', '', str(word).lower())
def stem(word): return word[:-1] if len(word) > 4 and word.endswith('s') else word
def tokens(text): return [norm(t) for t in re.findall(r"[A-Za-z']+", str(text)) if norm(t)]


# our ingredients as other-language ads name them (2026-11-20 film 1753, Spanish: the competitor's "amor de hortelano"
# and "trébol rojo" are our Cleavers and Red Clover; the prompt check refused them as foreign)
OWN_INGREDIENT_ALIASES = {
    'cleavers': ['amor de hortelano', 'galio', 'presera', 'aparine'],
    'burdock root': ['raiz de bardana', 'bardana', 'lampazo'],
    'dandelion': ['diente de leon'],
    'red clover': ['trebol rojo', 'trebol violeta'],
    'echinacea': ['equinacea'],
    'calendula': ['calendula', 'calendulas'],
    'bromelain': ['bromelina'],
}


def _fold(text):
    import unicodedata
    t = unicodedata.normalize('NFKD', str(text or '').lower())
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', ''.join(c for c in t if not unicodedata.combining(c)))).strip()


def own_ingredient(name, facts):
    """True when `name` (any language) is one of our printed ingredients or its alias."""
    f = _fold(name)
    ours = [_fold(n) for n in facts.get('ingredients_printed_front') or []]
    names = ours + [a for n in ours for a in OWN_INGREDIENT_ALIASES.get(n, [])]
    return any(f == n or f.startswith(n + ' ') or n in f.split(' de ') for n in names if n)


def vocabulary(run):
    """What must not be heard (names, ingredients we do not have) and what to look at (words replaced in this film)."""
    profile = comp.active(run); facts = api.read(comp.SKILL / 'assets/product/profile.json').get('facts') or {}
    own_ing = {stem(t) for name in facts.get('ingredients_printed_front') or [] for t in tokens(name)}
    foreign = [tokens(_fold(i)) for i in profile.get('ingredients_named') or [] if not own_ingredient(i, facts)]
    # compare stems on both sides: a profile that says "Cleaver" must still match our "Cleavers" (NV0187, 2026-11-13)
    foreign = [t for t in foreign if t and not any(len(x) >= 4 and stem(x) in own_ing for x in t)]
    # a count of herbs is not a named ingredient (user 2026-10-24: "four herbs" is no conflict); a profile listing
    # "4 herbs" made the prompt check refuse the film's own "Four herbs, one packet" (2026-11-22 film 110 G02)
    foreign = [t for t in foreign if not all(x in GENERIC_INGREDIENT_WORDS or x in NUMBERS for x in t)]
    replaced = set(); names = sorted(comp.names(profile))
    plan = Path(run) / 'plan/segments.json'
    for seg in (api.read(plan).get('segments') if plan.is_file() else []) or []:
        for r in (seg.get('rewrites') or {}).get('dialogue', []) + (seg.get('rewrites') or {}).get('vo', []):
            new = {stem(t) for t in tokens(r.get('rewrite'))}
            if _name_hit(''.join(tokens(r.get('original'))), '', names): continue      # the name itself ("Nu Flows"): the name check covers it
            replaced |= {stem(t) for t in tokens(r.get('original')) if len(t) >= 4 and t not in STOP and stem(t) not in new}
    return {'names': names, 'foreign_ingredients': foreign, 'replaced': sorted(replaced - set(names))}


def _name_hit(word, pair, names):
    """Whisper spells an unknown brand its own way (Nuflos -> NuFloes, NuFloce, New Flows): match by similarity, one
    word or two joined, and never a word that is closer to our own brand."""
    ratio = lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()
    # our brand heard as two words (2026-11-22 film 1106373148453803: "Yes, snap lymphin loop." -- "lymphin" alone is
    # 0.75 to lymphoria, joined with "loop" it is 0.90 to lymphloop): the pair is ours, not a competitor name
    if pair and max(ratio(pair, o) for o in OWN) >= 0.85: return None
    # "lymph" / "lymphatic" is the ads' own vocabulary: "your lymph... It's the system" joined to "lymphits" is 0.71 to
    # lymphoria (2026-11-22 film 1409783017240926). After it only a near-exact pair ("Lymph oria") counts
    plain = word in ORDINARY_WORDS
    for cand, need in (() if plain else ((word, 0.75),)) + ((pair, 0.90 if plain else 0.70),):
        if not cand or len(cand) < 4: continue
        best = max(((ratio(cand, n), n) for n in names), default=(0, ''))
        if best[0] >= need and best[0] > max(ratio(cand, o) for o in OWN): return best[1]
    return None


def hits(words, vocab):
    """words: [{w, a, b}] of the whole film. -> {'left': [...], 'check': [...]}, each {t, word, kind, context}."""
    n = [norm(w['w']) for w in words]; left, check = [], []
    ctx = lambda i, k=1: ' '.join(w['w'] for w in words[max(0, i - 5):i + k + 5])
    skip = -1
    for i, x in enumerate(n):
        if i <= skip or not x: continue
        name = _name_hit(x, x + n[i + 1] if i + 1 < len(n) else '', vocab['names'])
        if name:
            left.append({'t': words[i]['a'], 'word': words[i]['w'], 'kind': 'competitor_name', 'matches': name, 'context': ctx(i)}); skip = i + 1; continue
        for phrase in vocab['foreign_ingredients']:
            if [stem(t) for t in n[i:i + len(phrase)]] == [stem(t) for t in phrase]:
                left.append({'t': words[i]['a'], 'word': ' '.join(phrase), 'kind': 'ingredient_not_in_lymphloop', 'context': ctx(i, len(phrase))})
                skip = i + len(phrase) - 1; break
        else:
            if stem(x) in vocab['replaced']:
                check.append({'t': words[i]['a'], 'word': words[i]['w'], 'kind': 'replaced_elsewhere_in_this_film', 'context': ctx(i)})
    return {'left': left, 'check': check}


def gate(run, master, cache_dir, windows=None):
    """Transcribe the master once per file (cached by the caller's directory) and check it. With `windows`
    ([(start_s, end_s)]), only those stretches are heard (final-screen: the parts that were changed)."""
    import words as words_mod
    # keyed by the file heard and the windows: the manual CLI shares one folder, and two candidates checked one after the
    # other got the first one's words back (2026-11-22 film 1801069464466244 G04 r2 / r3: "words 0" twice)
    st = Path(master).stat()
    ident = hashlib.sha256(json.dumps([str(Path(master).resolve()), st.st_size, st.st_mtime_ns, [list(w) for w in windows or []]]).encode()).hexdigest()[:12]
    cache = Path(cache_dir) / f'words-{ident}.json'
    if cache.is_file(): heard = json.loads(cache.read_text())
    else:
        model = words_mod.load_model()
        if windows:
            heard = []
            for a, b in windows:
                heard += [{**w, 'a': w['a'] + a, 'b': w['b'] + a} for w in words_mod.transcribe(model, master, a, b - a)]
        else:
            heard = words_mod.transcribe(model, master)
        cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text(json.dumps(heard))
    vocab = vocabulary(run)
    out = {'type': 'lymphloop_speech_gate', 'master': str(master), 'words': len(heard), 'vocabulary': vocab, **hits(heard, vocab)}
    (Path(cache_dir) / 'report.json').write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('run'); p.add_argument('master', nargs='?')
    a = p.parse_args(argv); run = Path(a.run)
    master = Path(a.master) if a.master else Path(json.loads((run / 'state.json').read_text())['output_dir']) / 'master.mp4'
    out = gate(run, master, run / 'final' / 'speech-manual')
    print(json.dumps({k: out[k] for k in ('master', 'words', 'vocabulary', 'left', 'check')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
