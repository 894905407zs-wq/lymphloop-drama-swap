import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import media_pipeline as media
import shots


def synthetic(path, fps=25):
    """red 2 s + blue 2 s, one hard cut, 440 Hz sine audio, 64x96 portrait."""
    media.ff(['-y', '-f', 'lavfi', '-i', f'color=c=red:s=64x96:r={fps}:d=2', '-f', 'lavfi', '-i', f'color=c=blue:s=64x96:r={fps}:d=2',
              '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
              '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]', '-map', '2:a',
              '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)])


def png_size(path):
    data = Path(path).read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    return struct.unpack('>II', data[16:24])


def peak_audio(path):
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-i', str(path), '-map', '0:a:0', '-f', 's16le', '-ac', '1', '-ar', '16000', 'pipe:1'],
                         capture_output=True).stdout
    return max((abs(s[0]) for s in struct.iter_unpack('<h', raw)), default=0)


class ShotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root / 'source.mp4'; synthetic(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_detect_splits_at_hard_cut_and_covers_timeline(self):
        record = shots.detect(self.source, self.root / 'shots', threshold=.3, min_shot_seconds=.4)
        self.assertEqual(record['frame_count'], 100)
        self.assertEqual(len(record['shots']), 2, record['shots'])
        first, second = record['shots']
        self.assertEqual((first['in_frame'], first['out_frame']), (0, 50)); self.assertEqual((second['in_frame'], second['out_frame']), (50, 100))
        self.assertEqual([s['id'] for s in record['shots']], ['S001', 'S002'])
        for shot in record['shots']:
            w, h = png_size(shot['sheet'])
            self.assertGreaterEqual(w, 480); self.assertTrue(Path(shot['mid_frame']).is_file())
            self.assertTrue(all(shot['in_frame'] <= f < shot['out_frame'] for f in shot['sampled_frames']))
            self.assertLessEqual(len(shot['sampled_frames']), 16)
        saved = json.loads((self.root / 'shots/shots.json').read_text())
        self.assertEqual(saved['boundary_convention'], '[in,out)')

    def test_merge_short_folds_fragments(self):
        self.assertEqual(shots.merge_short([0, 3, 50, 100], '25', .4), [0, 50, 100])
        self.assertEqual(shots.merge_short([0, 50, 97, 100], '25', .4), [0, 50, 100])
        self.assertEqual(shots.merge_short([0, 50, 100], '25', .4), [0, 50, 100])
        self.assertEqual(shots.merge_short([0, 100], '25', .4), [0, 100])

    def test_cut_is_frame_accurate_and_mutes(self):
        clip = shots.cut(self.source, 10, 40, self.root / 'clip.mp4')
        self.assertEqual(clip['frame_count'], 30); self.assertTrue(clip['has_audio']); self.assertGreater(peak_audio(clip['path']), 1000)
        self.assertEqual(clip['sha256'], shots.sha(clip['path']))
        muted = shots.cut(self.source, 10, 40, self.root / 'muted.mp4', mute=True)
        self.assertFalse(muted['has_audio']); self.assertEqual(muted['frame_count'], 30)
        with self.assertRaises(ValueError):
            shots.cut(self.source, 90, 120, self.root / 'bad.mp4')

    def test_cut_downscales_long_edge_and_keeps_even_sizes(self):
        big = self.root / 'big.mp4'
        media.ff(['-y', '-f', 'lavfi', '-i', 'testsrc2=s=1080x1920:r=25:d=1', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(big)])
        clip = shots.cut(big, 0, 25, self.root / 'small.mp4', max_short_edge=720)
        info = media.probe(clip['path'])
        self.assertEqual(info['video']['width'], 720); self.assertEqual(info['video']['height'], 1280)

    def test_sheet_and_frames_for_candidates(self):
        clip = shots.cut(self.source, 0, 50, self.root / 'cand.mp4')
        sheet = shots.sheet(clip['path'], self.root / 'cand.png', fps_sample=2, cols=4, max_frames=16, label_offset_seconds=3)
        self.assertTrue(Path(sheet['path']).is_file()); self.assertEqual(len(sheet['frames']), 4)
        frames = shots.frames(clip['path'], self.root / 'frames', fps_sample=4, maximum=5)
        self.assertEqual(len(frames['frames']), 5); self.assertTrue(all(Path(r['path']).is_file() for r in frames['frames']))

    def test_cli_round_trip(self):
        out = subprocess.run([sys.executable, str(Path(shots.__file__)), 'detect', str(self.source), '--out', str(self.root / 'cli')], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr); self.assertEqual(json.loads(out.stdout)['shots'], 2)


if __name__ == '__main__':
    unittest.main()
