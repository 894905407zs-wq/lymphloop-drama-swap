"""Watermark detection (fuzzy match + edge pass), the white LYMPHLOOP stamp, and final-screen's brand requirement.

User rule (2026-10-25): the original watermark (droplet icon + LYMPHORIA) is cleaned and the same place gets LYMPHLOOP in
opaque white, same position, size and orientation, icon included. The batch shipped seven films with watermark frames the
exact-match scan missed (faint marks OCR as LYMPHORTA / LYMRHORIA / MPHORIA, or only after the edge is rotated and enlarged)."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import watermark_brand as wb  # noqa: E402
import media_pipeline as media  # noqa: E402


def make_video(path, frames, size=(360, 640), draw=None, fps=24):
    """Grey test video; draw(frame_index, PIL image) may paint on each frame."""
    W, H = size
    enc = subprocess.Popen([media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(fps),
                            '-i', '-', '-f', 'lavfi', '-i', f'sine=d={frames / fps}', '-shortest', '-c:v', 'libx264', '-crf', '4',
                            '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], stdin=subprocess.PIPE)
    for i in range(frames):
        im = Image.new('RGB', (W, H), (120, 110, 100))
        if draw: draw(i, im)
        enc.stdin.write(im.tobytes())
    enc.stdin.close(); enc.wait()


def frame_rgb(path, n):
    info = media.probe(str(path)); W, H = int(info['video']['width']), int(info['video']['height'])
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-i', str(path), '-vf', f"select='eq(n\\,{n})'", '-fps_mode', 'passthrough',
                          '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(H, W, 3)


class ScanCoverage(unittest.TestCase):
    def test_full_range_adaptive_scan_is_accepted_but_partial_or_sparse_is_rejected(self):
        import watermark_cleanup as wc
        source = {'path': 'source', 'sha256': 'hash'}
        scan = {'type': 'lymphloop_watermark_scan', 'target': 'LYMPHORIA', 'source': source,
                'frame_count': 100, 'scanned_frames': 40,
                'coverage': 'every 4th frame of [(0, 100)], every frame within 12 of a hit',
                'sparse_hits': 1, 'dense_ranges': [[40, 65]], 'detections': []}
        wc.validate_scan(scan, {'frame_count': 100}, source)
        for coverage in ('every 4th frame of [(20, 100)], every frame within 12 of a hit',
                         'every 20th frame of [(0, 100)], every frame within 12 of a hit'):
            with self.assertRaises(ValueError):
                wc.validate_scan({**scan, 'coverage': coverage}, {'frame_count': 100}, source)
        wc.validate_scan({**scan, 'scanned_frames': 100, 'coverage': 'every_frame'}, {'frame_count': 100}, source)


class Matching(unittest.TestCase):
    def test_fuzzy_reads_count_and_the_logo_and_stamp_do_not(self):
        for t in ('@LYMPHORIA', '& LYMPHORTA', 'LYMRHORIA', 'MPHORIA', '@LYMPHOIN', '@ LYMPHO', 'SLYMPHORL'):
            self.assertTrue(wb.is_watermark_text(t), t)
        for t in ('LyMPHOR', 'LуmPHOR', 'LYMPHLOOP', 'LYMPHOOP', 'yesnap', 'LYMPHATIC DRAINAGE'):
            self.assertFalse(wb.is_watermark_text(t), t)

    def test_icon_is_added_in_front_of_the_word(self):
        self.assertEqual(wb.with_icon({'bbox': [100, 50, 80, 10], 'text': 'LYMPHORIA'}), [89, 50, 91, 10])
        self.assertEqual(wb.with_icon({'bbox': [100, 50, 80, 10], 'text': '@LYMPHORIA'}), [100, 50, 80, 10])
        self.assertEqual(wb.with_icon({'bbox': [10, 100, 10, 70], 'text': 'LYMPHORIA'}), [10, 89, 10, 92])   # vertical: both ends


class EdgeGeometry(unittest.TestCase):
    def test_boxes_found_in_rotated_enlarged_strips_map_back_to_the_frame(self):
        """Paint a block, push the frame through the same crop/transpose/scale ffmpeg filters, find it, map it back."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'s.mp4'
            make_video(src, 3, draw=lambda i, im: ImageDraw.Draw(im).rectangle([20, 100, 31, 170], fill=(255, 255, 255)))
            for v in [x for x in wb.edge_variants(360, 640) if x[0] in ('left_cw', 'left_ccw', 'top')]:
                name, x0, y0, cw, ch, t, _ = v; k = 3.0
                vf = [f'crop={cw}:{ch}:{x0}:{y0}'] + ([f'transpose={t}'] if t else []) + [f'scale=iw*{k}:ih*{k}:flags=neighbor']
                out = Path(td)/f'{name}.mp4'
                subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-y', '-i', str(src), '-vf', ','.join(vf), '-frames:v', '1', '-c:v', 'libx264', '-crf', '0', str(out)], check=True)
                g = frame_rgb(out, 0).mean(axis=2) > 200
                ys, xs = np.nonzero(g)
                if not len(xs): continue        # block not inside this strip
                box = [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
                back = wb.map_box(box, v, k)
                for got, want in zip(back, [20, 100, 12, 71]): self.assertAlmostEqual(got, want, delta=3, msg=f'{name}: {back}')

    def test_edge_pass_reads_a_faint_vertical_watermark_the_full_frame_pass_misses(self):
        font = ImageFont.truetype(wb._font_path(), 14)
        def draw(i, im):
            t = Image.new('L', (110, 18), 0); ImageDraw.Draw(t).text((0, 0), '@LYMPHORIA', font=font, fill=255)
            t = t.rotate(-90, expand=True); layer = Image.new('RGB', t.size, (235, 235, 235)); im.paste(layer, (18, 20), t.point(lambda p: p * 0.55))
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'s.mp4'; make_video(src, 9, draw=draw)
            rec = wb.edge_scan(src, Path(td)/'edge.json', Path(td)/'work')
            self.assertTrue(rec['detections'], 'edge pass found nothing')
            box = wb._union([d['bbox'] for d in rec['detections'] if d['frame'] == 0] or [rec['detections'][0]['bbox']])
            self.assertTrue(10 <= box[0] <= 26 and box[1] <= 30 and box[1] + box[3] >= 110, box)


class CaptionLines(unittest.TestCase):
    def test_a_caption_naming_the_competitor_is_cleaned_but_not_stamped(self):
        self.assertTrue(wb.caption_line('LYMPHORIA 4 HERBS')); self.assertFalse(wb.caption_line('@ LYMPHORIA'))
        self.assertFalse(wb.caption_line('LYM PHORIA'))
        scan = {'detections': [{'frame': f, 'classification': 'floating_overlay', 'bbox': [51, 410, 225, 31], 'text': 'LYMPHORIA 4 HERBS'} for f in range(5)]
                + [{'frame': 7, 'classification': 'floating_overlay', 'bbox': [52, 410, 224, 31], 'text': 'LYMPHORIA', 'synthetic': 'fade_pad'}]
                + [{'frame': f, 'classification': 'floating_overlay', 'bbox': [20, 100, 12, 70], 'text': 'LYMPHORIA'} for f in range(5)]}
        p = wb.plan(scan, 360, 640)
        self.assertEqual(p['tracks'], 1); self.assertEqual(p['track_list'][0]['box'][0], 20)


class Plan(unittest.TestCase):
    def scan(self, dets): return {'detections': [{'classification': 'floating_overlay', **d} for d in dets]}

    def test_same_mark_from_two_passes_is_one_stamp_and_frames_link_into_a_track(self):
        dets = []
        for f in range(10):
            dets.append({'frame': f, 'bbox': [20, 100 + (f % 2), 12, 70], 'text': 'LYMPHORIA'})
            dets.append({'frame': f, 'bbox': [19, 95, 12, 60], 'text': '@LYMPHOR', 'pass': 'left_cw'})
        dets.append({'frame': 3, 'bbox': [300, 600, 50, 10], 'text': 'LYMPHORIA'})       # a second, horizontal mark
        p = wb.plan(self.scan(dets), 360, 640)
        self.assertEqual(p['tracks'], 2)
        self.assertEqual([len(p['stamps'][str(f)]) for f in (0, 3, 9)], [1, 2, 1])
        v = next(t for t in p['track_list'] if t['vertical']); h = next(t for t in p['track_list'] if not t['vertical'])
        self.assertEqual((v['orient_source'], h['orient']), ('pending', 'h'))
        self.assertEqual(p['stamps']['5'][0]['box'][1], 98)                             # median of both passes (not their union), smoothed

    def test_flash_decides_the_reading_direction_of_vertical_tracks(self):
        # only when external Gemini is allowed; the plugin's default is the next test
        dets = [{'frame': f, 'bbox': [20, 100, 12, 70], 'text': 'LYMPHORIA'} for f in range(5)]
        p = wb.plan(self.scan(dets), 360, 640)
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'s.mp4'; make_video(src, 6)
            import flash_screen
            with mock.patch.dict(os.environ, {'LYMPHLOOP_ALLOW_EXTERNAL_GEMINI': '1'}), \
                 mock.patch.object(flash_screen, 'call_flash', return_value={'result': {'icon_end': 'bottom', 'reads': 'bottom_to_top', 'text': 'LYMPHORIA'}}):
                p = wb.orient_tracks(p, src, td)
        self.assertEqual({st['orient'] for sts in p['stamps'].values() for st in sts}, {'v_up'})
        self.assertEqual(p['track_list'][0]['orient_source'], 'flash')

    def test_without_external_gemini_a_vertical_track_reads_downwards_and_its_crop_is_kept_for_the_agent(self):
        dets = [{'frame': f, 'bbox': [20, 100, 12, 70], 'text': 'LYMPHORIA'} for f in range(5)]
        p = wb.plan(self.scan(dets), 360, 640)
        native = {k: v for k, v in os.environ.items() if k not in ('LYMPHLOOP_ALLOW_EXTERNAL_GEMINI', 'LYMPHLOOP_NO_EXTERNAL_GEMINI')}
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'s.mp4'; make_video(src, 6)
            import flash_screen
            with mock.patch.dict(os.environ, native, clear=True), mock.patch.object(flash_screen, 'call_flash') as flash:
                p = wb.orient_tracks(p, src, td)
            track = p['track_list'][0]
            self.assertTrue(Path(track['agent_crop']).is_file())
        flash.assert_not_called()
        self.assertEqual({st['orient'] for sts in p['stamps'].values() for st in sts}, {'v_down'})
        self.assertEqual(track['orient_source'], 'agent_native_default')


class Stamp(unittest.TestCase):
    def test_white_lymphloop_fills_the_footprint_and_is_verified_in_the_encoded_file(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'m.mp4'; make_video(src, 12)
            plan = {'stamps': {str(f): [{'box': [18, 20, 14, 100], 'orient': 'v_down'}, {'box': [200, 600, 120, 16], 'orient': 'h'}] for f in range(3, 9)}, 'tracks': 2}
            out = Path(td)/'b.mp4'; rec = wb.stamp(src, plan, out, Path(td)/'r.json')
            self.assertTrue(rec['verification']['ok']); self.assertEqual(rec['stamped_frames'], 6)
            self.assertEqual(media.probe(str(out), include_frames=True)['frame_count'], 12)
            on, off = frame_rgb(out, 5), frame_rgb(out, 1)
            m, x, y = wb.stamp_mask([18, 20, 14, 100], 'v_down')
            self.assertGreater(m.shape[0], 85)                                  # fills the length of the footprint
            core = m > 0.8
            self.assertGreater((on[y:y + m.shape[0], x:x + m.shape[1]].mean(axis=2)[core] > 200).mean(), 0.8)
            self.assertLess(off[y:y + m.shape[0], x:x + m.shape[1]].mean(), 140)  # unstamped frame untouched

    def test_a_mark_under_the_stamp_is_inpainted_away_between_the_letters(self):
        font = ImageFont.truetype(wb._font_path(), 20)
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'m.mp4'; make_video(src, 4, draw=lambda i, im: ImageDraw.Draw(im).text((200, 600), 'LYMPHORIA', font=font, fill=(250, 250, 250)))
            plan = {'stamps': {'2': [{'box': [196, 598, 130, 26], 'orient': 'h'}]}}
            out = Path(td)/'b.mp4'; wb.stamp(src, plan, out, Path(td)/'r.json')
            m, x, y = wb.stamp_mask([196, 598, 130, 26], 'h'); reg = frame_rgb(out, 2)[y:y + m.shape[0], x:x + m.shape[1]].mean(axis=2)
            gaps = m < 0.05
            self.assertLess((reg[gaps] > 200).mean(), 0.1)          # old white letters do not show between the new ones

    def test_verification_catches_a_stamp_that_is_not_there(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'m.mp4'; make_video(src, 6)
            plan = {'stamps': {'2': [{'box': [200, 600, 120, 16], 'orient': 'h'}]}}
            self.assertFalse(wb.verify(src, plan)['ok'])


class FinalScreenNeedsBrand(unittest.TestCase):
    def test_a_watermarked_video_cannot_be_screened_unbranded(self):
        import swap
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'watermark').mkdir()
            (run/'watermark/scan.json').write_text(json.dumps({'detections': [{'frame': 1, 'classification': 'floating_overlay', 'bbox': [1, 1, 5, 5]}]}))
            state = {'stages': {'recut': {'master': {'path': '/out/master.mp4', 'sha256': 'aaa'}}}}
            with self.assertRaisesRegex(SystemExit, 'run swap.py brand'): swap.screened_master(state, run)
            state['stages']['brand'] = {'unbranded': {'sha256': 'aaa'}, 'master': {'path': '/out/master.mp4', 'sha256': 'bbb'}}
            self.assertEqual(str(swap.screened_master(state, run)), str(Path('/out/master.mp4')))
            (run/'watermark/scan.json').write_text(json.dumps({'detections': []})); del state['stages']['brand']
            self.assertEqual(str(swap.screened_master(state, run)), str(Path('/out/master.mp4')))

    def test_fade_pad_is_0_8_seconds_whatever_the_frame_rate(self):
        import swap
        self.assertEqual([swap.fade_pad_frames(f) for f in ('30', '25', '30000/1001', 60)], [24, 20, 24, 48])


if __name__ == '__main__':
    unittest.main()


class FixedPlaces(unittest.TestCase):
    """User 2026-11-20: the mark sits at the same few places all film long; LYMPHLOOP must not grow and shrink
    ("忽大忽小，很丑，所以我想要固定住它"); the checks read only those places."""
    def det(self, f, box, text='LYMPHORIA', **k):
        return {'frame': f, 'bbox': box, 'text': text, 'classification': 'floating_overlay', **k}

    def test_places_cluster_partial_reads_and_ignore_grown_vertical_boxes(self):
        import watermark_brand as wb
        wb.set_target('LYMPHORIA')
        d = [self.det(f, [331, 550, 16, 90]) for f in range(250, 260)]                       # full-frame, grown both ends
        d += [self.det(f, [334, 569, 10, 61], pass_='x') for f in range(250, 260)]
        for x in d[10:]: x['pass'] = x.pop('pass_')
        d += [self.det(f, [335, 581, 9, 43], **{'pass': 'right_cw'}) for f in range(4800, 4805)]   # partial read, later burst
        d += [self.det(f, [36, 99, 85, 14]) for f in range(400, 420)]
        d += [self.det(f, [36, 99, 85, 22], synthetic='fade_pad') for f in range(380, 400)]      # padding: not evidence
        places = wb.positions(d, 360, 640)
        self.assertEqual(len(places), 2)
        v = next(p for p in places if p['vertical'])
        self.assertEqual(v['box'], [334, 569, 10, 61]); self.assertEqual(v['bursts'], 2)
        self.assertEqual(next(p for p in places if not p['vertical'])['box'], [36, 99, 85, 14])

    def test_every_stamp_of_a_place_gets_the_same_box_and_one_direction(self):
        import watermark_brand as wb
        places = [{'id': 0, 'box': [334, 566, 13, 64], 'vertical': True}, {'id': 1, 'box': [53, 100, 64, 13], 'vertical': False}]
        plan = {'track_list': [{'id': 0, 'box': [331, 571, 16, 69]}, {'id': 1, 'box': [36, 95, 90, 23]}, {'id': 2, 'box': [200, 300, 60, 12]}],
                'stamps': {'1': [{'box': [331, 571, 16, 69], 'orient': 'v_down', 'track': 0}, {'box': [36, 95, 90, 23], 'orient': 'h', 'track': 1}],
                           '2': [{'box': [331, 581, 16, 43], 'orient': 'v_down', 'track': 0}],
                           '3': [{'box': [327, 567, 24, 77], 'orient': 'v_up', 'track': 0}],
                           '4': [{'box': [205, 301, 50, 11], 'orient': 'h', 'track': 2}]}}
        wb.fix_stamps(plan, places)
        boxes = {tuple(st['box']) for f in '123' for st in plan['stamps'][f] if st['position'] == 0}
        self.assertEqual(boxes, {(334, 566, 13, 64)})
        self.assertEqual({st['orient'] for f in '123' for st in plan['stamps'][f] if st['position'] == 0}, {'v_down'})
        self.assertEqual(plan['stamps']['1'][1]['box'], [53, 100, 64, 13])
        self.assertEqual(plan['stamps']['3'][0]['mark'], [327, 567, 24, 77])          # the frame's mark is still cleaned
        self.assertEqual(plan['stamps']['4'][0]['box'], [200, 300, 60, 12])           # no place: its track's one box

    def test_measured_footprint_and_shared_size(self):
        import watermark_brand as wb, media_pipeline as media, subprocess, tempfile, numpy as np
        rng = np.random.default_rng(1); W, H = 160, 200
        frames = []
        for k in range(12):
            im = np.full((H, W, 3), rng.integers(20, 120), np.uint8)
            cv = rng.integers(0, 255, (H // 20, W // 20, 3), dtype=np.uint8).repeat(20, 0).repeat(20, 1)  # moving blocks
            im = (0.5 * im + 0.5 * np.roll(cv, k * 7, axis=1)).astype(np.uint8)
            for j in range(8): im[40:50, 30 + j * 8:35 + j * 8] = 235      # a 8-letter horizontal line, 30..95 x 40..50
            for j in range(8): im[100 + j * 8:105 + j * 8, 140:150] = 235  # the same line, vertical, 140..150 x 100..165
            frames.append(im)
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'m.mp4'
            p = subprocess.Popen([media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', '30',
                                  '-i', '-', '-c:v', 'libx264', '-crf', '4', '-pix_fmt', 'yuv420p', str(src)], stdin=subprocess.PIPE)
            p.communicate(b''.join(f.tobytes() for f in frames))
            places = [{'id': 0, 'box': [14, 38, 85, 14], 'vertical': False}, {'id': 1, 'box': [139, 90, 12, 80], 'vertical': True}]
            with mock.patch.object(subprocess, 'Popen', wraps=subprocess.Popen) as processes:
                wb.measure_places(src, places, {0: set(range(12)), 1: set(range(12))})
            decodes = [c for c in processes.call_args_list
                       if '-vf' in c.args[0] and 'rawvideo' in c.args[0]]
            self.assertEqual(len(decodes), 1)  # both footprints share one actual source decode
        h, v = places[0], places[1]
        self.assertTrue(h['measured'] and v['measured'])
        self.assertLessEqual(abs(h['measured'][0] - 30), 3); self.assertLessEqual(abs(h['measured'][2] - 65), 4)
        self.assertEqual((h['box'][2], h['box'][3]), (v['box'][3], v['box'][2]))       # one size for both places
        self.assertEqual((h['size'], v['size']), ('shared', 'shared'))

    def test_slots_read_a_known_direction_once(self):
        import watermark_brand as wb
        s = wb.position_slots([{'id': 0, 'box': [334, 566, 13, 64], 'vertical': True, 'orient': 'v_down'},
                               {'id': 1, 'box': [6, 566, 13, 64], 'vertical': True},
                               {'id': 2, 'box': [53, 100, 64, 13], 'vertical': False}], 360, 640)
        looks = len(wb.SLOT_LOOKS)
        self.assertEqual([x[5] for x in s if x[0] == 0], [2] * looks)                   # top-to-bottom: turned ccw
        self.assertEqual(sorted({x[5] for x in s if x[0] == 1}), [1, 2])                # unknown: both ways
        self.assertEqual(len(s), looks * 4)


class PrintPlacesDropped(unittest.TestCase):
    """User 2026-11-21: product labels kept being taken for the watermark; places Flash calls product print are dropped."""
    def test_reads_at_print_places_become_labels(self):
        import watermark_brand as wb
        places = [{'id': 0, 'box': [35, 24, 26, 130], 'vertical': True, 'kind': 'floating_watermark'},
                  {'id': 1, 'box': [126, 285, 116, 31], 'vertical': False, 'kind': 'product_print'},
                  {'id': 2, 'box': [300, 600, 60, 12], 'vertical': False, 'kind': 'unsure'}]
        dets = [{'frame': 1, 'bbox': [35, 30, 26, 120], 'classification': 'floating_overlay'},
                {'frame': 2, 'bbox': [130, 287, 110, 28], 'classification': 'floating_overlay'},
                {'frame': 3, 'bbox': [302, 600, 58, 12], 'classification': 'floating_overlay'}]
        keep, n = wb.drop_print_places(dets, places)
        self.assertEqual([p['id'] for p in keep], [0, 2]); self.assertEqual(n, 1)
        self.assertEqual([d['classification'] for d in dets], ['floating_overlay', 'printed_label_keep', 'floating_overlay'])

    def test_moving_label_reads_go_with_their_print_place(self):
        # 2026-11-22 film 1141895738823753: a bottle label moves; its reads lie away from the place's measured box
        import watermark_brand as wb
        dets = [{'frame': f, 'bbox': [170 + 3 * f, 480 + 2 * f, 190, 56], 'text': 'LYMPHORIA', 'classification': 'floating_overlay'} for f in range(0, 40, 2)]
        dets += [{'frame': 100, 'bbox': [20, 30, 26, 120], 'text': '@LYMPHORIA', 'classification': 'floating_overlay'}]
        places = wb.positions(dets, 720, 1280)
        for q in places:
            q['kind'] = 'product_print' if q['box'][1] > 300 else 'floating_watermark'
            if q['kind'] == 'product_print': q['box'] = [300, 700, 60, 20]          # measured box far from most reads
        keep, n = wb.drop_print_places(dets, places)
        self.assertEqual(n, 20); self.assertEqual(dets[-1]['classification'], 'floating_overlay')
        self.assertTrue(all('reads' not in q for q in places))
