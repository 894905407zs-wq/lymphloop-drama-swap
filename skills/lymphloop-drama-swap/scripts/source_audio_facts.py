"""Objective source soundtrack facts for prompt authors; no video generation."""
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
from pathlib import Path
from fractions import Fraction
import argparse,json,subprocess,concurrent.futures
import google_api as api,media_pipeline as media
MODEL='gemini-3.8-flash'
PROMPT='Listen to the attached source audio. Report only objective audible facts. Is there background MUSIC (instruments or a musical score), distinct from engine/road/room hum? Use yes/no/uncertain. Briefly describe music and ambient sounds in Chinese. Briefly describe audible human voice qualities without inventing unseen speakers. Do not transcribe or rewrite speech.'
SCHEMA={'type':'OBJECT','properties':{'music':{'type':'STRING','enum':['yes','no','uncertain']},'music_description':{'type':'STRING'},'ambience':{'type':'STRING'},'voice_qualities':{'type':'STRING'}},'required':['music','music_description','ambience','voice_qualities']}
def main():
 p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--import-facts',help='Antigravity native mode: import agent-listened audio facts JSON {G##: {...}}');a=p.parse_args();r=Path(a.run).resolve();st=api.read(r/'state.json');plan=api.read(r/'plan/segments.json');fps=float(Fraction(str(plan['fps'])));out=r/'plan/audio_facts';out.mkdir(parents=True,exist_ok=True)
 import agent_native
 if a.import_facts:
  raw=api.read(a.import_facts);summary={}
  for gid,facts in raw.items():
   api.save(out/(gid+'.json'),{'mode':'antigravity_native','facts':facts});summary[gid]=facts
  (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False,indent=2));return
 if agent_native.is_native():
  wavs={}
  for s in plan['segments']:
   wav=out/(s['id']+'.wav')
   if not wav.exists():subprocess.run([media.binary('ffmpeg'),'-v','error','-y','-ss',str(s['in_frame']/fps),'-t',str((s['out_frame']-s['in_frame'])/fps),'-i',st['source'],'-vn','-ac','1','-ar','16000',str(wav)],check=True)
   wavs[s['id']]=str(wav)
  print(json.dumps({'mode':'antigravity_native','wavs':wavs,'schema':SCHEMA,'next':f'用 read_file 听 wavs 后写出 {{G##: {{music, music_description, ambience, voice_qualities}}}}，执行 source_audio_facts.py --run {r} --import-facts FILE.json'},ensure_ascii=False,indent=2));return
 def one(s):
  key={'source_sha256':st['source_sha256'],'in_frame':s['in_frame'],'out_frame':s['out_frame'],'fps':fps,'model':MODEL,'prompt':PROMPT};dest=out/(s['id']+'.json')
  if dest.exists()and api.read(dest).get('key')==key:return s['id'],api.read(dest)['facts']
  wav=out/(s['id']+'-'+api.sha(api.json_bytes(key))[:12]+'.wav')
  if not wav.exists():subprocess.run([media.binary('ffmpeg'),'-v','error','-y','-ss',str(s['in_frame']/fps),'-t',str((s['out_frame']-s['in_frame'])/fps),'-i',st['source'],'-vn','-ac','1','-ar','16000',str(wav)],check=True)
  t=api.Transport(api.required_key());part=api.media_part(wav,r/'google',t)
  payload={'contents':[{'role':'user','parts':[{'text':PROMPT},part]}],'generationConfig':{'temperature':0,'responseMimeType':'application/json','responseSchema':SCHEMA}}
  resp=t.json(api.API+'/v1beta/models/'+MODEL+':generateContent',method='POST',payload=payload,timeout=180)
  text='\n'.join(p.get('text','')for c in resp.get('candidates',[])for p in c.get('content',{}).get('parts',[])if not p.get('thought'));facts=json.loads(text);record={'key':key,'facts':facts,'usage_metadata':resp.get('usageMetadata'),'cost_usd':None,'audio_sha256':api.file_sha(wav)};tmp=dest.with_suffix('.tmp');tmp.write_text(json.dumps(record,ensure_ascii=False,indent=2));tmp.replace(dest);return s['id'],facts
 with concurrent.futures.ThreadPoolExecutor(8)as ex:result=dict(ex.map(one,plan['segments']))
 (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
