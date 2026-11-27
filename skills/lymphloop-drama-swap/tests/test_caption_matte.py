"""A removed old brand caption must not swallow the heard replacement caption."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import caption_matte as cm


class ReplacementBrandCaption(unittest.TestCase):
    def test_filtered_standalone_brand_is_redrawn_but_unchanged_caption_stays_copied(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            (run / 'clips/caption_work').mkdir(parents=True)
            for name in ('G01.hq.ref.mp4', 'G01.hq.clean.ref.mp4'):
                (run / 'clips' / name).touch()
            scan = {'width': 720, 'height': 1280, 'detections': [
                {'frame': f, 'text': text, 'bbox': [240, 760, 240, 30]}
                for text, frames in [('Hello', range(3, 18)), ('Lymphoria', range(33, 48))] for f in frames]}
            (run / 'clips/caption_work/G01.hq.ref.text.json').write_text(json.dumps(scan))
            seg = {'id': 'G01', 'speech': [{'text': 'Hello. Lymphoria.'}],
                   'rewrites': {'dialogue': [{'original': 'Lymphoria', 'rewrite': 'LymphLoop'}], 'vo': []}}
            hello = {'a': 0.1, 'b': 0.6, 'lines': ['Hello']}
            brand = {'a': 1.1, 'b': 1.6, 'lines': ['LymphLoop']}
            render = {'caps': [hello, brand], 'heard': [{'w': 'Hello', 'a': .1, 'b': .5},
                                                       {'w': 'LymphLoop', 'a': 1.1, 'b': 1.6}]}
            with mock.patch('caption_clean.is_caption', return_value=True), \
                    mock.patch.object(cm.media, 'probe', return_value={'fps': 30}), \
                    mock.patch.object(cm, 'mattes', return_value=[]), \
                    mock.patch.object(cm, 'composite', return_value={'frames': 60}) as composite:
                receipt = cm.apply(run, seg, {}, 'base.mp4', 'out.mp4', {}, ['Hello.', 'LymphLoop.'],
                                   {'lymphoria'}, lambda t: 'lymphoria' in t.lower(), render)
            self.assertEqual(composite.call_args.args[7], [brand])
            self.assertEqual(receipt['drawn'], ['LymphLoop'])


if __name__ == '__main__':
    unittest.main()


class GlyphGate(unittest.TestCase):
    """2026-11-20 film 1753 G02: a bottle label inside a caption box was copied with the caption."""
    def test_only_caption_coloured_pixels_pass(self):
        import numpy as np, caption_matte as cm
        crop = np.full((20, 60, 3), (200, 220, 235), np.uint8)       # cream label (BGR)
        crop[5:15, 5:10] = (20, 40, 80)                            # dark-brown label print
        crop[5:15, 40:45] = (255, 255, 255)                        # white caption glyph
        g = cm.glyph_gate(crop, [[255, 255, 255]], 2)
        self.assertEqual(g[10, 7], 0.0); self.assertEqual(g[10, 42], 1.0); self.assertEqual(g[10, 38], 1.0)


class ProductPrintMisread(unittest.TestCase):
    def test_misread_label_is_not_a_title(self):
        import caption_matte as cm
        banned = frozenset({'lymphatic', 'drainage', 'lymphoria'})
        self.assertFalse(cm.readable('drair', banned)); self.assertFalse(cm.readable('drai', banned))
        self.assertFalse(cm.readable('lymphatlc', banned))
        self.assertTrue(cm.readable('SEMANA 5', banned)); self.assertTrue(cm.readable('Primera semana', banned))

class MisspelledOldName(unittest.TestCase):
    def test_a_caption_spelling_the_old_brand_its_own_way_is_redrawn(self):
        # 2026-11-22 film 2896957363997251 G06: "CALLED LYMFORIA" for the spoken Lymphoria was copied back
        old = {'lymphoria', 'dropper'}
        self.assertTrue(cm.changed_event({'key': ['called', 'lymforia']}, old))
        self.assertTrue(cm.changed_event({'key': ['one', 'full', 'dropper']}, old))
        for key in (['tastes', 'like', 'honey'], ['yesnap', 'lymphloop'], ['your', 'lymphatic', 'system']):
            self.assertFalse(cm.changed_event({'key': key}, old), key)

class LostCaptionLine(unittest.TestCase):
    """2026-11-22 film 1801069464466244 G02: the caption CLEAVERS was never copied (spelled like the carton's
    "cleanse"; half its frames read CLEAVERSR) and the word was said with no caption."""
    def test_a_said_word_is_not_taken_for_product_print(self):
        banned = frozenset({'cleanse', 'calendula', 'bromelain'})
        self.assertFalse(cm.readable('CLEAVERS', banned))
        self.assertTrue(cm.readable('CLEAVERS', banned, said=frozenset({'cleavers', 'sweeps'})))
        self.assertTrue(cm.readable('CLEAVERSR', banned, said=frozenset({'cleavers'})))
        self.assertFalse(cm.readable('CLEANSE', banned | {'cleanse'}, said=frozenset({'cleavers'})))

    def test_ocr_spellings_of_one_line_stay_one_line(self):
        self.assertTrue(cm.says('CLEAVERSR', {'cleavers', 'sweeps'}, {'cleavers', 'sweeps', 'it', 'out'}))
        self.assertTrue(cm.similar(('cleavers',), ('cleaversr',)))
        self.assertFalse(cm.similar(('sweeps', 'it', 'out'), ('cleavers',)))
        self.assertFalse(cm.says('CLOVER', {'cleavers'}, {'cleavers'}))

    def test_heard_lines_the_copy_does_not_show_are_drawn(self):
        cap = lambda a, b, text: {'a': a, 'b': b, 'lines': [text]}
        caps = [cap(3.4, 4.7, 'BREAKS IT DOWN'), cap(4.7, 6.2, 'CLEAVERS'), cap(6.5, 7.8, 'SWEEPS IT OUT')]
        line = lambda *key: {'key': key}
        events = [line('breaks', 'it', 'down'), line('sweeps', 'it', 'out')]
        shown = [[3.46, 4.77], [6.56, 7.77]]
        self.assertEqual([c['lines'] for c in cm.uncovered_caps(caps, [], events, shown)], [['CLEAVERS']])
        # copied but ended part way (the OCR stopped reading it): drawn for its whole time
        events2 = events[:1] + [line('cleavers')] + events[1:]
        self.assertEqual(cm.uncovered_caps(caps, [], events2, [[3.46, 4.77], [4.77, 5.98], [6.56, 7.77]]), [])
        self.assertEqual([c['lines'] for c in cm.uncovered_caps(caps, [], events2, [[3.46, 4.77], [4.77, 5.2], [6.56, 7.77]])], [['CLEAVERS']])
        # a line already drawn (replaced word) is not drawn twice
        self.assertEqual(cm.uncovered_caps(caps, [caps[1]], events, shown), [])


class TitleCardsAndUnsaidLines(unittest.TestCase):
    def test_a_title_card_in_a_corner_is_a_title(self):
        # 2026-11-22 film 1585905389415123: WEEK 1-6 top left, 38 px high in 720x1280
        self.assertTrue(cm.is_title([29, 201, 170, 38], 720, 1280))
        self.assertFalse(cm.is_title([29, 201, 30, 38], 720, 1280))          # not horizontal
        self.assertFalse(cm.is_title([300, 600, 60, 14], 720, 1280))         # tiny print

    def test_a_line_the_clip_does_not_say_is_left_out(self):
        heard = [{'w': 'You', 'a': 0.2, 'b': 0.4}, {'w': 'spent', 'a': 0.4, 'b': 0.7}, {'w': 'everything.', 'a': 2.6, 'b': 3.2}]
        ev = lambda key, k0, k1: {'key': key, 'k0': k0, 'k1': k1}
        self.assertFalse(cm.heard_near(ev(('then', 'you'), 101, 110), 30, 0.15, heard))          # G10: muted repeat
        self.assertTrue(cm.heard_near(ev(('you', 'spent'), 5, 30), 30, 0.15, heard))
        self.assertFalse(cm.heard_near(ev(('i', "can't", 'believe', 'it'), 0, 40), 30, 0.0, []))   # silent sip
        said = [{'w': "It's", 'a': 0.02, 'b': 0.2}, {'w': 'called', 'a': 0.2, 'b': 0.44}]
        self.assertFalse(cm.heard_near(ev(('taste', 'it', "it's"), 0, 6), 30, 0.08, said))       # 2896 G06: the line before
        heads = [{'w': 'heads', 'a': 0.5, 'b': 0.8}, {'w': 'up', 'a': 0.8, 'b': 1.0}]
        self.assertTrue(cm.heard_near(ev(('and', 'heads', 'up'), 10, 30), 30, 0.0, heads))        # Whisper dropped "and"
        self.assertTrue(cm.heard_near(ev(('4', 'herbs'), 10, 30), 30, 0.0, [{'w': 'Four', 'a': 0.4, 'b': 0.6}, {'w': 'herbs.', 'a': 0.6, 'b': 1.0}]))

    def test_one_word_and_one_line_as_ocr_reads_them(self):
        self.assertTrue(cm.same_word('link', "link's")); self.assertTrue(cm.same_word('weeks', 'week'))
        self.assertFalse(cm.same_word('it', 'its')); self.assertFalse(cm.same_word('clover', 'cleavers'))
        self.assertTrue(cm.similar(('if', 'the', 'link'), ('ifthe', 'link')))
        self.assertTrue(cm.similar(('still', 'in', 'stock'), ('stildin', 'stock')))
        self.assertFalse(cm.similar(('if', 'the', 'link'), ('still', 'in', 'stock')))

    def test_a_drawn_line_takes_over_the_copied_part_of_it(self):
        # 2114607189446748 G04: GIVE ME 6 lost, WEEKS copied; the drawn GIVE ME SIX WEEKS replaces both
        gaps = [{'a': 8.28, 'b': 9.66, 'lines': ['GIVE ME SIX WEEKS']}]
        lines = [{'key': ('then', "don't", 'watch')}, {'key': ('weeks',)}]
        self.assertEqual(cm.merged_into(gaps, lines, [[7.39, 8.56], [9.16, 9.96]]), {1})
        caps = [{'a': 5.68, 'b': 8.28, 'lines': ["THEN DON'T WATCH"]}, gaps[0]]
        self.assertEqual(cm.uncovered_caps(caps, [], lines, [[7.39, 8.56], [9.16, 9.96]]), [gaps[0]])   # a late start is not a gap
        self.assertEqual(cm.uncovered_caps([{'a': 4.7, 'b': 5.5, 'lines': ['Four herbs']}], [], [{'key': ('4', 'herbs')}], [[5.38, 6.6]]), [])
        # 1801 G05: a heard line of short words between two copied lines is not drawn ("BE IN" doubled them)
        self.assertEqual(cm.uncovered_caps([{'a': 3.2, 'b': 3.5, 'lines': ['BE IN']}], [], [{'key': ("you're", 'going', 'to', 'be')}, {'key': ('in', 'the', 'bathroom')}],
                                           [[2.75, 3.25], [3.4, 3.95]]), [])

