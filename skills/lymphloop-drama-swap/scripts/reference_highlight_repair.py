#!/usr/bin/env python3
"""Remove reviewed caption backplates from cleaned references, preserving all props.
Only runs on a source-bound, agent-reviewed caption region before video submission.
The ordinary reference cache keeps the truthful additional-encode provenance.
"""
from pathlib import Path
import argparse,json,subprocess,os
from concurrent.futures import ThreadPoolExecutor
import cv2,numpy as np
import swap,caption_lama as cl,caption_clean as cc

def caption_backplate_boxes(raw, clean, anchors, region):
    H,W=raw.shape[:2];k=W/720
    def yellow(f):
        # Actual caption backplates are bright lemon yellow. Hue alone also
        # selects real cleavers leaves and gold animation highlights after the
        # text has already gone; those source details must remain untouched.
        return (cv2.inRange(cv2.cvtColor(f,cv2.COLOR_BGR2HSV),(18,100,170),(40,255,255))
                & cv2.inRange(f,(0,205,225),(190,255,255)))
    a=yellow(raw);b=yellow(clean)
    scope=np.zeros((H,W),np.uint8);x,y,w,h=[round(v*k)for v in region];scope[y:y+h,x:x+w]=255
    anchor=cl.line_mask(raw.shape,anchors)
    anchor=cv2.dilate(anchor,np.ones((round(20*k)+1,round(40*k)+1),np.uint8))
    a&=scope
    n,labels,stats,_=cv2.connectedComponentsWithStats(a)
    boxes=[]
    for x,y,w,h,area in stats[1:]:
        if area<200*k*k or w<8*k or h<24*k or area/(w*h)<.25:continue
        if not anchor[y:y+h,x:x+w].any() or cv2.countNonZero(b[y:y+h,x:x+w])<20*k*k:continue
        boxes.append([int(x),int(y),int(w),int(h)])
    return boxes

def repair(run, recheck=False, segments=None):
    state=swap.read(run/'state.json');review=swap.read(run/'plan/reference_highlight_review.json')
    assert review['approved'] and review['source_sha256']==swap.sha(state['source'])
    assert not list((run/swap.GEN_DIR/'jobs').glob('*.json')), 'Must repair before any video submission'
    reports=[]
    for seg in swap.read(run/'plan/segments.json')['segments']:
        gid=seg['id']
        if segments and gid not in segments: continue
        src=run/'clips'/f'{gid}.hq.clean.ref.mp4';raw=run/'clips'/f'{gid}.hq.ref.mp4';meta=src.with_suffix('.json');m=swap.read(meta)
        mark=m.get('caption_backplate_repair',{})
        if not recheck and mark.get('review_sha256')==swap.sha(run/'plan/reference_highlight_review.json') and mark.get('output_sha256')==swap.sha(src):
            reports.append(mark);continue
        scan=cc._scan(raw,run/'clips/caption_work');pol=swap.read(run/'plan/reference_caption_policy.json').get(gid)
        rw=seg.get('rewrites')or{};spoken=[s['text']for s in seg.get('speech',[])]+[s['original']for s in rw.get('dialogue',[])+rw.get('vo',[])]
        held,_=cl.held_boxes(scan,cl.shot_starts(raw),pol,spoken)
        cap=cv2.VideoCapture(str(src));orig=cv2.VideoCapture(str(raw));W,H,rate=cc.probe(src)
        tmp=src.with_name(src.stem+'.highlight.part.mp4');before=swap.sha(src);n=0;touched=[];pending=[]
        enc=subprocess.Popen([cc.FFMPEG,'-v','error','-y','-f','rawvideo','-pix_fmt','bgr24','-s',f'{W}x{H}','-r',rate,'-i','-','-i',str(src),'-map','0:v:0','-map','1:a:0?','-c:a','copy','-c:v','libx264','-preset','slow','-crf','12','-maxrate',m['maxrate'],'-bufsize',str(2*int(m['maxrate'][:-1]))+'k','-pix_fmt','yuv420p','-movflags','+faststart',str(tmp)],stdin=subprocess.PIPE)
        def flush():
            todo=[i for i,(_,mask)in enumerate(pending)if mask is not None]
            with ThreadPoolExecutor(cl.WORKERS)as ex:fixed=dict(zip(todo,ex.map(lambda i:cl.inpaint(*pending[i]),todo)))
            for i,(frame,_)in enumerate(pending):enc.stdin.write(fixed.get(i,frame).tobytes())
            pending.clear()
        while True:
            ok,frame=cap.read();ok2,original=orig.read()
            if not ok or not ok2:
                assert ok==ok2,'Reference frame mismatch';break
            boxes=caption_backplate_boxes(original,frame,held.get(n,[]),review['region_720'])
            if boxes:touched.append({'frame':n,'boxes':boxes})
            pending.append((frame,cl.line_mask(frame.shape,boxes)if boxes else None));n+=1
            if len(pending)>=24:flush()
        flush();enc.stdin.close();enc.wait();cap.release();orig.release();assert enc.returncode==0 and n==scan['frame_count']
        if touched:
            archive=src.with_name(src.stem+'.before-backplate.mp4')
            if not archive.exists():os.replace(src,archive)
            os.replace(tmp,src);m['encodes']=m.get('encodes',1)+1
        else:tmp.unlink()
        mark={'segment':gid,'method':'reviewed_caption_backplate_only_lama','color_filter':'HSV yellow AND B<=190,G>=205,R>=225; native source caption band and OCR anchor required','review_sha256':swap.sha(run/'plan/reference_highlight_review.json'),'input_sha256':before,'output_sha256':swap.sha(src),'frames':n,'frames_repaired':len(touched),'regions':touched}
        if m.get('caption_backplate_repair'):m.setdefault('caption_backplate_repair_history',[]).append(m['caption_backplate_repair'])
        m['output_sha256']=mark['output_sha256'];m['caption_backplate_repair']=mark;swap.save(meta,m);reports.append(mark)
        swap.save(run/'qa/reference-highlight-repairs.json',reports);print(gid,'caption backplates repaired',len(touched),flush=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--lock-wait',type=float,default=18000);p.add_argument('--recheck',action='store_true');p.add_argument('--segments');a=p.parse_args();r=Path(a.run).resolve()
    with swap.flock(r/'.run.lock',False,a.lock_wait,'reference caption backplate repair'):repair(r,a.recheck,set(a.segments.split(','))if a.segments else None)
if __name__=='__main__':main()
