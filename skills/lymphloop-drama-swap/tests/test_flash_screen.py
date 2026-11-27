import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import google_api as api
import media_pipeline as media
import flash_screen as fs
import shots


def raw(**over):
    base = {'competitor_visible': 'no', 'visible_evidence': 'plain colour field', 'brand_text': [], 'ingestion_action': 'none',
            'speech_present': False, 'speech_mentions_product': 'no', 'speech_transcript': '', 'captions_present': False,
            'floating_watermark': False, 'confidence': .9}
    return base | over


class FakeTransport:
    key = 'test-secret'

    def __init__(self, answers):
        self.answers = list(answers); self.calls = []

    def json(self, url, **kw):
        self.calls.append((url, kw))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {'responseId': f'resp-{len(self.calls)}', 'usageMetadata': {'totalTokenCount': 1},
                'candidates': [{'content': {'parts': [{'text': json.dumps(answer)}]}}]}


def fake_upload(path, state_root, transport, **kw):
    return {'name': 'files/fake', 'uri': 'https://generativelanguage.googleapis.com/v1beta/files/fake', 'mimeType': 'video/mp4', 'state': 'ACTIVE'}


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root / 'source.mp4'
        media.ff(['-y', '-f', 'lavfi', '-i', 'color=c=red:s=64x96:r=25:d=4', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
                  '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(self.source)])
        self.shots_json = self.root / 'shots.json'
        media.save(self.shots_json, {'source': str(self.source), 'source_sha256': shots.sha(self.source), 'fps': '25', 'frame_count': 100,
                                     'shots': [{'id': f'S00{i+1}', 'in_frame': i * 25, 'out_frame': (i + 1) * 25} for i in range(4)]})

    def tearDown(self):
        self.temp.cleanup()

    def test_verdict_rules_bias_to_recall(self):
        self.assertEqual(fs.verdict_for(raw(competitor_visible='yes'))[0], 'replace')
        self.assertEqual(fs.verdict_for(raw(competitor_visible='unsure'))[0], 'replace')
        self.assertEqual(fs.verdict_for(raw(ingestion_action='dropper'))[0], 'replace')
        self.assertEqual(fs.verdict_for(raw(brand_text=['LYMPHORIA']))[0], 'keep')  # brand text alone never pays for a regeneration
        self.assertEqual(fs.verdict_for(raw(brand_text=['LYMPHORIA'], competitor_visible='unsure'))[0], 'replace')
        self.assertEqual(fs.verdict_for(raw(brand_text=['yesnap LymphLoop']))[0], 'keep')
        self.assertEqual(fs.verdict_for(raw(speech_present=True, speech_mentions_product='unsure'))[0], 'replace')
        self.assertEqual(fs.verdict_for(raw())[0], 'keep')

    def test_screen_cuts_clips_applies_rules_and_caches(self):
        answers = [raw(competitor_visible='yes', visible_evidence='bottle'), raw(competitor_visible='unsure'),
                   raw(ingestion_action='dropper'), raw()]
        http = FakeTransport(answers)
        with patch.object(api, 'upload_file', fake_upload):
            summary = fs.screen(self.shots_json, self.root / 'run', transport=http)
            self.assertEqual([r['verdict'] for r in summary['shots']], ['replace', 'replace', 'replace', 'keep'])
            self.assertEqual(summary['replace_ids'], ['S001', 'S002', 'S003']); self.assertEqual(summary['unsure_ids'], ['S002'])
            self.assertEqual(summary['keep_ids'], ['S004']); self.assertEqual(len(http.calls), 4)
            for sid in ['S001', 'S002', 'S003', 'S004']:
                self.assertTrue((self.root / 'run/clips' / (sid + '.mp4')).is_file())
                self.assertTrue((self.root / 'run/screen' / (sid + '.json')).is_file())
            self.assertEqual(media.probe(str(self.root / 'run/clips/S002.mp4'))['frame_count'], 25)
            payload = http.calls[0][1]['payload']
            self.assertEqual(payload['generationConfig']['responseSchema'], fs.SCREEN_SCHEMA)
            self.assertTrue(any('inlineData' in p and p.get('videoMetadata') for p in payload['contents'][0]['parts']))     # small clips inline (2026-11-22: upload 429)
            self.assertTrue(payload['contents'][0]['parts'][0]['text'].startswith('You are screening'))
            again = fs.screen(self.shots_json, self.root / 'run', transport=FakeTransport([]))
            self.assertEqual(len(http.calls), 4); self.assertTrue(all(r['cache_hit'] for r in again['shots']))
            self.assertEqual(again['replace_ids'], summary['replace_ids'])
            saved = json.loads((self.root / 'run/screen/summary.json').read_text())
            self.assertEqual(saved['type'], 'lymphloop_flash_screen_summary')

    def test_known_failure_is_recorded_not_retried(self):
        http = FakeTransport([api.HTTPFailure(400)])
        with patch.object(api, 'upload_file', fake_upload):
            with self.assertRaises(api.HTTPFailure):
                fs.screen(self.shots_json, self.root / 'run', ids=['S001'], transport=http)
            with self.assertRaisesRegex(api.AdapterError, 'failed earlier'):
                fs.screen(self.shots_json, self.root / 'run', ids=['S001'], transport=FakeTransport([raw()]))
        states = list((self.root / 'run/flash').glob('*/result.json'))
        self.assertEqual(len(states), 1); self.assertEqual(json.loads(states[0].read_text())['status'], 'known_failed')

    def test_qa_writes_structured_second_opinion(self):
        clip = shots.cut(self.source, 0, 25, self.root / 'src.mp4'); cand = shots.cut(self.source, 25, 50, self.root / 'cand.mp4')
        prompt = self.root / 'prompt.txt'; prompt.write_text('【Generation Goal】 test prompt')
        answer = {'product_consistency': 80, 'action_fidelity': 70, 'identity_scene_consistency': 90, 'competitor_residue': False,
                  'subtitles_present': False, 'watermark_present': False, 'artifacts': ['slight flicker'], 'lip_sync': 'n/a',
                  'speech_transcript': '', 'verdict': 'revise', 'findings': ['sip stage missing']}
        http = FakeTransport([answer])
        with patch.object(api, 'upload_file', fake_upload):
            record = fs.qa(clip['path'], cand['path'], prompt, self.root / 'qa.json', self.root / 'run', task_kind='visual', transport=http)
        self.assertEqual(record['result'], answer); self.assertEqual(record['type'], 'lymphloop_flash_qa')
        self.assertEqual(record['prompt_sha256'], api.sha(prompt.read_bytes()))
        self.assertEqual(json.loads((self.root / 'qa.json').read_text())['candidate']['sha256'], cand['sha256'])
        payload = http.calls[0][1]['payload']
        self.assertEqual(payload['generationConfig']['responseSchema'], fs.QA_SCHEMA)
        self.assertEqual(sum('inlineData' in p for p in payload['contents'][0]['parts']), 2)
        self.assertIn('set lip_sync to "n/a"', payload['contents'][0]['parts'][0]['text'])

    def test_large_clip_still_uploads(self):
        clip = shots.cut(self.source, 0, 25, self.root / 'big.mp4')
        with patch.object(api, 'upload_file', fake_upload), patch.object(api, 'INLINE_MAX_BYTES', 10):
            parts = fs._build_parts([('video', clip['path'])], self.root / 'run', None, 2)
        self.assertIn('fileData', parts[0]); self.assertEqual(parts[0]['videoMetadata'], {'fps': 2})

    def test_probe_only_is_offline(self):
        out = subprocess.run([sys.executable, str(Path(fs.__file__)), 'probe-only'], capture_output=True, text=True,
                             env={'PATH': '/usr/bin:/bin', 'GEMINI_API_KEY': '',
                                  # what Python itself needs on Windows (system folder, home folder) ; no key
                                  **{k: os.environ[k] for k in ('SYSTEMROOT', 'PYTHONUTF8', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP') if k in os.environ}})
        self.assertEqual(out.returncode, 0, out.stderr)
        doc = json.loads(out.stdout)
        self.assertFalse(doc['network']); self.assertEqual(doc['model'], 'gemini-3.8-flash'); self.assertIn('lymphloop', doc['screen_prompt'].lower()); self.assertIn('unsure', doc['screen_prompt'])


if __name__ == '__main__':
    unittest.main()
