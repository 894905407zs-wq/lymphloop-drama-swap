"""Tests for Antigravity native multimodal (see + hear) mode, zero-external-Gemini
defaults, dialogue syllable alignment, highlight backplate detection, structured
6-item select --audit gate, shot_regen 1-frame tail clamp & shifted-cut audio fix,
and compare_board built-in Range HTTP server."""
import argparse
import contextlib
import http.client
import io
import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))

import agent_native as an  # noqa: E402
import compare_board as cb  # noqa: E402
import google_api as api  # noqa: E402
import media_pipeline as media  # noqa: E402
import scenes  # noqa: E402
import shot_regen as sr  # noqa: E402
import speech_map  # noqa: E402
import swap  # noqa: E402
import watermark_brand as wb  # noqa: E402


def _make_clip(path: Path, frames=10, w=360, h=640, fps=24, draw_yellow_pill=False):
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    vw = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    for i in range(frames):
        img = np.full((h, w, 3), (45, 50, 55), dtype=np.uint8)
        if draw_yellow_pill and i in (2, 3, 4):
            # Bright yellow caption highlight box in y=0.68..0.74
            y0, y1 = int(h * 0.68), int(h * 0.74)
            x0, x1 = int(w * 0.25), int(w * 0.75)
            # BGR yellow: B=10, G=235, R=250
            img[y0:y1, x0:x1] = (10, 235, 250)
        vw.write(img)
    vw.release()


def _make_av_clip(path: Path, dur=4.0, fps=24):
    media.ff([
        '-y',
        '-f', 'lavfi', '-i', f'color=c=navy:s=360x640:r={fps}:d={dur}',
        '-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:duration={dur}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(path),
    ])


class NativeModeAndZeroGemini(unittest.TestCase):
    def test_is_native_defaults_and_env_overrides(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LYMPHLOOP_ALLOW_EXTERNAL_GEMINI', None)
            os.environ.pop('LYMPHLOOP_NO_EXTERNAL_GEMINI', None)
            self.assertTrue(an.is_native())
            with self.assertRaises(api.AdapterError):
                api.required_key()
        with patch.dict(os.environ, {'LYMPHLOOP_ALLOW_EXTERNAL_GEMINI': '1'}, clear=False):
            self.assertFalse(an.is_native())
        with patch.dict(os.environ, {'LYMPHLOOP_NO_EXTERNAL_GEMINI': '0'}, clear=False):
            self.assertFalse(an.is_native())

    def test_reference_residue_skips_flash_in_native_mode(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(swap.reference_residue(Path(td), Path(td) / 'x.mp4'), [])

    def test_flash_screen_and_swap_design_observe_zero_gemini(self):
        import flash_screen
        import swap_design
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            with self.assertRaises(api.AdapterError):
                flash_screen.call_flash([('text', 'hi')], {}, r)
            # swap_design observe --import-obs writes obs JSON without calling Flash
            (r / 'shots').mkdir(); (r / 'plan').mkdir()
            (r / 'shots/shots.json').write_text(json.dumps({'fps': 30, 'source': str(r / 's.mp4'), 'shots': [{'id': 'S001', 'in_frame': 0, 'out_frame': 30}]}))
            (r / 'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': [{'id': 'G01', 'in_frame': 0, 'out_frame': 30, 'shot_ids': ['S001'], 'shot_times': [['S001', 0.0, 1.0]]}]}))
            obs_file = r / 'obs.json'
            obs_file.write_text(json.dumps({'S001': {'visible': True, 'kind': 'bottle', 'where': 'in_hand', 'size': 'medium', 'action': 'hold_show', 'main_subject': True, 'speaking': False}}))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                swap_design.cmd_observe(argparse.Namespace(run=str(r), shots=None, import_obs=str(obs_file)))
            res = json.loads(buf.getvalue())
            self.assertEqual(res['mode'], 'antigravity_native')
            self.assertTrue((r / 'design/obs/S001.json').is_file())

    def test_semantic_breaks_local_heuristic_zero_gemini(self):
        with tempfile.TemporaryDirectory() as td:
            sents = [
                {'start': 0.0, 'end': 1.5, 'text': 'Wait, what is this?'},
                {'start': 1.7, 'end': 3.2, 'text': 'It is a liquid packet.'},
                {'start': 4.8, 'end': 6.5, 'text': 'Now look at the box.'},
            ]
            res = speech_map.semantic_breaks(Path(td), sents, [0, 1])
            self.assertEqual(set(res.keys()), {0, 1})
            # After '?' should NOT split
            self.assertFalse(res[0]['good'])
            # After '.' with 1.6s pause SHOULD allow split
            self.assertTrue(res[1]['good'])

    def test_scenes_classify_cuts_local_zero_gemini(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            strips = td / 'plan' / 'scene_strips'
            strips.mkdir(parents=True)
            Image.new('RGB', (120, 60), (20, 180, 40)).save(strips / 'S001.jpg')
            Image.new('RGB', (120, 60), (25, 175, 42)).save(strips / 'S002.jpg')
            Image.new('RGB', (120, 60), (220, 30, 30)).save(strips / 'S003.jpg')
            shots = [
                {'id': 'S001', 'start_s': 0.0, 'end_s': 2.0, 'in_frame': 0, 'out_frame': 60},
                {'id': 'S002', 'start_s': 2.0, 'end_s': 4.0, 'in_frame': 60, 'out_frame': 120},
                {'id': 'S003', 'start_s': 4.0, 'end_s': 6.0, 'in_frame': 120, 'out_frame': 180},
            ]
            out = scenes.classify_cuts(td, td / 'dummy.mp4', shots, 30.0, {2: 'audio dip'})
            self.assertIn(out[1]['level'], ('none', 'shot', 'paragraph'))
            self.assertEqual(out[2]['level'], 'paragraph')

    def test_watermark_native_helpers_skip_flash(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            vid = td / 'v.mp4'
            _make_av_clip(vid, dur=1.0, fps=24)
            plan_rec = {
                'track_list': [{'id': 0, 'vertical': True, 'box': [10, 10, 20, 120], 'mid': 5,
                                'frames': 4, 'orient': 'v_down', 'orient_source': 'default'}],
                'stamps': {'5': [{'track': 0, 'orient': 'v_down'}]},
            }
            places = [{'id': 0, 'box': [10, 10, 20, 120], 'vertical': True, 'orient': 'v_down', 'first': 2}]
            res = wb.orient_tracks(plan_rec, vid, td, places=places)
            self.assertEqual(res['track_list'][0]['orient'], 'v_down')
            out_places = wb.classify_places(places, vid, [], td)
            self.assertEqual(out_places[0]['kind'], 'unsure')
            self.assertIn('agent_native', out_places[0]['kind_reason'])


class PauseSplittingAndSyllables(unittest.TestCase):
    def test_split_words_by_pause_breaks_long_silences(self):
        words = [
            {'start': 0.0, 'end': 0.4, 'word': 'Hello'},
            {'start': 0.45, 'end': 0.8, 'word': 'there'},
            {'start': 2.5, 'end': 2.9, 'word': 'Try'},
            {'start': 3.0, 'end': 3.4, 'word': 'this.'},
            {'start': 3.5, 'end': 3.9, 'word': 'Next'},
        ]
        sents = an.split_words_by_pause(words, offset_s=10.0, gap_s=0.7)
        self.assertEqual([s['text'] for s in sents], ['Hello there', 'Try this.', 'Next'])
        self.assertAlmostEqual(sents[0]['start'], 10.0, places=2)
        self.assertAlmostEqual(sents[1]['start'], 12.5, places=2)

    def test_dialogue_syllable_alignment_check(self):
        # 4 syllables (Lymphoria) -> 2 syllables (LymphLoop): warn & suggest Yesnap LymphLoop
        r_bad = an.check_dialogue_syllables("It's called Lymphoria.", "It's called LymphLoop.")
        self.assertIsNotNone(r_bad['warning'])
        self.assertIn('Yesnap LymphLoop', r_bad['warning'])

        # 4 syllables -> 4 syllables (Yesnap LymphLoop): clean
        r_good = an.check_dialogue_syllables("It's called Lymphoria.", "It's called Yesnap LymphLoop.")
        self.assertIsNone(r_good['warning'])
        self.assertEqual(r_good['diff'], 0)

        # 4 syllables (Stillingia) -> 4 syllables (Dandelion): clean
        r_herb = an.check_dialogue_syllables('With Stillingia root.', 'With Dandelion root.')
        self.assertIsNone(r_herb['warning'])


class HighlightScanAndGrids(unittest.TestCase):
    def test_scan_highlight_backplates_detects_yellow_pill(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dirty = td / 'dirty.mp4'
            clean = td / 'clean.mp4'
            _make_clip(dirty, frames=8, draw_yellow_pill=True)
            _make_clip(clean, frames=8, draw_yellow_pill=False)
            r_dirty = an.scan_highlight_backplates(dirty)
            r_clean = an.scan_highlight_backplates(clean)
            self.assertEqual(r_dirty['frames'], 3)
            self.assertEqual(r_dirty['sample_frames'], [2, 3, 4])
            self.assertIsNotNone(r_dirty['y_min'])
            self.assertEqual(r_clean['frames'], 0)

    def test_tile_and_stack_sheets(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            imgs = []
            for i in range(5):
                p = td / f'S{i:03d}.png'
                Image.new('RGB', (400, 120), (40 * i, 100, 180)).save(p)
                imgs.append(p)
            grids = an.tile_sheets(imgs, td / 'out', prefix='g', cols=2, per_grid=4)
            self.assertEqual(len(grids), 2)
            self.assertTrue(all(Path(g).is_file() for g in grids))
            stacked = an.stack_flashback_sheets(imgs, td / 'fb', prefix='fb', per_grid=3)
            self.assertEqual(len(stacked), 2)
            self.assertTrue(all(Path(s).is_file() for s in stacked))


class AuditGateAndNativeImports(unittest.TestCase):
    def test_parse_audit_and_select_enforcement(self):
        parsed = an.parse_audit('competitor_clean=1,front_face_ok=0,scale_ok=true')
        self.assertEqual(an.audit_failures(parsed), ['front_face_ok'])

        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            for d in ('plan', 'generation/jobs', 'generation/results', 'qa'):
                (r / d).mkdir(parents=True)
            vid = r / 'generation/results/G01-a1-c1.mp4'
            _make_av_clip(vid, dur=4.0, fps=24)
            (r / 'state.json').write_text(json.dumps({'source': str(vid), 'stages': {}}))
            (r / 'plan/segments.json').write_text(json.dumps({
                'fps': 24,
                'segments': [{'id': 'G01', 'in_frame': 0, 'out_frame': 96, 'duration_s': 4.0,
                              'kinds': ['visual'], 'speech': [], 'rewrites': {'dialogue': [], 'vo': []}}],
            }))
            (r / 'generation/jobs/G01-a1.json').write_text(json.dumps({
                'job_id': 'G01-a1', 'candidates': {'c1': {'output_path': str(vid)}},
            }))

            base_ns = dict(run=str(r), job='G01-a1', candidate='c1', clip_in=None, clip_out=None,
                           note='ok', keep_source_picture=False, audio_only=False, no_caption_clean=True,
                           keep_frozen_tail=True, sound=None, no_mouth=False, allow_audit_fail=False)
            # Failed audit must refuse selection
            with self.assertRaises(SystemExit) as cm:
                swap.cmd_select(argparse.Namespace(**base_ns, audit='competitor_clean=1,front_face_ok=0'))
            self.assertIn('front_face_ok', str(cm.exception))

            # Passing audit stores structured audit inside selections.json
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                swap.cmd_select(argparse.Namespace(
                    **base_ns,
                    audit='competitor_clean=1,front_face_ok=1,scale_ok=1,physics_ok=1,speech_lip_ok=1,text_clean=1',
                ))
            sel = json.loads((r / 'plan/selections.json').read_text())['G01']
            self.assertTrue(sel['audit']['front_face_ok'])
            self.assertTrue(sel['audit']['speech_lip_ok'])

    def test_native_import_verdicts_and_nodes(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            for d in ('shots', 'detect'):
                (r / d).mkdir(parents=True)
            vid = r / 'master.mp4'
            _make_av_clip(vid, dur=2.0, fps=24)
            (r / 'state.json').write_text(json.dumps({
                'source': str(vid),
                'probe': {'frame_count': 48, 'fps': '24'},
                'stages': {'prepare': {
                    'cfr_master': {'path': str(vid), 'sha256': api.file_sha(vid)},
                    'working_master': {'path': str(vid), 'sha256': api.file_sha(vid)},
                }},
            }))
            (r / 'detect/competitor.json').write_text(json.dumps({
                'reviewed': True, 'brand_names': ['Lymphoria'], 'form': 'drops', 'container_look': 'amber bottle',
            }))
            (r / 'shots/shots.json').write_text(json.dumps({
                'shots': [
                    {'id': 'S001', 'in_frame': 0, 'out_frame': 24, 'start_s': 0.0, 'end_s': 1.0, 'duration_s': 1.0},
                    {'id': 'S002', 'in_frame': 24, 'out_frame': 48, 'start_s': 1.0, 'end_s': 2.0, 'duration_s': 1.0},
                ],
            }))
            vf = r / 'verdicts.json'
            vf.write_text(json.dumps({'replace_ids': ['S002'], 'unsure_ids': [], 'notes': {'S002': 'bottle'}}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                swap.cmd_screen(argparse.Namespace(run=str(r), ids=None, import_verdicts=str(vf)))
            res = json.loads(out.getvalue())
            self.assertEqual(res['replace_ids'], ['S002'])
            self.assertTrue((r / 'screen/S002.json').is_file())

            nf = r / 'nodes.json'
            nf.write_text(json.dumps({'nodes': [{'in_frame': 24, 'out_frame': 48, 'scene': 'kitchen', 'visual': 'bottle', 'vo': None, 'dialogue': None}]}))
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                swap.cmd_detect_pro(argparse.Namespace(run=str(r), fps=5.0, wait=60, import_nodes=str(nf)))
            res2 = json.loads(out2.getvalue())
            self.assertEqual(res2['nodes'], 1)
            self.assertTrue((r / 'detect/pro_nodes.json').is_file())


class ShotRegenBugFixes(unittest.TestCase):
    def test_drop_cli_accepts_repeated_and_comma_flags(self):
        # Verify argparse in shot_regen handles --drop A --drop B and --drop C,D without overwriting
        p = argparse.ArgumentParser()
        p.add_argument('--drop', action='extend', nargs='+')
        ns = p.parse_args(['--drop', 'A', '--drop', 'B', '--drop', 'C,D'])
        flat = []
        for item in ns.drop:
            flat.extend(x.strip() for x in str(item).split(',') if x.strip())
        self.assertEqual(set(flat), {'A', 'B', 'C', 'D'})

    def test_round_splice_clamps_1frame_tail_flash(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            (r / 'plan').mkdir()
            (r / 'shots-regen/rounds').mkdir(parents=True)
            (r / 'plan/selections.json').write_text(json.dumps({
                'G01': {'job': 'G01-a1', 'candidate': 'c1', 'clip_in': 0.0, 'clip_out': 7.4667},
            }))
            rec = {
                'round': 1, 'status': 'open', 'items': [{
                    'name': 'S100_competitor_r1', 'seg': 'G01', 'kind': 'competitor', 'note': 'old bottle',
                    'base': 'G01-a1-c1', 'base_frames': [120, 178], 'seg_end': 7.4667,
                    'mode': 'cards', 'sound': 'none', 'state': 'generated',
                }],
            }
            (r / 'shots-regen/rounds/round-1.json').write_text(json.dumps(rec))
            # Fake cmd_splice returning 195 frames for the redone shot end, with padded old tail appended after 195
            fake_res = {
                'delta_s': (195 + 13 - 192) / 24.0,  # +0.6667s -> seg_end + delta_s = 8.1334s (> 195/24 = 8.125s!)
                'output_frames': 208,
                'base_fps': 24.0,
                'last_shot_b1': 178,
                'last_shot_end_frame': 195,
            }
            captured = {}

            def fake_run(cmd, **kw):
                captured['cmd'] = cmd
                return argparse.Namespace(returncode=0, stdout='{}', stderr='')

            with patch.object(sr, 'cmd_splice', return_value=fake_res), \
                    patch.object(sr.subprocess, 'run', side_effect=fake_run):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    sr.cmd_round_splice(argparse.Namespace(
                        run=str(r), round=1, drop=None, resplice=None,
                        partial=False, no_select=False, dry_run=False, finish=False,
                    ))
            idx = captured['cmd'].index('--clip-out')
            clip_out_val = float(captured['cmd'][idx + 1])
            # Must be clamped to 195 / 24.0 = 8.1250, NOT 8.1333 (which would flash 1 frame of old tail)
            self.assertAlmostEqual(clip_out_val, 8.125, places=4)

    def test_shifted_cut_switches_source_sound_to_first_generation(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            for d in ('shots-regen/rounds', 'generation/jobs', 'plan'):
                (r / d).mkdir(parents=True)
            fake_info = {
                'base': 'G07-a1-c1', 'base_frames': [60, 110], 'ref_frames': [40, 90],
                'mirrored': False, 'sound': 'source', 'real_s': 2.0, 'seg_end': 6.0,
                'shots': ['S171'], 'lines_in_shot': [{'text': 'Right now they are running.'}],
                'rewrites_in_shot': [], 'short': True,
                'timing_warning': '生成片的切点对不上原片切点（差 0.88 秒）',
            }

            def fake_ref(ra):
                p = r / 'shots-regen' / f'{ra.name}.json'
                p.write_text(json.dumps({'name': ra.name, 'segment': ra.seg, 'real_s': 2.0, 'pieces': [[40, 90]], 'ref_fps': 30.0}))

            with patch.object(sr, 'locate', return_value=fake_info), \
                    patch.object(sr, 'cmd_ref', side_effect=fake_ref), \
                    patch.object(sr, 'card_stub', return_value='# cards: A\n【写】'), \
                    patch.object(sr, 'redo_problem', return_value=None):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    sr.cmd_round_open(argparse.Namespace(
                        run=str(r), file=None, round=1, mode='cards',
                        goof=['G07@3.0:competitor:old bottle'],
                    ))
            data = json.loads(out.getvalue())
            self.assertEqual(data['items'][0]['sound'], 'first_generation')


class CompareBoardRangeServer(unittest.TestCase):
    def test_range_handler_serves_200_206_and_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            bdir = Path(td) / 'board'
            bdir.mkdir()
            (bdir / 'index.html').write_text('<h1>board</h1>', encoding='utf-8')
            (bdir / 'clip.mp4').write_bytes(b'0123456789ABCDEF')
            handler = cb._make_range_handler({'/': bdir})

            class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
                allow_reuse_address = True
                daemon_threads = True

            srv = Server(('127.0.0.1', 0), handler)
            port = srv.server_address[1]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                c.request('GET', '/index.html')
                r1 = c.getresponse()
                self.assertEqual(r1.status, 200)
                self.assertIn(b'board', r1.read())

                c.request('GET', '/clip.mp4', headers={'Range': 'bytes=4-9'})
                r2 = c.getresponse()
                self.assertEqual(r2.status, 206)
                self.assertEqual(r2.getheader('Content-Range'), 'bytes 4-9/16')
                self.assertEqual(r2.read(), b'456789')

                c.request('GET', '/../secret.txt')
                r3 = c.getresponse()
                self.assertIn(r3.status, (403, 404))
                r3.read()
                c.close()
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == '__main__':
    unittest.main()
