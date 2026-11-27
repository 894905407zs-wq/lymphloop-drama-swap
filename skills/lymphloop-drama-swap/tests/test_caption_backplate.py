import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import caption_lama as lama  # noqa: E402

BLUE = (250, 72, 91)          # BGR of the 1100729319517519 plate


def scene():
    """A 720x1280 frame: warm background, a pink shirt, a caption whose blue plate reaches past the OCR box."""
    f = np.full((1280, 720, 3), (150, 170, 200), np.uint8)
    f[700:1000, 150:600] = (110, 110, 200)                        # pink shirt, saturated but not the plate
    cv2.rectangle(f, (45, 786), (408, 856), BLUE, -1)            # plate: left of and above the text box
    cv2.putText(f, 'money-back guarantee.', (75, 845), cv2.FONT_HERSHEY_DUPLEX, 1.3, (255, 255, 255), 3)
    return f, [(73, 804, 581, 51)]                                # OCR box of the line


class Backplate(unittest.TestCase):
    def test_plate_is_followed_past_the_text_box(self):
        f, boxes = scene()
        m, found = lama.plate_mask(f, boxes)
        self.assertEqual(len(found), 1)
        ys, xs = np.where(m > 0)
        self.assertLessEqual(xs.min(), 45); self.assertLessEqual(ys.min(), 786)
        self.assertGreaterEqual(ys.max(), 856)
        # the shirt next to it is not taken
        self.assertEqual(int(m[950, 300]), 0)
        # the padded line mask alone leaves the plate's left end and top rim
        line = lama.line_mask(f.shape, boxes)
        self.assertEqual(int(line[790, 50]), 0); self.assertEqual(int(m[790, 50]), 255)

    def test_plain_or_coloured_text_has_no_plate(self):
        f = np.full((1280, 720, 3), (150, 170, 200), np.uint8)
        cv2.putText(f, 'for the holidays', (175, 845), cv2.FONT_HERSHEY_DUPLEX, 1.3, (255, 255, 255), 3)
        self.assertEqual(lama.plate_mask(f, [(170, 800, 374, 60)]), (None, []))
        cv2.putText(f, 'YELLOW WORDS', (175, 900), cv2.FONT_HERSHEY_DUPLEX, 1.3, (0, 230, 255), 3)
        self.assertEqual(lama.plate_mask(f, [(170, 860, 374, 60)]), (None, []))

    def test_a_plate_painted_back_is_a_leftover(self):
        f, boxes = scene()
        _, found = lama.plate_mask(f, boxes)
        self.assertTrue(lama.plate_left(f, found))                    # still there
        clean = f.copy(); clean[770:880, 30:700] = (150, 170, 200)
        self.assertFalse(lama.plate_left(clean, found))               # gone
        bar = clean.copy(); cv2.rectangle(bar, (60, 795), (300, 850), BLUE, -1)   # LaMa repainted an empty plate
        self.assertTrue(lama.plate_left(bar, found))

    def test_a_coloured_object_behind_the_words_is_not_a_plate(self):
        f = np.full((1280, 720, 3), (150, 170, 200), np.uint8)
        f[600:1100, 0:720] = (40, 40, 200)                        # a red coat filling the caption band
        cv2.putText(f, 'carrying something', (90, 845), cv2.FONT_HERSHEY_DUPLEX, 1.3, (255, 255, 255), 3)
        self.assertEqual(lama.plate_mask(f, [(85, 804, 500, 51)]), (None, []))

    def test_a_small_plate_over_a_saturated_shirt(self):
        # G08 frame 220: "with a 30-day", the plate only behind "a", a pink shirt (more strong pixels) behind the line
        f = np.full((1280, 720, 3), (150, 170, 200), np.uint8)
        f[700:1000, 120:620] = (60, 60, 220)                         # saturated pink-red shirt, S > 140
        cv2.rectangle(f, (300, 796), (350, 852), BLUE, -1)            # small plate behind one letter
        cv2.putText(f, 'with a 30-day', (200, 840), cv2.FONT_HERSHEY_DUPLEX, 1.4, (255, 255, 255), 3)
        m, found = lama.plate_mask(f, [(195, 800, 330, 54)])
        self.assertEqual(len(found), 1)
        ys, xs = np.where(m > 0)
        self.assertTrue(290 <= xs.min() <= 300 and 350 <= xs.max() <= 362, (xs.min(), xs.max()))
        self.assertEqual(int(m[900, 200]), 0)                          # the shirt stays


if __name__ == '__main__':
    unittest.main()
