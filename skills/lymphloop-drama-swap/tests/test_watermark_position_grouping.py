"""Disconnected watermark windows must retain exact frames with bounded decodes."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import watermark_brand as wb
import caption_clean


class GroupedPositionWindows(unittest.TestCase):
    def test_native_av_reader_counts_nonzero_sparse_and_disconnected_windows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scanner = caption_clean.scanner(root / 'native')
            frames = np.stack([np.full((96, 64, 3), i * 3, np.uint8) for i in range(60)])
            for rate in (24, 30):
                source = root / f'source-{rate}.mkv'
                subprocess.run([wb.media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo',
                                '-pix_fmt', 'bgr24', '-s', '64x96', '-r', str(rate), '-i', '-',
                                '-c:v', 'ffv1', str(source)], input=frames.tobytes(), check=True)
                with mock.patch.object(wb.media, 'probe', return_value={'fps': f'{rate}/1', 'frame_count': 60,
                                       'video': {'width': 64, 'height': 96}}), \
                     mock.patch.object(wb, 'position_slots', return_value=[('p0', 0, 0, 64, 96, 0, None, 64, 96)]), \
                     mock.patch.object(caption_clean, 'scanner', return_value=scanner):
                    sparse = wb.position_scan(source, [{'id': 'p0', 'box': [0, 0, 64, 96]}],
                                              root / f'sparse-{rate}', step=4, workers=2)
                    self.assertEqual(sparse['scanned_frames'], 16)
                    dense = wb.position_scan(source, [{'id': 'p0', 'box': [0, 0, 64, 96]}],
                                             root / f'dense-{rate}', step=1, workers=2,
                                             ranges=[(3, 9), (16, 21), (31, 36), (47, 53)])
                    self.assertEqual(dense['scanned_frames'], 22)

    def test_hundreds_of_windows_preserve_coverage_in_four_groups(self):
        windows = [(i * 100, i * 100 + 31) for i in range(200)]
        groups = wb.range_groups(windows, 4)
        self.assertLessEqual(len(groups), 4)
        actual = [f for group in groups for a, b in group for f in range(a, b)]
        wanted = [f for a, b in windows for f in range(a, b)]
        self.assertEqual(sorted(actual), wanted)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertTrue(all(group == sorted(group) for group in groups))
        self.assertEqual(wb.range_groups([], 4), [])

    def test_real_decoded_frame_order_and_mapping_across_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'source.mkv'
            frames = np.stack([np.full((96, 64, 3), i * 3, np.uint8) for i in range(60)])
            subprocess.run([wb.media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo',
                            '-pix_fmt', 'bgr24', '-s', '64x96', '-r', '24', '-i', '-',
                            '-c:v', 'ffv1', str(source)], input=frames.tobytes(), check=True)
            run = subprocess.run
            scanner = root / 'test-scanner'
            scanner.write_text('test OCR fixture')
            calls = []

            def execute(cmd, *args, **kwargs):
                if Path(cmd[0]).name != 'test-scanner':
                    return run(cmd, *args, **kwargs)
                cap = cv2.VideoCapture(str(cmd[1])); rows = []; index = 0
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    mean = float(frame[10:30, 10:30].mean())
                    rows.append({'frame': index, 'text': 'LYMPHORIA ' + str(mean),
                                 'bbox': [4, 4, 30, 10], 'confidence': 1})
                    index += 1
                cap.release(); calls.append(index)
                Path(cmd[2]).write_text(json.dumps({'frame_count': index, 'detections': rows}))
                return subprocess.CompletedProcess(cmd, 0)

            windows = [(3, 9), (16, 21), (31, 36), (47, 53)]
            with mock.patch.object(wb.media, 'probe', return_value={'fps': '24/1', 'frame_count': 60, 'video': {'width': 64, 'height': 96}}), \
                 mock.patch.object(wb, 'position_slots', return_value=[('p0', 0, 0, 64, 96, 0, None, 64, 96)]), \
                 mock.patch.object(caption_clean, 'scanner', return_value=scanner), \
                 mock.patch.object(subprocess, 'run', side_effect=execute):
                result = wb.position_scan(source, [{'id': 'p0', 'box': [0, 0, 64, 96]}],
                                          root / 'scan', ranges=windows, step=1, workers=2)
                initial_calls = len(calls)
                cached = wb.position_scan(source, [{'id': 'p0', 'box': [0, 0, 64, 96]}],
                                          root / 'scan', ranges=windows, step=1, workers=2)
                self.assertEqual(cached, result)
                self.assertEqual(len(calls), initial_calls)
                (root / 'scan/dense-0.mp4').write_bytes(b'incomplete interrupted encode')
                repaired = wb.position_scan(source, [{'id': 'p0', 'box': [0, 0, 64, 96]}],
                                            root / 'scan', ranges=windows, step=1, workers=2)
                self.assertEqual(repaired, result)
                self.assertEqual(len(calls), initial_calls + 1)
            wanted = [f for a, b in windows for f in range(a, b)]
            self.assertEqual([d['frame'] for d in result['detections']], wanted)
            self.assertEqual(result['scanned_frames'], len(wanted))
            self.assertLessEqual(initial_calls, 2)
            for detection in result['detections']:
                mean = float(detection['text'].split()[-1])
                self.assertAlmostEqual(mean, detection['frame'] * 3, delta=2)


if __name__ == '__main__':
    unittest.main()
