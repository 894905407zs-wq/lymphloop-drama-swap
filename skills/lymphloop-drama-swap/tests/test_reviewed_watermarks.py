"""Reviewed floating marks must never include native package print or subtitles."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import swap


class ReviewedWatermarks(unittest.TestCase):
    def setUp(self):
        self.review = {'reviewed': True, 'all_candidates_classified': True,
                       'source_sha256': 'source-a', 'width': 720, 'height': 1280,
                       'positions': [{'id': 0, 'box': [674, 23, 22, 135], 'vertical': True},
                                     {'id': 1, 'box': [486, 1046, 141, 22], 'vertical': False}]}

    def test_bottle_logos_and_highlighted_brand_subtitles_are_kept(self):
        data = {'detections': [
            {'bbox': [674, 23, 22, 135], 'classification': 'floating_overlay'},
            {'bbox': [277, 612, 159, 27], 'classification': 'floating_overlay'},
            {'bbox': [164, 932, 339, 49], 'classification': 'floating_overlay'},
            {'bbox': [251, 1218, 101, 28], 'classification': 'floating_overlay'}]}
        swap.classify_reviewed_watermarks(data, self.review, 720, 1280)
        self.assertEqual([d['classification'] for d in data['detections']],
                         ['floating_overlay', 'printed_label_keep', 'printed_label_keep', 'printed_label_keep'])
        self.assertTrue(data['overlay_classification_reviewed'])
        self.assertTrue(swap.reviewed_watermark_box([337, 12, 11, 67], self.review, 360, 640))
        self.assertFalse(swap.reviewed_watermark_box([125, 609, 51, 14], self.review, 360, 640))

    def test_source_bound_review_cannot_be_used_on_another_master(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run / 'watermark').mkdir()
            (run / 'watermark/agent_positions.json').write_text(json.dumps(self.review))
            state = {'stages': {'prepare': {'cfr_master': {'sha256': 'source-b'}}}}
            with self.assertRaises(SystemExit):
                swap.watermark_position_review(run, state)
            state['stages']['prepare']['cfr_master']['sha256'] = 'source-a'
            self.assertEqual(swap.watermark_position_review(run, state), self.review)


if __name__ == '__main__':
    unittest.main()
