"""Captions back on the generated segments (user, 2026-11-13: the film's own caption style and place, the words actually
spoken in the generated segment)."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import caption_burn as cb  # noqa: E402
import swap  # noqa: E402

STYLE = {'max_line_width': 0.69, 'max_lines': 2, 'lowercase_sentence_start': True, 'commas': True, 'hyphens': False,
         'trailing_period': False, 'uppercase': False, 'proper_nouns': ['Danny', 'Nordic']}


def say(text, step=0.4, gaps=None):
    out, t = [], 0.0
    for i, w in enumerate(text.split()):
        t += (gaps or {}).get(i, 0.0)
        out.append({'w': w, 'a': round(t, 2), 'b': round(t + 0.3, 2)}); t += step
    return out


class WhatTheCaptionSays(unittest.TestCase):
    def shown(self, heard, lines):
        words, unsaid = cb.spell(say(heard), lines)
        return ' '.join(w['w'] for w in words), unsaid

    def test_the_prompt_gives_spelling_and_the_clip_gives_the_words(self):
        self.assertEqual(self.shown("its called Limp Flute", ["It's called LymphLoop."]), ("It's called LymphLoop.", []))

    def test_a_word_that_was_not_said_is_not_shown(self):
        # NV0024 G04 and G02: the muted new word stayed silent; the caption follows the sound
        self.assertEqual(self.shown('Once in water morning and night', ['One packet in water, morning and night.']),
                         ('One in water, morning and night.', ['packet']))
        text, unsaid = self.shown('Cleavers sweeps the waste out. Restarts the circulation.', ['Cleavers sweeps the waste out.', 'Dandelion restarts the circulation.'])
        self.assertEqual((text, unsaid), ('Cleavers sweeps the waste out. restarts the circulation.', ['Dandelion']))

    def test_a_line_that_was_cut_away_leaves_no_caption(self):
        self.assertEqual(self.shown('How long', ['One packet in water.', 'How long?', 'Six weeks.'])[0], 'How long?')

    def test_without_prompt_lines_the_heard_words_are_used(self):
        self.assertEqual(self.shown('thank you so much', [])[0], 'thank you so much')


class HowTheFilmWritesIt(unittest.TestCase):
    def test_words_are_dressed_like_the_films_captions(self):
        d = lambda w, start=False: cb.dress(w, STYLE, start)
        self.assertEqual([d('So', True), d('Danny,', True), d("I've"), d('LymphLoop.'), d('30-day'), d('do?')],
                         ['so', 'Danny,', "I've", 'LymphLoop', '30 day', 'do?'])
        self.assertEqual(cb.dress('Hello.', {**STYLE, 'uppercase': True}, True), 'HELLO')
        self.assertEqual(cb.dress('So', {**STYLE, 'lowercase_sentence_start': False}, True), 'So')

    def test_names_are_the_capitals_never_written_small(self):
        self.assertEqual(cb.names_in(['Danny, I cut the beer', 'What do you mean', 'what did Danny do', 'women in Norway']), ['Danny', 'Norway'])

    def test_a_sentence_is_one_caption_and_wraps_at_the_films_width(self):
        width = lambda text: len(text) * 0.03                                    # 23 letters fill the line
        words, _ = cb.spell(say("Danny I've bought every pill at the drugstore those burn", gaps={8: 0.5}),
                            ["Danny, I've bought every pill at the drugstore.", 'Those burn.'])
        caps = cb.group(words, STYLE, width)
        self.assertEqual(caps[0]['lines'][0], "Danny, I've bought")
        self.assertTrue(all(len(c['lines']) <= 2 and all(width(l) <= 0.69 for l in c['lines']) for c in caps))
        self.assertEqual(caps[-1]['lines'], ['those burn'])
        self.assertTrue(all(a['b'] <= b['a'] for a, b in zip(caps, caps[1:])))    # one caption at a time

    def test_a_long_pause_starts_a_new_caption(self):
        words, _ = cb.spell(say('cleavers sweeps the waste out', gaps={1: 1.0}), ['Cleavers sweeps the waste out.'])
        self.assertEqual([c['lines'] for c in cb.group(words, STYLE, lambda t: len(t) * 0.02)], [['cleavers'], ['sweeps the waste out']])

    def test_word_times_are_pulled_in_to_the_sound(self):
        import numpy as np
        rate = 16000; audio = (np.random.default_rng(1).standard_normal(rate * 4) * 0.002).astype(np.float32)
        audio[int(1.6 * rate):int(2.5 * rate)] += 0.3 * np.sin(np.arange(int(0.9 * rate)) * 0.3).astype(np.float32)
        tight = cb.tighten([{'w': 'do?', 'a': 1.18, 'b': 3.18}], audio, rate)[0]       # whisper: 2.0 s for a 0.9 s word
        self.assertAlmostEqual(tight['a'], 1.6, delta=0.06); self.assertAlmostEqual(tight['b'], 2.5, delta=0.06)

    def test_prompt_lines_are_read_from_the_prompt(self):
        text = '镜头1（0–3 秒）：男士举着条包。灰发男士台词：“It\'s called LymphLoop.” 镜头2：画外旁白台词（平静）：“Cleavers sweeps the waste out.”'
        self.assertEqual(cb.prompt_lines(text), ["It's called LymphLoop.", 'Cleavers sweeps the waste out.'])


@unittest.skipUnless(cb.font_candidates(), 'no candidate font on this machine')
class Drawing(unittest.TestCase):
    def test_the_caption_is_drawn_at_the_measured_place_and_nowhere_else(self):
        import numpy as np
        name, file, index = cb.font_candidates()[0]
        style = {'font_file': file, 'font_index': index, 'size': 0.035, 'line_pitch': 0.035, 'centre_y': 0.70, 'centre_x': 0.5,
                 'fill': [255, 255, 255], 'stroke': [0, 0, 0], 'stroke_width': 0.003, 'stroke_blur': 0.35}
        frame = np.full((854, 480, 3), 120, np.uint8)
        out = cb.draw(frame, ['so what did', 'you do?'], style)
        changed = np.where(np.abs(out.astype(int) - 120).max(axis=2) > 40)
        self.assertTrue(0.64 * 854 < changed[0].min() and changed[0].max() < 0.76 * 854)        # two lines around 70 % of the height
        self.assertAlmostEqual((changed[1].min() + changed[1].max()) / 2 / 480, 0.5, delta=0.03)   # centred
        self.assertTrue((out[changed] > 230).all(axis=1).any() and (out[changed] < 40).all(axis=1).any())   # white fill, dark outline


class CaptionsStep(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); run = Path(self.td.name) / 'RUN'; self.run = run
        for d in ('plan', 'prompts', 'captions', 'generation/results', 'qa'): (run / d).mkdir(parents=True)
        (run / 'state.json').write_text('{}')
        (run / 'captions/style.json').write_text(json.dumps({'font': 'X', 'font_fit': 0.9, 'size': 0.03, 'centre_y': 0.7, 'max_line_width': 0.7,
                                                             'fill': [255] * 3, 'stroke': [0] * 3, 'centre_y_by_segment': {'G01': 0.68}}))
        segs = [{'id': g, 'in_frame': i * 60, 'out_frame': i * 60 + 60, 'speech': [{'text': 'It is called LymphLoop.'}]} for i, g in enumerate(('G01', 'G02', 'G03'))]
        (run / 'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': segs}))
        clip = lambda n: {'path': str(run / 'generation/results' / n), 'sha256': 'x'}
        for n in ('G01-a1-c1.mp4', 'G02-a2-c1.mp4'): (run / 'generation/results' / n).write_bytes(b'x')
        (run / 'plan/selections.json').write_text(json.dumps({
            'G01': {'job': 'G01-a1', 'media': clip('G01-a1-c1.mp4'), 'clip_in': 0.0, 'clip_out': 2.0},
            'G02': {'job': 'G02-a2', 'media': clip('G02-a2-c1.mp4'), 'clip_in': 0.0, 'clip_out': 2.0},     # a source piece spliced back in
            'G03': {'job': 'G03-a1', 'candidate': None, 'picture': 'source', 'generated': False}}))
        (run / 'prompts/G01-a1.final.txt').write_text('镜头1：灰发男士台词：“It\'s called LymphLoop.”')

    def tearDown(self): self.td.cleanup()

    def run_step(self, real_sha=False, **extra):
        burned = []
        real_ref = swap.ref
        def burn(clip, out, caps, style, centre_y=None):
            Path(out).write_bytes(b'y'); burned.append((Path(clip).name, centre_y)); return {'frames': 48, 'frames_captioned': 20}
        caps = [{'a': 0.2, 'b': 1.4, 'lines': ["it's called LymphLoop"]}]
        with mock.patch.object(cb, 'captions_for', return_value=(caps, [{'w': 'x', 'a': 0, 'b': 1}], [])), mock.patch.object(cb, 'burn', side_effect=burn), \
                mock.patch('caption_clean.caption_frames', side_effect=lambda clip, *a, **k: (40, 48, []) if 'G02' in str(clip) else (0, 48, [])), \
                mock.patch.object(swap, 'script'), mock.patch.object(swap, 'ref', side_effect=(real_ref if real_sha else lambda p: {'path': str(p), 'sha256': 'x'})), \
                mock.patch.object(swap, 'commit'), mock.patch('builtins.print'):
            swap.cmd_captions(argparse.Namespace(run=str(self.run), segment=None, remeasure=False, font=None, **extra))
        return burned, json.loads((self.run / 'plan/selections.json').read_text())

    def test_generated_picks_get_captions_and_the_rest_is_left_alone(self):
        burned, sel = self.run_step()
        self.assertEqual(burned, [('G01-a1-c1.mp4', 0.68)])                                   # this segment's own caption height
        self.assertTrue(sel['G01']['media']['path'].endswith('G01-a1-c1.captioned.mp4'))
        self.assertTrue(sel['G01']['media_uncaptioned']['path'].endswith('G01-a1-c1.mp4'))
        self.assertEqual(sel['G01']['captions']['events'][0]['lines'], ["it's called LymphLoop"])
        self.assertNotIn('captions', sel['G02']); self.assertTrue(sel['G02']['media']['path'].endswith('G02-a2-c1.mp4'))   # already captioned
        self.assertNotIn('captions', sel['G03'])                                               # source picture kept

    def test_running_it_again_starts_from_the_uncaptioned_clip(self):
        self.run_step(); burned, sel = self.run_step()
        self.assertEqual(burned, [('G01-a1-c1.mp4', 0.68)])
        self.assertTrue(sel['G01']['media']['path'].endswith('G01-a1-c1.captioned.mp4'))


    def test_only_segments_whose_inputs_changed_are_captioned_again(self):
        # user 2026-11-22: a round of goof redos changes a few segments; the rest keep their captioned clips
        first, _ = self.run_step(real_sha=True)
        again, sel = self.run_step(real_sha=True)
        self.assertEqual((len(first), again), (1, []))
        self.assertTrue(sel['G01']['media']['path'].endswith('G01-a1-c1.captioned.mp4'))
        forced, _ = self.run_step(real_sha=True, force=True)
        self.assertEqual(len(forced), 1)
        (self.run / 'generation/results/G01-a1-c1.mp4').write_bytes(b'a new splice of this segment')
        changed, _ = self.run_step(real_sha=True)
        self.assertEqual(len(changed), 1)
        style = self.run / 'captions/style.json'; d = json.loads(style.read_text()); d['size'] = 0.04; style.write_text(json.dumps(d))
        restyled, _ = self.run_step(real_sha=True)
        self.assertEqual(len(restyled), 1)


    def test_a_copy_that_misses_most_words_is_drawn_instead(self):
        # 2026-11-22 film 1912538379721657 G01: the copy brought back one of four lines
        import caption_matte
        poor = {'method': 'copied from the film', 'events': [{'t': 0.1, 'text': 'so', 'how': 'copied'}], 'drawn': [], 'frames_copied': 3}
        good = {'method': 'copied from the film', 'events': [{'t': 0.1, 'text': "it's called LymphLoop", 'how': 'copied'}], 'drawn': [], 'frames_copied': 30}
        def fake_apply(run, seg, s, base, out, *a, **k):
            Path(out).write_bytes(b'z'); return dict(self.answer)
        for answer, drawn in ((poor, 1), (good, 0)):
            self.answer = answer
            with mock.patch.object(caption_matte, 'apply', side_effect=fake_apply):
                burned, sel = self.run_step(force=True)
            self.assertEqual(len(burned), drawn, answer['events'])


    def test_a_splice_on_a_splice_that_moved_pictures_is_not_copied(self):
        # 2026-11-22 film 2896957363997251 G05: a3 refitted one shot into a2 in place, but a2 had put a redone shot back
        # at its own length; the film's captions were copied ~2 s early
        import caption_matte
        jobs = self.run / 'generation/jobs'; jobs.mkdir(parents=True, exist_ok=True)
        res = lambda n: str(self.run / 'generation/results' / n)
        (jobs / 'G01-a2.json').write_text(json.dumps({'provider': 'local_splice', 'recipe': {'pictures': [
            {'source': 'G01-a1-c1', 'path': res('G01-a1-c1.mp4'), 'frames_inclusive': [0, 5]},
            {'source': 'G01-shot_r1-c1', 'path': res('G01-shot_r1-c1.mp4'), 'frames_inclusive': [0, 95]},
            {'source': 'G01-a1-c1', 'path': res('G01-a1-c1.mp4'), 'frames_inclusive': [82, 144]}]}}))
        (jobs / 'G01-a3.json').write_text(json.dumps({'provider': 'local_splice', 'recipe': {'pictures': [
            {'source': 'G01-hand_r1-c1', 'path': res('G01-hand_r1-c1.mp4'), 'frames_inclusive': [0, 95], 'fit': 49},
            {'source': 'G01-a2-c1', 'path': res('G01-a2-c1.mp4'), 'frames_inclusive': [49, 207]}]}}))
        (self.run / 'generation/results/G01-a3-c1.mp4').write_bytes(b'x')
        sel = json.loads((self.run / 'plan/selections.json').read_text())
        sel['G01'].update(job='G01-a3', media={'path': res('G01-a3-c1.mp4'), 'sha256': 'x'}); (self.run / 'plan/selections.json').write_text(json.dumps(sel))
        seen = []
        def fake_apply(run, seg, s, base, out, *a, **k):
            seen.append(k.get('frames_match')); return None
        with mock.patch.object(caption_matte, 'apply', side_effect=fake_apply):
            burned, _ = self.run_step(force=True)
        self.assertEqual(seen[0], False); self.assertEqual(len(burned), 1)           # drawn at the heard times instead
        # the same refit on a base that kept its timing is still copied
        (jobs / 'G01-a2.json').write_text(json.dumps({'provider': 'local_splice', 'recipe': {'pictures': [
            {'source': 'G01-a1-c1', 'path': res('G01-a1-c1.mp4'), 'frames_inclusive': [0, 5]},
            {'source': 'G01-shot_r1-c1', 'path': res('G01-shot_r1-c1.mp4'), 'frames_inclusive': [0, 95], 'fit': 76},
            {'source': 'G01-a1-c1', 'path': res('G01-a1-c1.mp4'), 'frames_inclusive': [82, 144]}]}}))
        seen.clear()
        with mock.patch.object(caption_matte, 'apply', side_effect=fake_apply):
            self.run_step(force=True)
        self.assertEqual(seen[0], True)


class PercentSign(unittest.TestCase):
    def test_split_percent_is_kept(self):
        heard = [{'w': '70', 'a': 0.0, 'b': 0.3}, {'w': '%', 'a': 0.3, 'b': 0.5}, {'w': 'off', 'a': 0.5, 'b': 0.7}]
        for lines in (['70% off'], ['70 off'], []):
            words, _ = cb.spell(heard, lines)
            self.assertEqual(words[0]['w'], '70%', lines)


class BrokenFace(unittest.TestCase):
    def test_a_face_that_cannot_draw_the_text_is_skipped(self):
        # 2026-11-22: PIL getbbox raised "division by zero" for one installed face and the whole measurement stopped
        with mock.patch.object(cb, '_fit_face', side_effect=OSError('division by zero')):
            self.assertIsNone(cb._fit({'ink': None, 'text': 'x', 'height': 20}, 'Bad', '/x.ttf', 0))

class HighlightFilm(unittest.TestCase):
    def test_white_captions_with_a_yellow_word_keep_both_colours(self):
        # 2026-11-22 film 1659373095741717: 5 of 14 samples white + yellow, the rest read as one yellow fill
        import caption_burn as cb
        W, Y = [255, 255, 255], [16, 255, 245]                       # BGR
        lit = [{'color': W, 'highlight': Y}] * 5
        rest = [{'color': [80, 255, 255], 'highlight': None}] * 7 + [{'color': [133, 194, 232], 'highlight': None}] * 2
        self.assertEqual(cb.caption_colours(lit + rest), ([255, 255, 255], [245, 255, 16]))
        # plain white captions, and plain yellow ones, keep the old reading
        self.assertEqual(cb.caption_colours([{'color': W, 'highlight': None}] * 6), ([255, 255, 255], None))
        self.assertEqual(cb.caption_colours([{'color': [80, 255, 255], 'highlight': None}] * 6 + [{'color': W, 'highlight': Y}]), ([255, 255, 80], None))


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(cb.font_candidates(), 'no candidate font on this machine')
class WordBackplates(unittest.TestCase):
    def style(self):
        name, file, index = next((f for f in cb.font_candidates() if f[0]=='Arial Bold'), cb.font_candidates()[0])
        return {'font_file':file,'font_index':index,'size':0.05,'line_pitch':0.06,'centre_y':0.7,'centre_x':0.5,
                'fill':[255,255,255],'stroke':[0,0,0],'stroke_width':0.002,
                'highlight_background':{'color':[247,228,110],'pad_x':0.007,'pad_y':0.006,'radius':0.01}}

    def test_box_moves_with_actual_word_times_and_preserves_other_pixels(self):
        import numpy as np
        frame=np.full((854,480,3),70,np.uint8);style=self.style()
        cap={'lines':['SIP ONE PACKET'], 'words':[{'text':w,'a':i*.5,'b':i*.5+.4}for i,w in enumerate(['SIP','ONE','PACKET'])]}
        centres=[]
        for t in (0.1,0.7,1.2):
            out=cb.draw(frame,cap['lines'],style,highlight_words=cb.active_highlight(cap,t))
            yellow=(out[:,:,2]>220)&(out[:,:,1]>190)&(out[:,:,0]<160)
            ys,xs=np.where(yellow);self.assertGreater(len(xs),100);centres.append(float(xs.mean()))
            self.assertTrue(np.array_equal(out[:480],frame[:480]))
        self.assertLess(centres[0],centres[1]);self.assertLess(centres[1],centres[2])
        plain={k:v for k,v in style.items()if k!='highlight_background'}
        self.assertTrue(np.array_equal(cb.draw(frame,cap['lines'],plain),cb.draw(frame,cap['lines'],plain,highlight_words=(1,2))))

    def test_white_glyphs_inside_yellow_box_are_not_yellow_letter_fills(self):
        import numpy as np
        frame=np.full((854,480,3),70,np.uint8);out=cb.draw(frame,['SIP ONE PACKET'],self.style(),highlight_words=(1,2))
        crop=out[550:645];fill,hi=cb._fill_colours(crop);self.assertIsNotNone(cb._backplate(crop,fill,hi))
        plain=self.style();plain.pop('highlight_background');plain['fill']=[247,228,110]
        yellow_letters=cb.draw(frame,['SIP ONE PACKET'],plain)[550:645]
        self.assertIsNone(cb._backplate(yellow_letters,[255,255,255],[110,228,247]))

    def test_group_keeps_word_windows_for_rendering(self):
        words,_=cb.spell(say('sip one packet'),['sip one packet'])
        caps=cb.group(words,STYLE,lambda text:len(text)*.01)
        self.assertEqual([w['text']for w in caps[0]['words']],['sip','one','packet'])
        self.assertEqual(cb.active_highlight(caps[0],.45),(1,2))


class ReviewedCaptionTiming(unittest.TestCase):
    def test_reviewed_word_boundaries_are_bound_to_exact_media_bytes(self):
        import words,hashlib
        with tempfile.TemporaryDirectory()as td:
            r=Path(td);(r/'captions').mkdir();clip=r/'clip.mp4';clip.write_bytes(b'reviewed native media')
            digest=hashlib.sha256(clip.read_bytes()).hexdigest();timed=[{'w':"I'm",'a':2.98,'b':3.1},{'w':'driving.','a':3.1,'b':3.42}]
            (r/'captions/timing_overrides.json').write_text(json.dumps({'clips':{digest:{'reviewed':True,'words':timed,'evidence':'native waveform/source word comparison'}}}))
            info={'video':{'width':480,'height':854},'video_duration':4}
            with mock.patch.object(words,'RUN',r),mock.patch.object(cb.media,'probe',return_value=info),mock.patch.object(cb,'width_fn',return_value=lambda text:len(text)*.01),mock.patch.object(words,'transcribe')as transcribe:
                caps,heard,_=cb.captions_for(clip,["I'm driving."],STYLE)
                self.assertEqual(heard,timed);self.assertGreater(caps[0]['a'],2.9);transcribe.assert_not_called()
                clip.write_bytes(b'different native media')
                with mock.patch.object(words,'load_model'),mock.patch.object(words,'pcm',return_value=[]),mock.patch.object(cb,'tighten',return_value=[]):
                    transcribe.return_value=[];cb.captions_for(clip,["I'm driving."],STYLE);transcribe.assert_called_once()
