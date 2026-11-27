"""User rule 2026-10-24: no TTS. Reworded lines are re-voiced by Wan Controlled Edit from the original."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import media_pipeline as media  # noqa: E402
import swap  # noqa: E402


def shot(decision, dialogue=(), vo=()):
    return {'decision': decision, 'rewrites': {'dialogue': list(dialogue), 'vo': list(vo)}}


class NeedsWan(unittest.TestCase):
    def test_product_shot_is_generated(self):
        self.assertTrue(swap.needs_edit(shot('replace')))

    def test_product_free_shot_with_a_reworded_line_is_generated(self):
        self.assertTrue(swap.needs_edit(shot('keep', vo=[{'original': 'Lymphoria drops', 'rewrite': 'yesnap LymphLoop'}])))
        self.assertTrue(swap.needs_edit(shot('keep', dialogue=[{'original': "It's four herbs", 'rewrite': "It's twelve herbs"}])))

    def test_plain_shot_is_left_alone(self):
        self.assertFalse(swap.needs_edit(shot('keep')))


class Mute(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(swap.parse_mutes(['G02:1.50-2.28', 'G06:9.8-11.1,3-3.4']),
                         {'G02': [(1.5, 2.28)], 'G06': [(9.8, 11.1), (3.0, 3.4)]})
        self.assertEqual(swap.parse_mutes(None), {})

    def test_muted_window_is_silent_and_picture_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            clip = Path(d)/'G02.hq.clean.mp4'
            media.ff(['-y', '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=30:duration=3', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                      '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(clip)])
            out = swap.muted_reference(clip, [(1.0, 2.0)])
            self.assertEqual(out.name, 'G02.hq.clean.muted-1.00_2.00.mp4')
            import media_pipeline
            ffmpeg = media_pipeline.binary('ffmpeg')

            def level(a, b):
                r = subprocess.run([ffmpeg, '-v', 'info', '-ss', str(a), '-t', str(b - a), '-i', str(out), '-af', 'volumedetect', '-f', 'null', '-'],
                                   capture_output=True, text=True)
                return float(r.stderr.split('max_volume:')[1].split('dB')[0])
            self.assertLess(level(1.2, 1.8), -60)
            self.assertGreater(level(0.2, 0.8), -20)
            self.assertGreater(level(2.2, 2.8), -20)
            self.assertEqual(media.probe(str(out))['video']['codec_name'], 'h264')


class GeneratedAudio(unittest.TestCase):
    def test_any_rewrite_takes_the_generated_soundtrack(self):
        seg = {'kinds': ['visual'], 'rewrites': {'dialogue': [{'original': "It's four herbs", 'rewrite': "It's twelve herbs"}], 'vo': []}}
        self.assertTrue(swap.generated_audio(seg))
        self.assertFalse(swap.generated_audio({'kinds': ['visual'], 'rewrites': {'dialogue': [], 'vo': []}}))


class HerbCount(unittest.TestCase):
    """User rule 2026-10-24: herb counts are not conflicts; only named ingredients the product lacks are."""
    def fix(self, o, r):
        return swap.only_real_conflicts({'dialogue': {'original': o, 'rewrite': r}, 'vo': None})['dialogue']

    def test_count_only_change_is_dropped(self):
        self.assertIsNone(self.fix("It's four herbs", "It's twelve herbs"))
        self.assertIsNone(self.fix("4 herbs", "12 herbs"))

    def test_count_restored_inside_a_real_rewrite(self):
        self.assertEqual(self.fix('Lymphoria. Four herbs. Half a dropper.', 'Yesnap LymphLoop. Twelve herbs. One stick packet.')['rewrite'],
                         'Yesnap LymphLoop. Four herbs. One stick packet.')
        self.assertEqual(self.fix('4 herbs, one full', '12 herbs, one stick')['rewrite'], '4 herbs, one stick')

    def test_named_ingredient_still_rewritten(self):
        self.assertEqual(self.fix('Stillingia', 'Burdock Root')['rewrite'], 'Burdock Root')


class NoTtsCommand(unittest.TestCase):
    def test_tts_subcommand_is_gone(self):
        r = subprocess.run([sys.executable, str(SCRIPTS/'swap.py'), 'tts', '--help'], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((SCRIPTS/'audio_align.py').exists())


if __name__ == '__main__':
    unittest.main()


class MuteWindowsAndWholeFilmSpeech(unittest.TestCase):
    """2026-11-12, three Nuflos films (user: as few slips in the spoken lines and the picture as possible)."""

    def test_a_mute_window_that_cuts_through_the_word_is_reported(self):
        import numpy as np, words
        rate = 16000; audio = (np.random.default_rng(0).standard_normal(rate * 5) * 0.0005).astype(np.float32)
        audio[int(2.50 * rate):int(3.30 * rate)] += 0.2 * np.sin(np.arange(int(0.80 * rate)) * 0.2).astype(np.float32)   # "New Flows." 2.50-3.30
        spoken = [{'w': 'called', 'a': 2.30, 'b': 2.50}, {'w': 'New', 'a': 2.50, 'b': 2.82}, {'w': 'Flows.', 'a': 2.82, 'b': 3.30}, {'w': 'a', 'a': 3.30, 'b': 3.38}]
        cuts = words.cut_words(audio, spoken, [(2.56, 3.24)], rate)                 # NV0011 G11: came back as "Lymph-loo-naps"
        self.assertEqual([(c['word'], c['side'], c['widen_to']) for c in cuts], [('New', 'start', [2.5, 3.24]), ('Flows.', 'end', [2.56, 3.3])])
        self.assertEqual(words.cut_words(audio, spoken, [(2.50, 3.30)], rate), [])   # the whole word: nothing left over
        self.assertEqual(words.cut_words(audio, spoken, [(2.48, 3.34)], rate), [])   # touching the neighbours is not a cut

    def test_the_whole_film_is_checked_for_the_old_name_ingredients_and_replaced_words(self):
        import speech_gate
        vocab = {'names': ['nuflos'], 'foreign_ingredients': [['prickly', 'ash']], 'replaced': ['bottle', 'scoop']}
        say = lambda text: [{'w': w, 'a': float(i), 'b': i + 0.5} for i, w in enumerate(text.split())]
        h = speech_gate.hits(say("It's called New Flows. Prickly ash wakes it up. Two scoops a day"), vocab)
        self.assertEqual([(x['kind'], x['word']) for x in h['left']], [('competitor_name', 'New'), ('ingredient_not_in_lymphloop', 'prickly ash')])
        self.assertEqual([x['word'] for x in h['check']], ['scoops'])
        clean = speech_gate.hits(say("It's called Lymph Loop. Dandelion wakes it up and your lymph flows. Two packets a day"), vocab)
        self.assertEqual(clean, {'left': [], 'check': []})
        self.assertEqual(speech_gate.hits(say('It is called NuFloce and LymphLoop'), vocab)['left'][0]['word'], 'NuFloce')

    def test_our_brand_split_in_two_words_is_not_the_competitor(self):
        # 2026-11-22 film 1106373148453803: "Yes, snap lymphin loop." flagged as lymphoria
        import speech_gate
        vocab = {'names': ['lymphoria'], 'foreign_ingredients': [], 'replaced': []}
        say = lambda text: [{'w': w, 'a': float(i), 'b': i + 0.5} for i, w in enumerate(text.split())]
        self.assertEqual(speech_gate.hits(say('Yes, snap lymphin loop. One packet a day'), vocab)['left'], [])
        # "lymph" is ordinary vocabulary: only a near-exact pair after it is the name (2026-11-22 film 1409)
        self.assertEqual(speech_gate.hits(say("When estrogen drops, your lymph... It's the system"), vocab)['left'], [])
        self.assertEqual(speech_gate.hits(say('your lymphatic system'), vocab)['left'], [])
        for text in ('It is called Lymphoria drops', 'Lymphoria lymphloop', 'Try Lymphoria now', "It's called Lymph oria"):
            self.assertEqual([x['matches'] for x in speech_gate.hits(say(text), vocab)['left']], ['lymphoria'], text)

    def test_the_vocabulary_comes_from_the_run(self):
        import json, tempfile, speech_gate
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'detect').mkdir(); (run/'plan').mkdir()
            (run/'detect/competitor.json').write_text(json.dumps({'reviewed': True, 'brand_names': ['Nuflos'], 'ingredients_named': ['cleavers', 'prickly ash', 'burdock root']}))
            (run/'plan/segments.json').write_text(json.dumps({'segments': [{'rewrites': {'dialogue': [
                {'original': 'Nu Flows', 'rewrite': 'LymphLoop'}, {'original': 'two scoops', 'rewrite': 'two packets'}, {'original': 'little bottle', 'rewrite': 'little box'}], 'vo': []}}]}))
            self.assertEqual(speech_gate.vocabulary(run), {'names': ['nuflos'], 'foreign_ingredients': [['prickly', 'ash']], 'replaced': ['bottle', 'scoop']})


class PaddedTailSilenced(unittest.TestCase):
    def test_tail_after_the_segment_is_muted(self):
        # 2026-11-22 film 2836804316692183 G15: a 1.5 s segment padded to 4 s carried the next line's sound
        self.assertEqual(swap.with_tail({'duration_s': 1.5}, [(0.0, 1.5)]), [(0.0, 1.5), (1.5, 4.05)])
        self.assertEqual(swap.with_tail({'duration_s': 7.98}, []), [])
