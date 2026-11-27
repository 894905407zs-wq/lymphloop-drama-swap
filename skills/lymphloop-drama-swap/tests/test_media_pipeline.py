"""Offline regression tests using synthetic video/audio; no model/API calls."""
import importlib.util
import json
import math
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
from array import array

MODULE = Path(__file__).resolve().parents[1] / 'scripts/media_pipeline.py'
spec = importlib.util.spec_from_file_location('media_pipeline', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def movie(path, duration, fps=25, color='red', tone=220):
    m.ff(['-y', '-f', 'lavfi', '-i', f'color=c={color}:s=128x192:r={fps}:d={duration}',
          '-f', 'lavfi', '-i', f'sine=frequency={tone}:sample_rate=48000:duration={duration}',
          '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-preset', 'ultrafast',
          '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', path])


def tone(path, duration, frequency=880):
    m.ff(['-y', '-f', 'lavfi', '-i', f'sine=frequency={frequency}:sample_rate=48000:duration={duration}',
          '-ac', '2', '-c:a', 'pcm_s16le', path])


def spectral_strength(path, start, frequency):
    with wave.open(str(path), 'rb') as f:
        sr, channels = f.getframerate(), f.getnchannels()
        f.setpos(round(start*sr))
        x = array('h', f.readframes(round(.1*sr)))[::channels]
    a = sum(v*math.cos(2*math.pi*frequency*i/sr) for i,v in enumerate(x))
    b = sum(v*math.sin(2*math.pi*frequency*i/sr) for i,v in enumerate(x))
    return math.hypot(a,b)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.binary('ffmpeg'); m.binary('ffprobe')
        cls.temp = tempfile.TemporaryDirectory(prefix='lymphloop-media-test-')
        cls.root = Path(cls.temp.name)
        movie(cls.root/'source.mp4', 8)
        movie(cls.root/'v1.mp4', 2.4, color='blue')
        movie(cls.root/'v2.mp4', 1, color='green')
        movie(cls.root/'dialogue.mp4', 1.4, color='yellow', tone=660)
        tone(cls.root/'vo.wav', 3)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def manifest(self, name='timeline.json'):
        data = {'schema_version':1,'source':'source.mp4','fps':'25','max_speed':1.2,
          'picture_replacements':[
            {'id':'V1','kind':'Visual','source_in':25,'source_out':75,'path':'v1.mp4','evidence':'verified synthetic boundary'},
            {'id':'V2','kind':'Ingredient','source_in':75,'source_out':100,'path':'v2.mp4','evidence':'adjacent full shot'},
            {'id':'D1','kind':'Dialogue','source_in':100,'source_out':125,'path':'dialogue.mp4','evidence':'complete utterance'}],
          'audio_replacements':[{'id':'VO1','kind':'VO','source_in':0,'source_out':50,'path':'vo.wav','evidence':'complete source sentence'}]}
        path=self.root/name; m.save(path,data); return path

    def test_complete_recut_and_no_old_voice(self):
        path=self.manifest()
        p=m.plan(path)
        self.assertEqual(p['output_frames'],235)
        self.assertEqual(p['output_fps'],'25')
        self.assertEqual(p['audio_replacements'][0]['picture_stretch'],1.5)
        kinds=[x['kind'] for x in p['segments']]
        self.assertEqual(kinds,['Original','Visual','Visual','Ingredient','Dialogue','Original'])
        for left,right in zip(p['segments'],p['segments'][1:]):
            self.assertEqual(left['out_out'],right['out_in'])
            self.assertEqual(left['source_out'],right['source_in'])
        out=self.root/'result.mp4'; work=self.root/'render'
        report=m.render(path,out,work)
        self.assertEqual(report['verification']['frame_count'],235)
        self.assertTrue(report['verification']['full_decode_passed'])
        wav=work/'final_mix.wav'
        self.assertLess(spectral_strength(wav,.4,220),spectral_strength(wav,.4,880)*.02)
        self.assertGreater(spectral_strength(wav,5.3,660),spectral_strength(wav,5.3,220)*20)
        self.assertGreater(spectral_strength(wav,7,220),spectral_strength(wav,7,880)*20)
        with wave.open(str(wav),'rb') as f:
            self.assertEqual(f.getnframes(),235*48000//25)
        # Resume preserves matching rendered atom files rather than paying for any model call.
        first=Path(json.loads(Path(str(out)+'.timeline.json').read_text())['segments'][0]['atom_video'])
        stat=first.stat().st_mtime_ns
        m.render(path,out,work)
        self.assertEqual(first.stat().st_mtime_ns,stat)

    def test_ripple_redo_reencodes_only_the_changed_atom(self):
        # User 2026-11-22: one goof redo used to re-encode the whole film. Atoms are keyed by content, not position:
        # a longer V1 moves everything after it, and only V1 is encoded again.
        movie(self.root/'v1_long.mp4', 3.2, color='purple')
        def manifest(v1, name):
            data={'schema_version':1,'source':'source.mp4','fps':'25','timing':'ripple','max_speed':1.0,
                  'picture_replacements':[
                    {'id':'V1','kind':'Visual','source_in':25,'source_out':75,'path':v1,'clip_in':0,'clip_out':2.0,'evidence':'synthetic'},
                    {'id':'D1','kind':'Dialogue','source_in':100,'source_out':125,'path':'dialogue.mp4','clip_in':0,'clip_out':1.0,'evidence':'synthetic'}],
                  'audio_replacements':[]}
            path=self.root/name; m.save(path,data); return path
        work=self.root/'ripple_work'
        m.render(manifest('v1.mp4','ripple_a.json'),self.root/'ripple_a.mp4',work)
        a=json.loads(Path(str(self.root/'ripple_a.mp4')+'.timeline.json').read_text())['segments']
        before={x['atom_key']:Path(x['atom_video']).stat().st_mtime_ns for x in a}
        data=json.loads(Path(self.root/'ripple_a.json').read_text()); data['picture_replacements'][0].update(path='v1_long.mp4',clip_out=3.0)
        m.save(self.root/'ripple_b.json',data)
        report=m.render(self.root/'ripple_b.json',self.root/'ripple_b.mp4',work)
        b=json.loads(Path(str(self.root/'ripple_b.mp4')+'.timeline.json').read_text())['segments']
        self.assertEqual(report['verification']['frame_count'],a[-1]['out_out']+25)
        changed=[x for x in b if x['atom_key'] not in before]
        self.assertEqual([x.get('picture_id') for x in changed],['V1'])
        for x in b:
            if x['atom_key'] in before:
                self.assertEqual(Path(x['atom_video']).stat().st_mtime_ns,before[x['atom_key']])
        self.assertGreater(b[-1]['out_in'],a[-1]['out_in'])     # later atoms moved, same files

    def test_frozen_render_uses_selected_plan_without_replanning(self):
        manifest = self.manifest('frozen_timeline.json')
        review = self.root/'generated_index.json'
        m.save(review, {'status': 'reviewed', 'items': ['V1', 'V2', 'D1', 'VO1']})
        frozen_path = self.root/'frozen_edit_plan.json'
        frozen = m.freeze(manifest, frozen_path, [review])
        self.assertEqual(frozen['plan']['output_frames'], 235)
        self.assertEqual(m.verify_frozen_plan(frozen_path, manifest)['plan_sha256'], frozen['plan_sha256'])
        with patch.object(m, 'plan', side_effect=AssertionError('render must not replan')):
            report = m.render(manifest, self.root/'frozen_master.mp4', self.root/'frozen_render', frozen_path)
        self.assertEqual(report['verification']['frame_count'], 235)
        timeline = json.loads(Path(report['timeline']).read_text())
        self.assertEqual(timeline['frozen_edit_plan']['sha256'], m.file_sha256(frozen_path))
        manifest.write_text(manifest.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'Frozen dependency changed'):
            m.verify_frozen_plan(frozen_path)

    def test_frozen_hash_guards_reviews_media_and_timeline(self):
        copied = self.root/'frozen_source_copy.mp4'
        copied.write_bytes((self.root/'source.mp4').read_bytes())
        manifest = self.root/'frozen_guard_manifest.json'
        m.save(manifest, {'source': str(copied)})
        review = self.root/'frozen_guard_review.json'
        m.save(review, {'status': 'reviewed', 'items': []})
        frozen_path = self.root/'frozen_guard.json'
        with self.assertRaisesRegex(ValueError, 'upstream'):
            m.freeze(manifest, frozen_path, [])
        m.freeze(manifest, frozen_path, [review])
        review.write_text(review.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'Frozen dependency changed'):
            m.verify_frozen_plan(frozen_path)
        m.freeze(manifest, frozen_path, [review])
        # Size and modification time alone are insufficient to bind a selected media file.
        stat = copied.stat()
        contents = bytearray(copied.read_bytes()); contents[-1] ^= 1
        copied.write_bytes(contents)
        os.utime(copied, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'Frozen dependency changed'):
            m.verify_frozen_plan(frozen_path)
        copied.write_bytes((self.root/'source.mp4').read_bytes())
        m.freeze(manifest, frozen_path, [review])
        document = json.loads(frozen_path.read_text())
        document['plan']['segments'][0]['out_out'] += 1
        m.save(frozen_path, document)
        with self.assertRaisesRegex(ValueError, 'seal mismatch'):
            m.verify_frozen_plan(frozen_path)

    def test_short_vo_mutes_entire_old_sentence(self):
        tone(self.root/'short.wav',.4)
        path=self.root/'short_manifest.json'
        m.save(path,{'source':'source.mp4','audio_replacements':[{'id':'VO','kind':'VO',
               'source_in':0,'source_out':25,'path':'short.wav','evidence':'one full sentence'}]})
        m.render(path,self.root/'short_result.mp4',self.root/'short_work')
        with wave.open(str(self.root/'short_work/final_mix.wav'),'rb') as f:
            f.setpos(36000); chunk=array('h',f.readframes(4800))
            self.assertEqual(max(abs(v) for v in chunk),0)

    def test_strict_guards(self):
        path=self.manifest('invalid.json'); data=json.loads(path.read_text())
        data['picture_replacements'][0]['source_out']=65
        m.save(path,data)
        with self.assertRaisesRegex(ValueError,'explicit clip_in/clip_out'):
            m.plan(path)
        path=self.manifest('invalid.json'); data=json.loads(path.read_text())
        data['audio_replacements'].append({'id':'VO2','kind':'VO','source_in':25,'source_out':75,
              'path':'vo.wav','evidence':'bad overlap'})
        m.save(path,data)
        with self.assertRaisesRegex(ValueError,'Overlapping audio'):
            m.plan(path)
        data['audio_replacements']=data['audio_replacements'][:1]
        data['audio_replacements'][0]['background_path']='source.mp4'
        m.save(path,data)
        with self.assertRaisesRegex(ValueError,'background_isolated'):
            m.plan(path)

    def quiet_tail_manifest(self):
        path = self.root/'quiet_tail.json'
        speech = self.root/'quiet_speech_review.json'
        evidence = self.root/'quiet_tail_evidence.json'
        m.save(evidence, {'actual_review': 'synthetic yellow frames after 0.8s have no speaking face'})
        m.save(speech, {'items': [{'task_id':'D1', 'complete':True,
            'media_sha256':m.file_sha256(self.root/'dialogue.mp4'), 'utterance_range':[0,.6]}]})
        proof = self.root/'quiet_tail_review.json'
        m.save(proof, {'type':'lymphloop_non_speaking_tail_review','task_id':'D1',
            'media_sha256':m.file_sha256(self.root/'dialogue.mp4'),
            'source_tail_start':115, 'clip_tail_start':.8,
            'no_speaking_lips':True,'no_dialogue_audio':True,
            'reviewer':'synthetic fixture','review_note':'Complete synthetic utterance precedes quiet tail.',
            'speech_review':m.hashed_identity(speech),'visual_evidence':[m.hashed_identity(evidence)]})
        m.save(path, {'source':'source.mp4','fps':'25','picture_replacements':[
            {'id':'D1','kind':'Dialogue','source_in':100,'source_out':125,
             'path':'dialogue.mp4','clip_in':0,'clip_out':1.4,'evidence':'complete dialogue and quiet product tail',
             'non_speaking_tail_review':m.hashed_identity(proof)}],
            'audio_replacements':[{'id':'VO','kind':'VO','source_in':120,'source_out':135,
             'path':'vo.wav','evidence':'whole subsequent VO sentence'}]})
        return path, proof, speech

    def test_verified_quiet_tail_preserves_dialogue_and_slows_only_vo_picture(self):
        path, proof, speech = self.quiet_tail_manifest()
        p = m.plan(path)
        prefix = next(x for x in p['segments'] if x['source_in']==100)
        tail = next(x for x in p['segments'] if x['source_in']==120)
        self.assertAlmostEqual(prefix['picture_speed'], 1, places=7)
        self.assertLess(tail['picture_speed'], 1)
        native = next(x for x in p['audio_replacements'] if x['kind']=='Dialogue')
        self.assertEqual(native['source_out'],120)
        self.assertAlmostEqual(native['clip_out'],1.12)
        self.assertEqual(native['speed'],1)
        frozen = self.root/'quiet_frozen.json'
        m.freeze(path,frozen,[speech])
        m.render(path,self.root/'quiet_result.mp4',self.root/'quiet_render',frozen_plan=frozen)
        wav=self.root/'quiet_render/final_mix.wav'
        self.assertGreater(spectral_strength(wav,4.4,660),spectral_strength(wav,4.4,220)*20)
        self.assertGreater(spectral_strength(wav,5.4,880),spectral_strength(wav,5.4,660)*20)
        m.save(proof, {'tampered':True})
        with self.assertRaisesRegex(ValueError,'Frozen dependency changed'):
            m.verify_frozen_plan(frozen,path)

    def test_quiet_tail_rejects_speech_cut_wrong_media_or_unverified_evidence(self):
        for mutation in ['early_vo','early_tail','wrong_media','evidence_changed','speech_changed']:
            path, proof, speech = self.quiet_tail_manifest()
            data=json.loads(path.read_text()); review=json.loads(proof.read_text())
            if mutation=='early_vo':data['audio_replacements'][0]['source_in']=110
            elif mutation=='early_tail':review['clip_tail_start']=.5
            elif mutation=='wrong_media':review['media_sha256']='0'*64
            elif mutation=='evidence_changed':m.save(self.root/'quiet_tail_evidence.json',{'tampered':True})
            elif mutation=='speech_changed':m.save(speech,{'items':[]})
            m.save(proof,review)
            data['picture_replacements'][0]['non_speaking_tail_review']=m.hashed_identity(proof)
            m.save(path,data)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):m.plan(path)

    def test_dialogue_guards_and_automatic_id_collision(self):
        for a,b in [(100,125),(110,120),(90,110)]:
            path=self.manifest('dialogue_guard.json')
            data=json.loads(path.read_text())
            data['audio_replacements'].append({'id':'VO_COLLISION','kind':'VO',
                'source_in':a,'source_out':b,'path':'vo.wav','evidence':'bad VO/Dialogue overlap'})
            m.save(path,data)
            with self.assertRaisesRegex(ValueError,'VO overlaps Dialogue picture'):
                m.plan(path)
        path=self.manifest('dialogue_guard.json')
        data=json.loads(path.read_text())
        data['audio_replacements'].append({'id':'D_EXPLICIT','kind':'Dialogue',
            'source_in':100,'source_out':125,'path':'dialogue.mp4','clip_out':1.4,'speed':1.2,
            'evidence':'independent acceleration would desynchronize lips'})
        m.save(path,data)
        with self.assertRaisesRegex(ValueError,'independent Dialogue audio speed'):
            m.plan(path)
        data['audio_replacements'][-1]['speed']=1
        m.save(path,data)
        self.assertEqual(m.plan(path)['output_frames'],235)
        path=self.manifest('dialogue_guard.json')
        data=json.loads(path.read_text()); data['audio_replacements'][0]['id']='D1_audio'
        m.save(path,data)
        with self.assertRaisesRegex(ValueError,'collides with an input id'):
            m.plan(path)

    def test_exact_pcm_duration_does_not_add_a_frame(self):
        # ffprobe displays 3.466667, but 166400 / 48000 is exactly 104 / 30 seconds.
        pcm=self.root/'exact_104_frames.wav'
        with wave.open(str(pcm),'wb') as f:
            f.setnchannels(2); f.setsampwidth(2); f.setframerate(48000)
            f.writeframes(bytes(166400*4))
        path=self.root/'exact_duration.json'
        m.save(path,{'source':'source.mp4','output_fps':'30','audio_replacements':[
            {'id':'VO','kind':'VO','source_in':0,'source_out':25,'path':str(pcm),
             'evidence':'exact PCM samples, complete sentence'}]})
        plan=m.plan(path)
        self.assertEqual(plan['audio_replacements'][0]['natural_seconds_exact'],'52/15')
        self.assertEqual(plan['audio_replacements'][0]['out_out'],104)
        self.assertEqual(plan['output_frames'],314)

    def test_external_dialogue_duration_and_picture_route(self):
        path=self.manifest('external_dialogue.json')
        data=json.loads(path.read_text())
        data['audio_replacements'].append({'id':'D_EXPLICIT','kind':'Dialogue',
            'source_in':100,'source_out':125,'path':'vo.wav','speed':1,
            'evidence':'wrong independent audio length'})
        m.save(path,data)
        with self.assertRaisesRegex(ValueError,'external Dialogue audio duration differs'):
            m.plan(path)
        data['picture_replacements']=[]
        m.save(path,data)
        with self.assertRaisesRegex(ValueError,'requires a matching complete Dialogue picture'):
            m.plan(path)

    def test_audio_verification_rejects_missing_and_truncated_tail(self):
        missing=self.root/'missing_audio.mp4'
        m.ff(['-y','-i',self.root/'source.mp4','-an','-c:v','copy',missing])
        with self.assertRaisesRegex(ValueError,'no audio stream'):
            m.verify(missing,200,'25',8*48000)
        # A ten-millisecond missing tail can fit inside an AAC block, but is still truncation.
        short=self.root/'truncated_tail.mp4'
        m.ff(['-y','-i',self.root/'source.mp4','-filter_complex','[0:a]atrim=end=7.99[a]',
              '-map','0:v','-map','[a]','-c:v','copy','-c:a','aac',short])
        with self.assertRaisesRegex(ValueError,'Audio tail is short'):
            m.verify(short,200,'25',8*48000)
        valid=m.verify(self.root/'source.mp4',200,'25',8*48000)
        self.assertTrue(valid['audio_verification']['planned_tail_present'])
        self.assertLessEqual(abs(valid['audio_verification']['decoded_samples']-8*48000),1026)
        # A source with no audio remains usable: render creates complete silence instead of failing.
        manifest=self.root/'silent_source.json'
        m.save(manifest,{'source':str(missing),'fps':'25'})
        rendered=m.render(manifest,self.root/'silent_result.mp4',self.root/'silent_work')
        self.assertTrue(rendered['verification']['audio_verification']['planned_tail_present'])
        self.assertEqual(rendered['verification']['pcm_mix_verification']['decoded_samples'],8*48000)

    def test_fractional_fps_and_complete_audio_ceiling(self):
        movie(self.root/'fractional.mp4',1,fps='30000/1001')
        tone(self.root/'fractional_vo.wav',1.013)
        path=self.root/'fractional.json'
        m.save(path,{'source':'fractional.mp4','fps':'30000/1001',
             'audio_replacements':[{'id':'VO','kind':'VO','source_in':0,'source_out':m.probe(self.root/'fractional.mp4')['frame_count'],
               'path':'fractional_vo.wav','evidence':'complete fractional-frame sentence'}]})
        p=m.plan(path)
        self.assertEqual(p['output_frames'],31)
        self.assertGreaterEqual(p['output_seconds'],1.013)
        m.render(path,self.root/'fractional_result.mp4',self.root/'fractional_work')
        result=m.normalize(self.root/'fractional.mp4',self.root/'normalized.mp4')
        self.assertEqual(result['fps'],'30')
        self.assertTrue(Path(str(self.root/'normalized.mp4')+'.normalization.json').is_file())
        self.assertEqual(m.frame_to_tc(31,'30000/1001'),'[00:00:01:01]')

    def test_color_metadata_changes_keep_global_frame_indices(self):
        pieces=[]
        settings=[('red','bt709','bt709','bt709'),('blue','bt709','iec61966-2-1','bt709'),
                  ('green','2','2','2'),('yellow','bt709','bt709','bt709')]
        for i,(color,primaries,transfer,matrix) in enumerate(settings):
            part=self.root/f'color_metadata_{i}.mp4'
            m.ff(['-y','-f','lavfi','-i',f'color=c={color}:s=128x192:r=25:d=0.4',
                  '-frames:v','10','-c:v','libx264','-pix_fmt','yuv420p',
                  '-color_primaries',primaries,'-color_trc',transfer,'-colorspace',matrix,part])
            pieces.append(part)
        listing=self.root/'color_concat.txt'
        listing.write_text('\n'.join(m.concat_path(p) for p in pieces)+'\n')
        mixed=self.root/'mixed_colors.mp4'
        m.ff(['-y','-f','concat','-safe','0','-i',listing,'-c','copy',mixed])
        self.assertEqual(m.probe(mixed)['frame_count'],40)
        extracted=self.root/'color_extracted'
        self.assertEqual(m.extract(mixed,extracted,8,33,1)['count'],25)
        def rgb(path,frame=0):
            data=subprocess.check_output([m.binary('ffmpeg'),'-v','error','-reinit_filter','0','-i',str(path),
                '-vf',f'select=eq(n\\,{frame}),format=rgb24,crop=1:1:64:160','-frames:v','1',
                '-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
            return tuple(data[:3])
        def is_color(value,color):
            r,g,b=value
            return {'red':r>180 and g<40 and b<40,'blue':b>180 and r<40 and g<40,
                    'green':g>70 and r<40 and b<40,'yellow':r>180 and g>180 and b<40}[color]
        for frame,color in [(9,'red'),(10,'blue'),(29,'green'),(30,'yellow')]:
            self.assertTrue(is_color(rgb(extracted/f'F{frame:09d}.png'),color),(frame,color))
        candidates=m.scenes(mixed,.1)['candidates']
        self.assertEqual([x['source_frame'] for x in candidates],[10,20,30])
        normalized=self.root/'mixed_normalized.mp4'
        self.assertEqual(m.normalize(mixed,normalized,25)['frame_count'],40)
        # Rendering an Original tail with trim=start_frame=15 must count across earlier metadata changes.
        manifest=self.root/'mixed_render.json'
        m.save(manifest,{'source':str(mixed),'picture_replacements':[
            {'id':'V','kind':'Visual','source_in':0,'source_out':15,'path':str(pieces[0]),
             'evidence':'synthetic whole-shot replacement followed by original global frame 15'}]})
        rendered=self.root/'mixed_rendered.mp4'
        report=m.render(manifest,rendered,self.root/'mixed_render_work')
        self.assertEqual(report['verification']['frame_count'],40)
        self.assertTrue(is_color(rgb(rendered,15),'blue'))
        self.assertTrue(is_color(rgb(rendered,30),'yellow'))
        saved=json.loads((self.root/'mixed_render_work/render_plan.json').read_text())
        self.assertEqual(len(saved['renderer_implementation_sha256']),64)
        # Burned n must stay global too: compare the caption at global frame 25 with a stable-color baseline.
        stable=self.root/'stable_color.mp4'
        m.ff(['-y','-f','lavfi','-i','color=red:s=128x192:r=25:d=1.6','-frames:v','40',
              '-c:v','libx264','-pix_fmt','yuv420p',stable])
        burned=self.root/'mixed_timecode.mp4'; reference=self.root/'stable_timecode.mp4'
        m.burn_timecode(mixed,burned); m.burn_timecode(stable,reference)
        def caption(path,frame):
            return subprocess.check_output([m.binary('ffmpeg'),'-v','error','-reinit_filter','0','-i',str(path),
                '-vf',f'select=eq(n\\,{frame}),crop=112:16:8:8,format=gray','-frames:v','1',
                '-f','rawvideo','-pix_fmt','gray','pipe:1'])
        actual=caption(burned,25); correct=caption(reference,25); wrong=caption(reference,5)
        self.assertEqual(len(actual),112*16)
        correct_error=sum(abs(a-b) for a,b in zip(actual,correct))
        wrong_error=sum(abs(a-b) for a,b in zip(actual,wrong))
        self.assertLess(correct_error,wrong_error)

    def test_vfr_requires_working_copy_and_normalizes(self):
        vfr=self.root/'vfr.mp4'
        m.ff(['-y','-f','lavfi','-i','color=red:s=128x192:r=25:d=1',
              '-vf',"setpts='if(lt(N,10),N/(25*TB),(0.4+(N-10)/15)/TB)'",
              '-fps_mode','vfr','-c:v','libx264','-pix_fmt','yuv420p',vfr])
        info=m.probe(vfr,True)
        self.assertFalse(info['cfr'])
        with self.assertRaisesRegex(ValueError,'Variable frame timing'):
            m.require_cfr(info)
        target=self.root/'vfr_normalized.mp4'
        report=m.normalize(vfr,target,25)
        normalized=m.probe(target,True)
        self.assertTrue(normalized['cfr'])
        self.assertEqual(normalized['fps'],'25')
        detail=json.loads(Path(str(target)+'.normalization.json').read_text())
        self.assertEqual(len(detail['mapping']),normalized['frame_count'])
        self.assertTrue(detail['original_unchanged'])
        self.assertTrue(vfr.is_file())

    def test_burn_extract_and_scene_candidates(self):
        out=self.root/'timecoded.mp4'
        record=m.burn_timecode(self.root/'source.mp4',out)
        self.assertEqual(record['frame_count'],200)
        self.assertEqual(record['last_timecode'],'[00:00:07:24]')
        m.extract(out,self.root/'frames',23,28,1)
        rows=json.loads((self.root/'frames/frames.json').read_text())['frames']
        self.assertEqual([r['source_frame'] for r in rows],[23,24,25,26,27])
        self.assertEqual(rows[2]['timecode'],'[00:00:01:00]')
        self.assertAlmostEqual(rows[2]['pts'],1)
        self.assertTrue(all(Path(r['path']).is_file() for r in rows))
        # Exact red-to-blue edit should be recalled as a candidate, then reviewed locally.
        cut=self.root/'cut.mp4'
        m.ff(['-y','-f','lavfi','-i','color=red:s=128x192:r=25:d=1',
              '-f','lavfi','-i','color=blue:s=128x192:r=25:d=1',
              '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]',
              '-c:v','libx264','-pix_fmt','yuv420p',cut])
        candidates=m.scenes(cut,.1)['candidates']
        self.assertIn(25,[x['source_frame'] for x in candidates])


if __name__=='__main__':
    unittest.main(verbosity=2)
