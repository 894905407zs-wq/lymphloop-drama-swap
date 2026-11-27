"""Remove only source-bound, agent-reviewed title cards from existing clean references."""
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
from pathlib import Path
import argparse,json,os
import swap,caption_lama as cl,caption_clean as cc,media_pipeline as media

def main():
 p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--lock-wait',type=float,default=18000);a=p.parse_args();r=Path(a.run).resolve()
 with swap.flock(r/'.run.lock',False,a.lock_wait,'reviewed reference title repair'):
  st=swap.read(r/'state.json');review=swap.read(r/'plan/reference_title_review.json');assert review['approved']and review['source_sha256']==st['source_sha256']
  assert not list((r/'generation/jobs').glob('*.json')),'Repair before submission'
  for gid,policy in review['segments'].items():
   src=r/'clips'/f'{gid}.hq.clean.ref.mp4';meta=src.with_suffix('.json');m=swap.read(meta);before=swap.sha(src);old=m.get('caption_title_repair',{})
   if old.get('review_sha256')==swap.sha(r/'plan/reference_title_review.json')and old.get('output_sha256')==before:continue
   tmp=src.with_name(src.stem+'.titles.part.mp4');W,H,_=cc.probe(src);work=r/'clips/title_work';duration=float(m['duration_s'])
   report=cl.clean(src,tmp,work,keep_audio=True,pixels={'path':str(src),'ss':0,'t':duration,'size':[W,H],'vf':None,'crf':12,'maxrate':m['maxrate'],'bufsize':str(2*int(m['maxrate'][:-1]))+'k','audio_copy':True,'caption_policy':{'title_pattern':policy['pattern'],'min_text_height_px':1000000},'source_spoken':[]})
   assert report['frames_left_caption']==0,'Title still readable'
   assert media.probe(str(tmp))['frame_count']==report['frames']
   archive=src.with_name(src.stem+'.before-titles.mp4')
   if not archive.exists():oscompat.replace(src,archive)
   oscompat.replace(tmp,src);m['output_sha256']=swap.sha(src);m['encodes']=m.get('encodes',1)+1;m['caption_title_repair']={'review_sha256':swap.sha(r/'plan/reference_title_review.json'),'input_sha256':before,'output_sha256':m['output_sha256'],'report':report};swap.save(meta,m);print(gid,'reviewed title removed',report['caption_texts'],'frames',report['frames_cleaned'],flush=True)
if __name__=='__main__':main()
