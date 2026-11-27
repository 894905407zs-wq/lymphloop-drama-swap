"""words.py: Whisper's lines on silent tracks are dropped; the source word cache follows the planned range (2026-11-22)."""
import json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import words as W  # noqa: E402


class Hallucination(unittest.TestCase):
    def test_stock_lines_on_silence_are_dropped(self):
        seg = lambda nsp, lp: SimpleNamespace(no_speech_prob=nsp, avg_logprob=lp)
        self.assertTrue(W.hallucinated(seg(0.55, -0.98)))     # 180 G08 "Thank you so much for watching"
        self.assertTrue(W.hallucinated(seg(0.49, -1.42)))     # 110 G06 "It's not that bad."
        self.assertFalse(W.hallucinated(seg(0.00, -0.17)))    # real lines
        self.assertFalse(W.hallucinated(seg(0.04, -0.40)))
        self.assertFalse(W.hallucinated(seg(0.55, -0.30)))    # unsure but confident words stay


class SourceCache(unittest.TestCase):
    def test_a_moved_segment_is_heard_again(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'plan/words').mkdir(parents=True)
            (run/'plan/words/G14.json').write_text(json.dumps([{'w': 'old', 'a': 0, 'b': 1}]))     # from before: no range
            seg = {'id': 'G14', 'in_frame': 300, 'out_frame': 600}
            with mock.patch.object(W, 'transcribe', return_value=[{'w': 'new', 'a': 0, 'b': 1}]) as t, mock.patch.object(W, 'load_model'):
                self.assertEqual(W.source_words(run, seg, 30.0, '/src.mp4')[0]['w'], 'new')
                self.assertEqual(W.source_words(run, seg, 30.0, '/src.mp4')[0]['w'], 'new'); self.assertEqual(t.call_count, 1)   # cached
                W.source_words(run, {**seg, 'out_frame': 660}, 30.0, '/src.mp4'); self.assertEqual(t.call_count, 2)          # re-planned
            self.assertIsNone(W.source_words(run, {**seg, 'in_frame': 1}, 30.0, '/src.mp4', refresh=False))


class HerbCount(unittest.TestCase):
    def test_a_count_of_herbs_is_not_a_foreign_ingredient(self):
        import speech_gate
        prof = {'ingredients_named': ['4 herbs', 'Stillingia', 'Cleavers'], 'brand_names': ['Lymphoria']}
        with mock.patch.object(speech_gate.comp, 'active', return_value=prof), mock.patch.object(speech_gate.comp, 'names', return_value={'lymphoria'}):
            with tempfile.TemporaryDirectory() as td:
                self.assertEqual(speech_gate.vocabulary(td)['foreign_ingredients'], [['stillingia']])


if __name__ == '__main__':
    unittest.main()
