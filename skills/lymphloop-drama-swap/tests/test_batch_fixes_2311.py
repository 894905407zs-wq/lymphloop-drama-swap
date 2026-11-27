"""11-22 23:xx thread-3 fixes: rewrite VO split over lines, time cards off the reference, shirt print not a caption
leftover, manual speech gate cache per file."""
import json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))


class RewriteOverLines(unittest.TestCase):
    def test_a_rewrite_spanning_two_lines_and_misspelled_is_found(self):
        import shot_regen as sr
        rw = {'original': "It's called Lymphoria. Half a dropper,", 'rewrite': "It's called yesnap LymphLoop. Take one packet,"}
        self.assertTrue(sr.line_has_rewrite("It's called Lymfuria.", rw))
        self.assertTrue(sr.line_has_rewrite('Half a dropper, morning and night,', rw))
        self.assertFalse(sr.line_has_rewrite('Hit the button below.', rw))


class TimeCards(unittest.TestCase):
    def test_time_cards_are_taken_off_the_reference(self):
        import caption_lama as cl
        for t in ('DAY 1', 'WEEK TWO', 'ONE WEEK LATER', '3 days later'): self.assertTrue(cl.TITLE_CARD.fullmatch(t), t)
        for t in ('DAYLIGHT', 'COVINGTON IRON', 'day by day'): self.assertFalse(cl.TITLE_CARD.fullmatch(t), t)
        d = {'text': 'DAY 1', 'bbox': [300, 80, 120, 40], 'confidence': 0.9}      # top of the frame, not the caption band
        self.assertTrue(cl.reference_caption(d, 720, 1280))


class SpeechCacheKey(unittest.TestCase):
    def test_two_files_do_not_share_words(self):
        import speech_gate as sg
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td)/'a.mp4', Path(td)/'b.mp4'; a.write_bytes(b'1'); b.write_bytes(b'22')
            calls = []
            with mock.patch('words.load_model'), mock.patch('words.transcribe', side_effect=lambda m, p, *x: calls.append(p) or []), \
                 mock.patch.object(sg, 'vocabulary', return_value={'names': [], 'foreign_ingredients': [], 'replaced': []}):
                sg.gate(td, a, Path(td)/'c'); sg.gate(td, b, Path(td)/'c'); sg.gate(td, a, Path(td)/'c')
            self.assertEqual([Path(c).name for c in calls], ['a.mp4', 'b.mp4'])


if __name__ == '__main__':
    unittest.main()


class Later(unittest.TestCase):
    def test_time_card_is_a_title_at_any_height(self):
        import caption_matte as cm
        self.assertTrue(cm.TIME_CARD.fullmatch('DAY 1'))
        self.assertFalse(cm.is_title([317, 238, 89, 29], 720, 1280))     # 29 px: under the general title height

    def test_quiet_still_tail_with_a_whisper_word_is_cut(self):
        import numpy as np, swap
        with mock.patch.object(swap.media, 'frozen_tail', return_value=2.67), \
             mock.patch('words.transcribe', return_value=[{'w': 'you', 'a': 0.5, 'b': 1.3}]), mock.patch('words.load_model'), \
             mock.patch('words.pcm', return_value=np.full(16000, 0.005, np.float32)):          # about -46 dB
            self.assertEqual(swap.trim_frozen_tail(Path('/x/G02-r1-c1.mp4'), 0.0, 4.0, 1.5)[0], 2.67)
        with mock.patch.object(swap.media, 'frozen_tail', return_value=2.67), \
             mock.patch('words.transcribe', return_value=[{'w': 'night', 'a': 0.0, 'b': 0.9}]), mock.patch('words.load_model'), \
             mock.patch('words.pcm', return_value=np.full(16000, 0.1, np.float32)):            # -20 dB: real talk
            self.assertGreater(swap.trim_frozen_tail(Path('/x/G02-r1-c1.mp4'), 0.0, 4.0, 1.5)[0], 3.5)


class SoundRoot(unittest.TestCase):
    def test_a_resplice_borrows_the_first_generation_sound(self):
        import shot_regen as sr
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'generation/jobs').mkdir(parents=True)
            (run/'generation/jobs/G07-a2.json').write_text(json.dumps({'provider': 'local_splice', 'recipe': {'pictures': [
                {'source': 'G07-S128_r1-c1', 'frames_inclusive': [0, 95], 'fit': 50},
                {'source': 'G07-S129_r1-c1', 'frames_inclusive': [0, 95], 'fit': 48},
                {'source': 'G07-a1-c1', 'frames_inclusive': [98, 312]}]}}))
            self.assertEqual(sr.sound_root(run, 'G07-a2-c1', 0, 49), 'G07-a1-c1')
            self.assertEqual(sr.sound_root(run, 'G07-a2-c1', 50, 97), 'G07-a1-c1')
            (run/'generation/jobs/G07-a3.json').write_text(json.dumps({'provider': 'local_splice', 'recipe': {'pictures': [
                {'source': 'G07-S128_r1-c1', 'frames_inclusive': [0, 95]},                 # real speed: timing moved
                {'source': 'G07-a1-c1', 'frames_inclusive': [50, 312]}]}}))
            self.assertIsNone(sr.sound_root(run, 'G07-a3-c1', 0, 49))
            self.assertIsNone(sr.sound_root(run, 'G07-a1-c1', 0, 49))                      # not a splice
