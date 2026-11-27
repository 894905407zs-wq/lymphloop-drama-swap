import sys
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import speech_map as sm  # noqa: E402
import swap  # noqa: E402


def spans(bounds): return list(zip(bounds[:-1], bounds[1:]))
def S(a, b, t='x'): return {'start': a, 'end': b, 'text': t}


class BeatSegmentation(unittest.TestCase):
    # v025 S061–S076: the user's own reading of where the three segments should be
    BOUNDS = [157.21, 159.04, 160.96, 163.12, 165.42, 166.38, 168.29, 170.29, 172.00, 174.25, 175.92, 179.38, 181.12, 183.29, 185.21, 187.04, 189.42]
    SENT = [S(159.48, 160.22, 'Drops?'), S(160.22, 161.75), S(161.88, 163.35), S(163.35, 165.39), S(166.63, 171.39, 'Four herbs...'),
            S(171.49, 174.20, 'They get the drain moving...'), S(174.20, 176.00), S(179.30, 180.80, 'One full dropper a day,'), S(180.80, 183.20), S(183.20, 185.00)]

    def test_v025_cuts_at_the_two_beats_not_at_15_seconds(self):
        groups, notes = sm.segment_run(spans(self.BOUNDS), self.SENT)
        self.assertEqual(groups, [(0, 5), (5, 11), (11, 16)])
        self.assertEqual([round(n['start'], 2) for n in notes], [157.21, 166.38, 179.38])
        self.assertEqual(notes[1]['sentences'][0]['text'], 'Four herbs...')
        self.assertEqual(notes[2]['sentences'][0]['text'], 'One full dropper a day,')
        self.assertTrue(all(n['duration'] <= 15 for n in notes))

    def test_never_cuts_a_sentence_even_when_a_cut_would_fit_15s(self):
        # one long sentence over the only boundaries near 15 s: the cut must move to the pause
        b = [0, 5, 10, 14, 18, 22]
        sent = [S(1, 9), S(11, 17), S(19, 21)]
        groups, notes = sm.segment_run(spans(b), sent)
        for (i, j), n in zip(groups, notes):
            if i: self.assertFalse(n['cut_in']['crosses_speech'])

    def test_silent_product_shot_stays_with_the_line_before(self):
        b = [0, 4, 6, 8, 12]          # line, product shot (4–6), product shot (6–8), next line
        sent = [S(0.5, 3.8, 'line one'), S(8.2, 11.5, 'line two')]
        groups, _ = sm.segment_run(spans(b), sent)
        self.assertEqual(groups, [(0, 3), (3, 4)])

    def test_short_beats_merge_into_a_neighbour(self):
        b = [0, 1.5, 6, 10]
        sent = [S(0.2, 1.0, 'Hi.'), S(2.5, 5.5), S(6.5, 9.5)]
        groups, notes = sm.segment_run(spans(b), sent)
        self.assertTrue(all(n['duration'] >= sm.MIN_SEGMENT or len(groups) == 1 for n in notes))

    def test_no_speech_packs_under_the_ceiling(self):
        b = [0, 4, 8, 12, 16, 20, 24]
        groups, notes = sm.segment_run(spans(b), [])
        self.assertTrue(all(n['duration'] <= 15 for n in notes))
        self.assertEqual(len(groups), 2)

    def test_unavoidable_crossing_is_flagged(self):
        b = [0, 8, 16, 24]
        sent = [S(1, 23, 'one very long monologue')]
        _, notes = sm.segment_run(spans(b), sent)
        self.assertTrue(any('warning' in n for n in notes))

    def test_forced_split_prefers_the_semantic_break(self):
        # 22 s of continuous talk, two sentence-safe boundaries: a longer pause (A) and a semantic break (B)
        b = [0, 6, 9, 13, 22]
        sent = [S(0.2, 5.9, 'q'), S(6.3, 8.8, 'a'), S(9.1, 12.9, 'topic two'), S(13.1, 21.8, 'more')]
        g0, n0 = sm.segment_run(spans(b), sent)
        self.assertTrue(any(n.get('forced_split') for n in n0))
        g1, n1 = sm.segment_run(spans(b), sent, preferred={2})
        self.assertIn(2, [i for i, _ in g1])
        self.assertTrue(all(n['duration'] <= 15 for n in n1))

    def test_word_check_flags_blends_and_leftovers(self):
        rew = [{'original': 'Drops?', 'rewrite': 'Packets?'}, {'original': 'One full dropper a day', 'rewrite': 'One full packet a day'}]
        ok = sm.word_check([S(0, 1, 'Packets? That\'s how you did it?'), S(1, 2, 'One full packet a day,')], rew)
        self.assertEqual((ok['missing'], ok['leftover']), ([], []))
        bad = sm.word_check([S(0, 1, 'Drop kits? That\'s how you did it?'), S(1, 2, 'One full dropper a day,')], rew)
        self.assertIn('packets', bad['missing']); self.assertIn('dropper', bad['leftover'])


class LongShotAndDurations(unittest.TestCase):
    """User rules 2026-10-23: reference < 15 s; a single shot over the ceiling is cut at a sentence pause and its
    breath trimmed in recut; Wan output is whole seconds ≥ 2 and trimmed back to the segment."""
    def test_long_single_shot_is_cut_at_the_longest_usable_pause(self):
        import speech_map as sm
        spans = [(0.0, 22.0)]
        sentences = [{'start': 0.5, 'end': 6.0, 'text': 'a'}, {'start': 6.3, 'end': 9.0, 'text': 'b'},
                     {'start': 10.2, 'end': 15.0, 'text': 'c'}, {'start': 15.2, 'end': 21.5, 'text': 'd'}]
        pieces, origin, cuts = sm.split_long_shots(spans, sentences, 14.9)
        self.assertEqual(len(pieces), 2); self.assertEqual(origin, [0, 0])
        self.assertAlmostEqual(pieces[0][1], 9.6, places=3)              # midpoint of the 9.0–10.2 pause
        self.assertEqual(cuts, {1: {'kind': 'in_shot_pause', 'pause': [9.0, 10.2]}})
        self.assertTrue(all(b - a < 15 for a, b in pieces))

    def test_short_shots_are_not_split(self):
        import speech_map as sm
        pieces, origin, cuts = sm.split_long_shots([(0, 5), (5, 12)], [], 14.9)
        self.assertEqual(pieces, [(0, 5), (5, 12)]); self.assertEqual(cuts, {})

    def test_silent_long_shot_is_cut_evenly_and_flagged(self):
        import speech_map as sm
        pieces, _, cuts = sm.split_long_shots([(0.0, 20.0)], [], 14.9)
        self.assertEqual(pieces, [(0.0, 10.0), (10.0, 20.0)]); self.assertEqual(cuts[1]['kind'], 'in_shot_even')

    def test_segment_run_prefers_a_real_hard_cut_over_an_in_shot_cut(self):
        import speech_map as sm
        # 0–12 s is one shot pre-split at 6 s (virtual boundary 1); 12 s is a real hard cut (boundary 2).
        groups, notes = sm.segment_run([(0, 6), (6, 12), (12, 20)], [], max_s=14.9, virtual={1})
        self.assertEqual(groups, [(0, 2), (2, 3)])
        self.assertNotIn('in_shot', notes[1]['cut_in'])

    def test_segment_run_uses_the_in_shot_cut_only_when_forced(self):
        import speech_map as sm
        # a 20 s shot split at 10 s, then a 4 s shot: the only legal way under 15 s is the in-shot cut.
        groups, notes = sm.segment_run([(0, 10), (10, 20), (20, 24)], [], max_s=14.9, virtual={1})
        self.assertEqual(groups, [(0, 1), (1, 3)])
        self.assertTrue(notes[1]['cut_in']['in_shot'])
        self.assertTrue(all(n['duration'] < 15 for n in notes))

    def test_output_seconds_round_up_to_whole_seconds_with_the_api_minimum(self):
        self.assertEqual(swap.output_seconds(1.54), 4); self.assertEqual(swap.output_seconds(0.6), 4)        # API minimum
        self.assertEqual(swap.output_seconds(6.12), 7); self.assertEqual(swap.output_seconds(14.79), 15)     # user 2026-11-11: decimals round UP
        self.assertEqual(swap.output_seconds(7.0), 7); self.assertEqual(swap.output_seconds(11.8333), 12)
        self.assertIs(type(swap.output_seconds(7.0)), int)

    def test_reference_ceiling_is_strictly_under_15(self):
        import speech_map as sm
        self.assertLess(swap.SEGMENT_MAX, 15); self.assertLess(sm.MAX_SEGMENT, 15)


class LockPlanning(unittest.TestCase):
    """The long-paragraph test clip (2026-10-24): 22 shots; Flash labelled florist->kitchen before S004 and
    kitchen->salon before S020 as paragraph locks, the WEEK 1 time jump before S019 as a shot lock."""
    BOUNDS = [0.0, 1.53, 3.53, 7.0, 8.73, 10.87, 12.87, 14.93, 17.3, 18.93, 20.77, 22.83, 24.8, 26.9, 28.63, 30.93, 32.6, 35.07,
              37.03, 38.77, 41.17, 43.4, 45.23]
    PRODUCT = {'S003', 'S004', 'S005', 'S006', 'S007', 'S009', 'S010', 'S013', 'S016', 'S017', 'S018', 'S019'}
    KITCHEN = [{'start': 11.1, 'end': 11.8, 'text': "What's that?"}, {'start': 12.3, 'end': 14.9, 'text': 'Herbs. From Dawn Kowalski.'},
               {'start': 15.4, 'end': 17.2, 'text': "Kel, about Gino's,"}, {'start': 17.4, 'end': 19.1, 'text': 'I was showing off for the guys.'},
               {'start': 19.2, 'end': 20.0, 'text': "I'm sorry."}, {'start': 20.6, 'end': 24.4, 'text': "I'm not hiding behind my name tag, Scotty."},
               {'start': 24.6, 'end': 26.0, 'text': 'Give me six weeks.'}, {'start': 29.4, 'end': 30.3, 'text': 'Six weeks?'},
               {'start': 30.9, 'end': 31.9, 'text': 'Homecoming.'}]

    def spans(self, a, b): return list(zip(self.BOUNDS[a:b], self.BOUNDS[a + 1:b + 1]))
    def product(self, a, b): return [f'S{k + 1:03d}' in self.PRODUCT for k in range(a, b)]

    def test_blocks_run_from_lock_to_lock(self):
        florist = sm.lock_blocks(self.spans(0, 3), self.product(0, 3), {}, [{'start': 2.2, 'end': 3.2}, {'start': 3.48, 'end': 5.76}])
        self.assertEqual([(i, j) for i, j, _ in florist], [(0, 3)])                 # S001–S003: explored back to the paragraph start
        kitchen = sm.lock_blocks(self.spans(3, 19), self.product(3, 19), {15: 'shot'}, self.KITCHEN)
        self.assertEqual([(3 + i, 3 + j) for i, j, _ in kitchen], [(3, 18), (18, 19)])  # S004–S018, then S019 behind the shot lock
        salon = sm.lock_blocks(self.spans(19, 22), self.product(19, 22), {}, [])
        self.assertEqual(salon, [])                                                  # no product: not generated

    def test_kitchen_block_is_cut_at_the_topic_change_and_the_beat(self):
        groups, notes = sm.segment_run(self.spans(3, 18), self.KITCHEN, max_s=14.9, preferred={7 - 3})
        self.assertEqual([round(n['start'], 2) for n in notes], [7.0, 14.93, 28.63]); self.assertTrue(all(n['duration'] < 15 for n in notes))

    def test_a_line_across_a_shot_lock_merges_the_blocks_but_never_across_a_paragraph_lock(self):
        spans = [(0, 3), (3, 6), (6, 9)]; line = [{'start': 2.0, 'end': 4.5}]
        self.assertEqual([(i, j) for i, j, _ in sm.lock_blocks(spans, [True, True, True], {1: 'shot'}, line)], [(0, 3)])
        out = sm.lock_blocks(spans, [True, True, True], {1: 'paragraph'}, line)
        self.assertEqual([(i, j) for i, j, _ in out], [(0, 1), (1, 3)]); self.assertTrue(out[1][2])


class SemanticAndQuiet(unittest.TestCase):
    def test_a_pause_flash_calls_mid_exchange_is_no_boundary_and_the_last_cut_in_a_pause_wins(self):
        spans = list(zip(LockPlanning.BOUNDS[3:18], LockPlanning.BOUNDS[4:19]))
        sents = LockPlanning.KITCHEN
        with unittest.mock.patch.object(sm, 'semantic_breaks', side_effect=lambda run, s, lines: {k: {'good': k == 1, 'reason': ''} for k in lines}):
            groups, notes = sm.plan_run('/tmp', spans, sents, max_s=14.9)
        self.assertEqual([round(n['start'], 2) for n in notes], [7.0, 14.93, 28.63])   # 28.63, not 26.90: same pause, last cut

    def test_quiet_cut_forgives_a_transcript_edge_but_not_a_cut_deep_in_a_sentence(self):
        line = [{'start': 166.12, 'end': 171.0}]
        self.assertTrue(sm._crossing(166.38, line, 0.25))
        self.assertFalse(sm._crossing(166.38, line, 0.25, quiet={166.38}))      # "Four" 0.26 s early in the transcript
        self.assertTrue(sm._crossing(168.5, line, 0.25, quiet={168.5}))         # a quiet dip mid-sentence is still speech

    def test_long_ranges_are_transcribed_in_chunks_that_end_on_cuts(self):
        calls = []
        def fake(run, master, a, b, margin=0.4):
            calls.append((round(a, 2), round(b, 2))); return [{'start': a + 1, 'end': a + 2, 'text': f'{a}'}]
        with unittest.mock.patch.object(sm, 'transcribe', side_effect=fake):
            out = sm.transcribe_chunked('/tmp', 'm', 0.0, 70.0, cuts=[10, 20, 28, 41, 55, 66])
        self.assertEqual(calls, [(0.0, 28.0), (28.0, 55.0), (55.0, 70.0)])
        self.assertEqual(len(out), 3)


class SceneSignals(unittest.TestCase):
    def test_audio_break_needs_near_digital_silence(self):
        import numpy as np, scenes
        rms = np.full(400, 200.0)
        rms[98:102] = 3.0      # 5.0 s: the bed is cut
        rms[198:202] = 50.0    # 10.0 s: a pause inside dialogue (25 % of the clip level)
        self.assertEqual(list(scenes.audio_breaks(rms, [5.0, 10.0, 15.0])), [5.0])

    def test_flash_verdicts_become_lock_levels_and_overrides_win(self):
        import json, tempfile, scenes
        from pathlib import Path
        self.assertEqual([scenes.level_of(False, True), scenes.level_of(True, False), scenes.level_of(True, True)], ['paragraph', 'shot', 'none'])
        shots = [{'id': f'S00{k}', 'in_frame': k * 30, 'out_frame': k * 30 + 30} for k in range(1, 5)]
        answer = {'cuts': [{'after_shot': 'S001', 'next_shot': 'S002', 'same_location': True, 'continuous': True, 'location_next': 'kitchen', 'reason': 'reverse'},
                           {'after_shot': 'S002', 'next_shot': 'S003', 'same_location': True, 'continuous': False, 'location_next': 'kitchen', 'reason': 'WEEK 1'},
                           {'after_shot': 'S003', 'next_shot': 'S004', 'same_location': False, 'continuous': False, 'location_next': 'salon', 'reason': 'salon'}]}
        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(scenes, 'shot_strip', return_value=Path(td)/'x.jpg'), \
                unittest.mock.patch.object(scenes, '_rms_track', return_value=__import__('numpy').zeros(0)):
            master = Path(td)/'m.mp4'; master.write_bytes(b'x')
            cuts = scenes.cut_levels(td, master, shots, 30, call=lambda parts: answer)
            self.assertEqual({k: c['level'] for k, c in cuts.items()}, {1: 'none', 2: 'shot', 3: 'paragraph'})
            (Path(td)/'plan/lock_overrides.json').write_text(json.dumps({'S002': 'paragraph'}))
            self.assertEqual(scenes.cut_levels(td, master, shots, 30, call=None)[1]['level'], 'paragraph')   # cached + override


if __name__ == '__main__':
    unittest.main()
