"""Pure metadata fixtures: never opens media or invokes ffmpeg/API."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

local_scripts = Path(__file__).resolve().parent.parent/'scripts'
sys.path.insert(0, str(local_scripts if (local_scripts/'finished_patch.py').is_file() else Path.home()/'.codex/skills/lymphloop-swap-claude/scripts'))
import finished_patch as fp
import media_pipeline as media


class FinishedPatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.source = self.write('old.mp4', b'fake old media')
        self.timeline = self.write('old.timeline.json', {'output_fps': '30', 'output_frames': 300, 'width': 720, 'height': 1280})
        self.owner = self.write('owner.json', {'reviewer': 'Codex', 'master_sha256': self.source['sha256'], 'all_flashback_sheets_viewed': True})
        self.recipe = {'type': fp.TYPE, 'schema_version': 1, 'source': self.source, 'source_owner': self.owner,
                       'source_timeline': self.timeline, 'replacements': []}
        for ident, a, b in [('S180', 100, 130), ('S181', 130, 160)]:
            raw = self.write(ident+'.raw.mp4', ident.encode())
            final = self.write(ident+'.captioned.mp4', (ident+' captions').encode())
            ledger = self.write(ident+'.ledger.json', {'provider': 'apimart', 'candidates': {'c1': {
                'status': 'downloaded', 'output_path': raw['path'], 'output_sha256': raw['sha256']}}})
            image = self.write(ident+'.png', b'fake reviewed image')
            review = self.write(ident+'.review.json', {'reviewer': 'Codex', 'reviewed': True,
                'raw_media': raw, 'media': final, 'clip_seconds': ['0', '1'], 'own_same_time_audio': True,
                'caption_audio_packet_sha256': {'before': 'f'*64, 'after': 'f'*64}, 'evidence': [image]})
            self.recipe['replacements'].append({'id': ident, 'segment': 'G01' if ident == 'S180' else 'G02', 'old_frames': [a,b], 'raw_media': raw,
                'media': final, 'generation_ledger': ledger, 'candidate': 'c1', 'clip_seconds': ['0','1'], 'review': review})

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content):
        p = self.d/name
        p.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
        return fp.ref(p)

    def edited_review(self, key, value):
        r = self.recipe['replacements'][0]
        p = Path(r['review']['path']); d = fp.read(p); d[key] = value
        p.write_text(json.dumps(d)); r['review'] = fp.ref(p)

    def native_plan(self, gap=False):
        if gap:
            self.recipe['replacements'][1]['old_frames'] = [140,160]
        manifest, _, _ = fp.build_manifest(self.recipe)
        atoms = []
        audios = []
        cursor = 0
        for row in self.recipe['replacements']:
            a,b = row['old_frames']
            if cursor < a:
                atoms.append({'source_in':cursor,'source_out':a,'kind':'Original','picture_id':None,
                              'path':self.source['path'],'audio_replacement_id':None,'frames':a-cursor})
            atoms.append({'source_in':a,'source_out':b,'kind':'Dialogue','picture_id':row['id'],
                          'path':row['media']['path'],'audio_replacement_id':row['id']+'_audio','frames':30,
                          'clip_in':0.,'clip_out':1.})
            audios.append({'id':row['id']+'_audio','path':row['media']['path'],'clip_in':0.,'clip_out':1.,'speed':1})
            cursor=b
        atoms.append({'source_in':cursor,'source_out':300,'kind':'Original','picture_id':None,
                      'path':self.source['path'],'audio_replacement_id':None,'frames':300-cursor})
        out=0
        for a in atoms:
            a.update(out_in=out,out_out=out+a['frames'],picture_speed=1.,source_audio_speed=1.)
            out=a['out_out']
        paths=[self.source['path']]+[r['media']['path'] for r in self.recipe['replacements']]
        return {'source':self.source['path'],'source_has_audio':True,'source_frames':300,'output_frames':out,'output_fps':'30',
                'timing':'ripple','max_speed':1,'output_seconds':out/30,'output_samples':out*1600,'sample_rate':48000,
                'segments':atoms,'audio_replacements':audios,'inputs':[media.file_identity(p) for p in paths]}

    def test_paired_two_only(self):
        m,_,_=fp.build_manifest(self.recipe)
        self.assertEqual([p['kind'] for p in m['picture_replacements']], ['Dialogue','Dialogue'])
        self.assertEqual(m['audio_replacements'],[])
        self.assertEqual(m['timing'],'ripple')
        self.assertEqual(m['source'],self.source['path'])

    def test_adjacent_four_atoms(self):
        p=self.native_plan();fp.validate_plan(self.recipe,p)
        self.assertEqual(len(p['segments']),4)

    def test_gap_five_atoms(self):
        p=self.native_plan(True);fp.validate_plan(self.recipe,p)
        self.assertEqual(len(p['segments']),5)

    def test_stale_source_rejected(self):
        Path(self.source['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):fp.build_manifest(self.recipe)

    def test_overlap_rejected(self):
        self.recipe['replacements'][1]['old_frames']=[120,160]
        with self.assertRaisesRegex(ValueError,'overlapping'):fp.build_manifest(self.recipe)

    def test_wrong_raw_ledger_rejected(self):
        r=self.recipe['replacements'][0];p=Path(r['generation_ledger']['path']);d=fp.read(p)
        d['candidates']['c1']['output_sha256']='0'*64;p.write_text(json.dumps(d));r['generation_ledger']=fp.ref(p)
        with self.assertRaisesRegex(ValueError,'registered'):fp.build_manifest(self.recipe)

    def test_unknown_provider_rejected(self):
        r=self.recipe['replacements'][0];p=Path(r['generation_ledger']['path']);d=fp.read(p);d['provider']='fake'
        p.write_text(json.dumps(d));r['generation_ledger']=fp.ref(p)
        with self.assertRaisesRegex(ValueError,'strict shared'):fp.build_manifest(self.recipe)

    def test_changed_caption_audio_rejected(self):
        self.edited_review('caption_audio_packet_sha256',{'before':'f'*64,'after':'e'*64})
        with self.assertRaisesRegex(ValueError,'audio packets'):fp.build_manifest(self.recipe)

    def test_foreign_voice_rejected(self):
        self.edited_review('own_same_time_audio',False)
        with self.assertRaisesRegex(ValueError,'same-time'):fp.build_manifest(self.recipe)

    def test_unreviewed_clip_rejected(self):
        self.recipe['replacements'][0]['clip_seconds']=['0','2']
        with self.assertRaisesRegex(ValueError,'exact chosen'):fp.build_manifest(self.recipe)

    def test_old_owner_wrong_master_rejected(self):
        p=Path(self.owner['path']);d=fp.read(p);d['master_sha256']='a'*64;p.write_text(json.dumps(d))
        self.recipe['source_owner']=fp.ref(p)
        with self.assertRaisesRegex(ValueError,'exact old finished'):fp.build_manifest(self.recipe)

    def test_speed_rejected(self):
        p=self.native_plan();p['segments'][-1]['picture_speed']=1.1
        with self.assertRaisesRegex(ValueError,'Speed'):fp.validate_plan(self.recipe,p)

    def test_tail_from_other_file_rejected(self):
        p=self.native_plan();p['segments'][-1]['path']='other.mp4'
        with self.assertRaisesRegex(ValueError,'old finished picture'):fp.validate_plan(self.recipe,p)

    def test_av_different_window_rejected(self):
        p=self.native_plan();p['audio_replacements'][0]['clip_out']=.9
        with self.assertRaisesRegex(ValueError,'same file and same time'):fp.validate_plan(self.recipe,p)

    def test_no_output_overwrite(self):
        with self.assertRaisesRegex(ValueError,'never overwrite'):fp.render(self.d,self.source['path'])

    def test_prepare_real_native_freeze_no_render(self):
        native_plan=self.native_plan();rp=self.write('recipe.json',self.recipe)['path'];destination=self.d/'prepared'
        with patch.object(media,'plan',return_value=native_plan),patch.object(media,'render',side_effect=AssertionError('must not render')):
            receipt=fp.prepare(rp,destination)
        self.assertEqual(receipt['status'],'prepared_not_rendered')
        self.assertEqual(receipt['output_frames'],300)
        self.assertFalse(receipt['review_scope']['new_full_film_semantic_review'])
        self.assertTrue(receipt['export']['reencodes_old_finished_material'])
        self.assertFalse(receipt['export']['tail_bitstream_identity_claimed'])
        self.assertEqual(receipt['join_frames'],[100,130,160])
        self.assertEqual([x['segment'] for x in receipt['replacements']],['G01','G02'])
        media.verify_frozen_plan(destination/'frozen.json')


if __name__=='__main__':
    unittest.main(verbosity=2)
