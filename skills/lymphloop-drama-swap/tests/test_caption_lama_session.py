"""Concurrent reference/frame workers must share one LaMa model instance."""
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import caption_lama as lama


class SessionLoading(unittest.TestCase):
    def test_readable_bottle_label_kept_while_spoken_caption_and_title_removed(self):
        policy = {'spoken_filter': True, 'min_text_height_px': 36,
                  'title_pattern': r'(?i)^WEEK\s*\d+$'}
        spoken = ["It's called Lymphoria."]
        def row(text, box):
            return {'text': text, 'bbox': box, 'confidence': 1, 'frame': 0}
        self.assertFalse(lama.reference_caption(row('drainage', [321, 762, 93, 27]), 720, 1280, policy, spoken))
        self.assertFalse(lama.reference_caption(row('Lymphoria', [320, 700, 100, 18]), 720, 1280, policy, spoken))
        self.assertTrue(lama.reference_caption(row('Lymphoria.', [225, 862, 279, 48]), 720, 1280, policy, spoken))
        self.assertTrue(lama.reference_caption(row('WEEK 1', [220, 100, 280, 72]), 720, 1280, policy, []))
        self.assertFalse(lama.reference_caption(row('drainage', [321, 762, 93, 48]), 720, 1280, policy, []))
        self.assertTrue(lama.reference_caption(row('itall', [225, 862, 279, 48]), 720, 1280, policy,
                                               ['Prickly ash gets it all flowing again.']))
        self.assertTrue(lama.reference_caption(row('1-9', [225, 862, 279, 48]), 720, 1280, policy,
                                               ['Breaker one-nine.']))
        self.assertTrue(lama.reference_caption(row('70%', [225, 862, 279, 48]), 720, 1280, policy,
                                               ['Seventy percent off.']))

    def test_soda_logo_false_ocr_is_not_erased_with_actual_subtitles(self):
        caption = [223, 862, 278, 48]
        logo = [353, 700, 113, 63]
        scan = {'width': 720, 'height': 1280, 'frame_count': 30,
                'detections': [{'frame': 10, 'bbox': logo, 'text': '40 092)', 'confidence': .3},
                               {'frame': 10, 'bbox': caption, 'text': 'Same seat.', 'confidence': 1}]}
        held, raw = lama.held_boxes(scan)
        self.assertEqual(raw[10], [caption])
        self.assertTrue(all(logo not in boxes for boxes in held.values()))
        self.assertEqual(held[9], [caption])  # subtitle fades remain covered

    def test_concurrent_first_calls_load_once(self):
        barrier = threading.Barrier(9)
        instance = object()

        def load(*args, **kwargs):
            time.sleep(.04)  # Real ONNX loading releases the GIL.
            return instance

        constructor = mock.Mock(side_effect=load)
        ort = SimpleNamespace(SessionOptions=SimpleNamespace, InferenceSession=constructor)

        def first_call(_):
            barrier.wait()
            return lama.session()

        with mock.patch.object(lama, '_session', None), \
             mock.patch.object(lama, 'model_path', return_value=Path('/cached/lama.onnx')), \
             mock.patch.dict(sys.modules, {'onnxruntime': ort}):
            with ThreadPoolExecutor(9) as pool:
                results = list(pool.map(first_call, range(9)))
            self.assertEqual(constructor.call_count, 1)
            self.assertTrue(all(result is instance for result in results))


if __name__ == '__main__':
    unittest.main()
