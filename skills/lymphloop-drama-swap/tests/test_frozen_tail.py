"""select cuts the still picture a generated clip ends on (2026-11-22, user: "生成段的最后都会被定格一下")."""
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import swap  # noqa: E402


class FrozenTail(unittest.TestCase):
    def run_trim(self, still, words, clip_out=9.0, seg_s=8.25):
        import numpy as np
        with mock.patch.object(swap.media, 'frozen_tail', return_value=still), \
             mock.patch('words.transcribe', return_value=words), mock.patch('words.load_model'), \
             mock.patch('words.pcm', return_value=np.full(16000, 0.1, np.float32)):          # a voice, about -20 dB
            return swap.trim_frozen_tail(Path('/x/G03-a1-c1.mp4'), 0.0, clip_out, seg_s)

    def test_silent_still_tail_is_cut_but_never_below_the_segment(self):
        self.assertEqual(self.run_trim(8.125, [])[0], 8.25)                 # 1585 G03: still from 8.12, segment 8.25
        self.assertEqual(self.run_trim(14.583, [], 15.0, 14.4333)[0], 14.583)   # 1603 G02

    def test_voice_over_a_still_picture_is_a_defect(self):
        out, rec = self.run_trim(8.208, [{'w': 'morning', 'a': 0.0, 'b': 0.2}, {'w': 'night.', 'a': 0.25, 'b': 0.48}], 9.0, 8.0417)
        self.assertAlmostEqual(out, 8.838, places=3); self.assertEqual(rec['talking'], 'morning night.')

    def test_last_syllable_over_a_few_still_frames_is_cut_after_it(self):
        out, rec = self.run_trim(3.625, [{'w': 'relation.', 'a': 0.0, 'b': 0.16}], 4.0, 2.0417)
        self.assertAlmostEqual(out, 3.935, places=3); self.assertNotIn('talking', rec)

    def test_no_still_tail_or_clip_within_the_segment_is_left_alone(self):
        self.assertEqual(self.run_trim(None, []), (9.0, None))
        self.assertEqual(self.run_trim(5.0, [], 8.25, 8.25), (8.25, None))


if __name__ == '__main__':
    unittest.main()


class RetrimRanges(unittest.TestCase):
    def test_redo_still_tails_inside_a_splice_are_found_and_floored(self):
        import json, tempfile
        import shot_regen as sr
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'generation/jobs').mkdir(parents=True); (run/'shots-regen').mkdir()
            redo = run/'redo.mp4'; redo.write_bytes(b'0'); base = run/'base.mp4'; base.write_bytes(b'0')
            (run/'shots-regen/gym_carton_r1.json').write_text(json.dumps({'real_s': 2.5}))
            (run/'generation/jobs/G03-a2.json').write_text(json.dumps({'provider': 'local_splice', 'recipe': {'pictures': [
                {'source': 'G03-gym_carton_r1-c1', 'path': str(redo), 'frames_inclusive': [0, 95]},
                {'source': 'G03-a1-c1', 'path': str(base), 'frames_inclusive': [83, 192]}]}}))
            (run/'generation/jobs/G03-a1.json').write_text(json.dumps({'provider': 'apimart'}))
            with mock.patch.object(sr.media, 'probe', return_value={'fps': '24'}), \
                 mock.patch.object(sr.media, 'frozen_tail', return_value=2.0), \
                 mock.patch.object(swap.media, 'frozen_tail', return_value=2.0), \
                 mock.patch('words.transcribe', return_value=[]), mock.patch('words.load_model'):
                talking = []
                # still from 2.0 s but the source shot is 2.5 s long: frames 60-95 go, 48-59 stay
                self.assertEqual(sr.still_ranges(run, 'G03-a2', talking), [(60, 95)])
                self.assertEqual(talking, [])


class DriftingStill(unittest.TestCase):
    """2026-11-23 film 2114 G03: a redo shot held 1.4 s with frame differences of 0.03-0.25 (the coach talked at
    0.3-2.4); freezedetect missed it because the drift crossed its tolerance, and the user saw a freeze at the join."""
    import media_pipeline as m

    # G03-gym_carton_r1-c1, frames 26-68 (24 fps): talking, then a drifting hold to the end
    TALK = [0.84, 0.72, 1.66, 0.32, 0.59, 0.58, 1.25]
    HOLD = [0.1, 0.1, 0.08, 0.25, 0.1, 0.07, 0.09, 0.24, 0.1, 0.07, 0.09, 0.24, 0.1, 0.08, 0.08, 0.21, 0.08, 0.07, 0.07,
            0.19, 0.07, 0.07, 0.07, 0.17, 0.07, 0.06, 0.06, 0.14, 0.05, 0.05, 0.05, 0.1, 0.04, 0.03, 0.04]

    def test_drifting_hold_starts_after_the_last_movement(self):
        diffs = [0.0] + [1.0] * 25 + self.TALK + self.HOLD
        self.assertEqual(self.m.still_start_index(diffs, 24), 1 + 25 + len(self.TALK))

    def test_quiet_talking_is_not_still(self):
        # G03 a3 frames 84-115: a talking shot with a few low differences
        talk = [0.45, 0.91, 0.78, 0.28, 0.22, 0.29, 0.78, 0.92, 0.26, 1.39, 2.29, 1.39, 0.26, 1.03, 2.38, 1.51]
        self.assertIsNone(self.m.still_start_index([0.0] + talk * 3, 24))

    def test_a_short_hold_is_not_a_frozen_tail(self):
        self.assertIsNone(self.m.still_start_index([0.0] + [1.0] * 40 + [0.05] * 4, 24))

    def test_earlier_of_freezedetect_and_differences(self):
        with mock.patch.object(self.m, 'probe', return_value={'video_duration': 4.0417, 'fps': '24'}), \
             mock.patch.object(self.m, '_freezedetect_tail', return_value=2.875), \
             mock.patch.object(self.m, 'frame_diffs', return_value=[0.0] + [1.0] * 32 + [0.05] * 63):
            self.assertAlmostEqual(self.m.frozen_tail('/x/G03-gym_carton_r1-c1.mp4'), 1.375, places=3)   # frame 32 is where the last movement lands
        with mock.patch.object(self.m, 'probe', return_value={'video_duration': 4.0417, 'fps': '24'}), \
             mock.patch.object(self.m, '_freezedetect_tail', return_value=3.541), \
             mock.patch.object(self.m, 'frame_diffs', return_value=[0.0] + [1.0] * 96):
            self.assertEqual(self.m.frozen_tail('/x/G10-a1-c1.mp4'), 3.541)


class RecutCutsStillFrames(unittest.TestCase):
    """User 2026-11-23: "回剪的时候把定格帧裁掉" - finish.py retrims every segment first and recut refuses still frames left."""

    def test_finish_runs_retrim_before_captions(self):
        import finish
        self.assertEqual(finish.STEPS[:2], ['retrim', 'captions'])
        a = mock.Mock(previous=None, previous_timeline=None, previous_label=None, full=False, user_approved_cut=None)
        cmd = finish.step_cmd('retrim', a, Path('/r'))
        self.assertEqual(cmd[-4:], ['retrim', '--run', str(Path('/r')), '--all'])

    def test_recut_names_the_segments_with_still_frames(self):
        import shot_regen as sr
        plan = {'G01': {'duration_s': 7.4}, 'G03': {'duration_s': 7.57}, 'G05': {'duration_s': 14.4}}
        sel = {'G01': {'job': 'G01-a1', 'clip_out': 7.4}, 'G03': {'job': 'G03-a3', 'clip_out': 10.54},
               'G05': {'job': 'G05-a1', 'clip_out': 15.0}}
        left = {'G01': None, 'G03': {'ranges': [(36, 68), (117, 155)]},
                'G05': {'tail': [14.4, {'still_from': 13.9}]}}
        with mock.patch.object(sr, 'pending_still', side_effect=lambda run, g, s, seg_s, t: left[g]):
            out = swap.still_left(Path('/r'), plan, sel)
        self.assertEqual(len(out), 2)
        self.assertIn('G03', out[0]); self.assertIn('(36, 68)', out[0]); self.assertIn('14.40', out[1])

    def test_nothing_to_cut_leaves_the_selection_alone(self):
        import shot_regen as sr
        a = mock.Mock(dry_run=False, lock_wait=0)
        with tempfile.TemporaryDirectory() as td, mock.patch.object(sr, 'pending_still', return_value=None), \
             mock.patch.object(sr.subprocess, 'run') as run_cmd:
            self.assertFalse(sr.retrim_one(a, Path(td), 'G01', {'job': 'G01-a1', 'candidate': 'c1', 'clip_out': 7.4}, 7.4))
            run_cmd.assert_not_called()

    def test_kept_still_end_and_source_picture_are_not_pending(self):
        import shot_regen as sr
        self.assertIsNone(sr.pending_still('/r', 'G01', {'job': 'G01-a1', 'keep_frozen_tail': True}, 7.4, []))
        self.assertIsNone(sr.pending_still('/r', 'G01', {'picture': 'source'}, 7.4, []))


class TranscriptionJitter(unittest.TestCase):
    """1409 G03 / 998 G05 (11-23): a few frames left after the last word are not a freeze for recut."""

    def test_two_leftover_frames_do_not_block_recut(self):
        import json
        import shot_regen as sr
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'generation/jobs').mkdir(parents=True)
            (run/'generation/jobs/G03-a8.json').write_text(json.dumps({'provider': 'local_splice', 'recipe': {'fps': '24', 'pictures': []}}))
            sel = {'job': 'G03-a8', 'clip_out': 13.333, 'media': {'path': str(run/'none.mp4')}}
            with mock.patch.object(sr, 'still_ranges', return_value=[(134, 135)]):
                self.assertIsNone(sr.pending_still(run, 'G03', sel, 12.84, []))
            with mock.patch.object(sr, 'still_ranges', return_value=[(120, 145)]):
                self.assertEqual(sr.pending_still(run, 'G03', sel, 12.84, []), {'ranges': [(120, 145)]})
