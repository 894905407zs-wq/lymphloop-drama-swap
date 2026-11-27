"""Independent audio-only verbatim check; no expected script supplied and no video generation."""
from pathlib import Path
import argparse,json,subprocess
import google_api as api,media_pipeline as media
PROMPT=('Listen only to the attached audio. Transcribe the speech actually audible, verbatim in its original language. '
 'Do not infer or complete missing words from context. Preserve omissions and malformed pronunciation; write [unintelligible] where needed. '
 'If there is no intelligible human speech, set speech_present false and transcript empty. Breathing, swallowing and room noise are not speech. '
 'Give each audible utterance with approximate start/end seconds, and describe any uncertain word exactly as it sounds. Do not normalize uncertain brand names.')
SCHEMA={'type':'OBJECT','properties':{'speech_present':{'type':'BOOLEAN'},'transcript':{'type':'STRING'},'utterances':{'type':'ARRAY','items':{'type':'OBJECT','properties':{'start':{'type':'NUMBER'},'end':{'type':'NUMBER'},'text':{'type':'STRING'}},'required':['start','end','text']}},'uncertain_words':{'type':'STRING'},'non_speech_sounds':{'type':'STRING'}},'required':['speech_present','transcript','utterances','uncertain_words','non_speech_sounds']}
def main():
 p=argparse.ArgumentParser();p.add_argument('--media',required=True);p.add_argument('--out',required=True);p.add_argument('--model',default='gemini-3.8-flash');a=p.parse_args()
 src=Path(a.media).resolve();out=Path(a.out).resolve();out.parent.mkdir(parents=True,exist_ok=True)
 import agent_native
 if agent_native.is_native():
  wav=out.with_suffix('.wav');subprocess.run([media.binary('ffmpeg'),'-v','error','-y','-i',str(src),'-vn','-ac','1','-ar','16000',str(wav)],check=True)
  sents=agent_native.whisper_sentences(wav,gap_s=0.7)
  transcript=' '.join(s['text'] for s in sents).strip()
  result={'speech_present':bool(transcript),'transcript':transcript,'utterances':[{'start':s['start'],'end':s['end'],'text':s['text']} for s in sents],'uncertain_words':'','non_speech_sounds':'listen to audio_wav directly via read_file if needed','audio_wav':str(wav)}
  api.save(out,{'type':'independent_audio_verbatim_check','mode':'antigravity_native','source':str(src),'audio_wav':str(wav),'audio_sha256':api.file_sha(wav),'result':result});print(json.dumps(result,ensure_ascii=False));return
 key={'source_sha256':api.file_sha(src),'model':a.model,'prompt':PROMPT,'schema':SCHEMA}
 if out.exists()and api.read(out).get('key')==key:print(json.dumps(api.read(out)['result'],ensure_ascii=False));return
 wav=out.with_suffix('.wav');subprocess.run([media.binary('ffmpeg'),'-v','error','-y','-i',str(src),'-vn','-ac','1','-ar','16000',str(wav)],check=True)
 t=api.Transport(api.required_key());part=api.media_part(wav,out.parent/'google',t)
 payload={'contents':[{'role':'user','parts':[{'text':PROMPT},part]}],'generationConfig':{'temperature':0,'responseMimeType':'application/json','responseSchema':SCHEMA}}
 response=t.json(api.API+'/v1beta/models/'+a.model+':generateContent',method='POST',payload=payload,timeout=300)
 raw='\n'.join(x.get('text','')for c in response.get('candidates',[])for x in c.get('content',{}).get('parts',[])if not x.get('thought'));result=json.loads(raw)
 api.save(out,{'type':'independent_audio_verbatim_check','key':key,'source':str(src),'audio_sha256':api.file_sha(wav),'result':result,'usage_metadata':response.get('usageMetadata'),'cost_usd':None});print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
