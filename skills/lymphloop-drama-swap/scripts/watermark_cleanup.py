#!/usr/bin/env python3
"""Local, frame-indexed overlay cleanup. OCR proposals require actual overlay review."""
from __future__ import annotations
import argparse,hashlib,json,subprocess,tempfile,shutil,math
from pathlib import Path
import media_pipeline as media

def ref(path):
    p=Path(path).resolve();return {'path':str(p),'sha256':sha(p)}
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()
def read(p):return json.loads(Path(p).read_text())
def audio_hash(p):
    return media.run([media.binary('ffmpeg'),'-v','error','-i',p,'-map','0:a:0','-c','copy','-f','hash','-hash','sha256','-']).strip()
def bound(value,roots=None):
    p=Path(value.get('path','')).resolve()
    if roots and not any(p==Path(r).resolve() or Path(r).resolve() in p.parents for r in roots):raise ValueError('Watermark dependency outside this video workspace')
    if not p.is_file() or sha(p)!=value.get('sha256'):raise ValueError('Watermark dependency missing or changed')
    return p

import watermark_brand as _wb


def validate_scan(scan,info,source):
    if scan.get('type')!='lymphloop_watermark_scan' or not scan.get('target') or scan.get('source')!=source:
        raise ValueError('Watermark scan must bind this exact full video and target')
    _wb.set_target(scan['target'])   # any competitor's watermark word (2026-11-11)
    n=info['frame_count']
    full = scan.get('scanned_frames') == n and scan.get('coverage') == 'every_frame'
    # The supported fast scanner searches the entire source every fourth frame, then decodes
    # every frame around each hit. Preserve its truthful coverage metadata rather than relabel
    # its count as a frame-by-frame scan (workflow changed on 2026-11-19).
    import re
    match = re.fullmatch(r'every ([1-4])th frame of \[\(0, (\d+)\)\], every frame within (\d+) of a hit', str(scan.get('coverage', '')))
    fast = bool(match and int(match[2]) == n and int(match[3]) >= 12
                and isinstance(scan.get('scanned_frames'), int)
                and scan['scanned_frames'] >= math.ceil(n / int(match[1]))
                and isinstance(scan.get('sparse_hits'), int) and scan['sparse_hits'] >= 0
                and isinstance(scan.get('dense_ranges'), list)
                and all(isinstance(r, (list, tuple)) and len(r) == 2 and all(type(v) is int for v in r)
                        and 0 <= r[0] < r[1] <= n for r in scan['dense_ranges']))
    if scan.get('frame_count') != n or not (full or fast):
        raise ValueError('Watermark scan must cover the full video, either every frame or full-range adaptive dense scanning')
    if not isinstance(scan.get('detections'),list):raise ValueError('Explicit watermark detections required')
    for item in scan['detections']:
        if type(item.get('frame'))is not int or not 0<=item['frame']<n:raise ValueError('Watermark frame outside video')
        # fuzzy: faint marks OCR as LYMPHORTA / LYMRHORIA / MPHORIA (watermark_brand.is_watermark_text); kept labels are not checked
        if item.get('classification')=='floating_overlay' and not _wb.is_watermark_text(item.get('text','')):raise ValueError('Detection must identify the requested text')
        rect=item.get('bbox');w=info['video']['width'];h=info['video']['height']
        if not isinstance(rect,list) or len(rect)!=4 or any(type(v)is not int for v in rect) or min(rect[2:])<=0 or min(rect[:2])<0 or rect[0]+rect[2]>w or rect[1]+rect[3]>h:raise ValueError('Watermark mask outside picture')

# Agents write natural variants (Codex used 'caption_keep', 2026-10-25); map them instead of crashing the apply.
CLASS_ALIASES={'caption_keep':'original_caption_keep','original_caption':'original_caption_keep','caption':'original_caption_keep','printed_label':'printed_label_keep','label_keep':'printed_label_keep','overlay':'floating_overlay','watermark':'floating_overlay'}



def normalize_class(c):
    """Map an agent's wording onto the three valid classes by meaning (caption_keep, subtitle_keep, label_keep ...)."""
    if c in CLASS_ALIASES: return CLASS_ALIASES[c]
    t=str(c or '').lower()
    if t in ('floating_overlay','printed_label_keep','original_caption_keep'): return t
    if 'keep' in t or 'ignore' in t or 'not_overlay' in t:
        if 'caption' in t or 'subtitle' in t or 'title' in t: return 'original_caption_keep'
        if 'label' in t or 'print' in t or 'packag' in t or 'product' in t: return 'printed_label_keep'
    if 'overlay' in t or 'watermark' in t: return 'floating_overlay'
    return c


def overlay_groups(scan):
    """Use the measured stamp footprint, retaining oversized OCR only as evidence."""
    groups = {}
    for detection in scan['detections']:
        c = normalize_class(detection.get('classification'))
        if c == 'floating_overlay':
            place = _wb.position_of(detection['bbox'], scan.get('positions', []))
            box = list(place['box'] if place else detection['bbox'])
            boxes = groups.setdefault(detection['frame'], [])
            if box not in boxes:
                boxes.append(box)
        elif c not in {'printed_label_keep', 'original_caption_keep'}:
            raise ValueError(f"Detection {detection.get('frame')} has classification {detection.get('classification')!r}")
    return groups


def render(source,scan_path,output,report):
    import cv2,numpy as np
    source=Path(source).resolve();output=Path(output).resolve();report=Path(report).resolve()
    if source==output:raise ValueError('Keep the scanned source immutable; cleanup creates a verified derivative')
    info=media.probe(str(source),include_frames=True);media.require_cfr(info);scan=read(scan_path);validate_scan(scan,info,ref(source))
    if not scan.get('reviewer') or not scan.get('review_note') or scan.get('overlay_classification_reviewed')is not True:raise ValueError('Review OCR matches as floating overlays; preserve printed product labels')
    groups=overlay_groups(scan)
    output.parent.mkdir(parents=True,exist_ok=True);report.parent.mkdir(parents=True,exist_ok=True)
    w,h=info['video']['width'],info['video']['height'];rate=info['fps']
    if not groups:shutil.copyfile(source,output)
    else:
        with tempfile.TemporaryDirectory(prefix='lymphloop-watermark-',dir=output.parent) as tmp:
            picture=Path(tmp)/'picture.mp4'
            dec=subprocess.Popen([media.binary('ffmpeg'),'-v','error','-i',str(source),'-map','0:v:0','-f','rawvideo','-pix_fmt','bgr24','pipe:1'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            enc=subprocess.Popen([media.binary('ffmpeg'),'-nostdin','-y','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s',f'{w}x{h}','-r',rate,'-i','pipe:0','-an','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',str(picture)],stdin=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                for i in range(info['frame_count']):
                    data=bytearray();needed=w*h*3
                    while len(data)<needed:
                        block=dec.stdout.read(needed-len(data))
                        if not block:raise ValueError('Unexpected video decode EOF')
                        data.extend(block)
                    frame=np.frombuffer(data,dtype=np.uint8).reshape(h,w,3)
                    if i in groups:
                        mask=np.zeros((h,w),np.uint8)
                        for x,y,bw,bh in groups[i]:mask[y:y+bh,x:x+bw]=255
                        frame=cv2.inpaint(frame,mask,3,cv2.INPAINT_TELEA)
                    enc.stdin.write(frame.tobytes())
                enc.stdin.close();enc.wait();dec.stdout.close();dec.wait()
                if enc.returncode or dec.returncode:raise ValueError('Watermark decode/encode failed')
            finally:
                for proc in [enc,dec]:
                    if proc.poll()is None:proc.terminate();proc.wait()
                    if proc.stderr:proc.stderr.close()
                    if proc.stdout and not proc.stdout.closed:proc.stdout.close()
                    if proc.stdin and not proc.stdin.closed:proc.stdin.close()
            media.ff(['-y','-i',str(picture),'-i',str(source),'-map','0:v:0','-map','1:a?','-c','copy','-movflags','+faststart',str(output)])
    out=media.probe(str(output),include_frames=True);media.require_cfr(out)
    if out['frame_count']!=info['frame_count'] or media.ratio(out['fps'])!=media.ratio(info['fps']) or out['video']['width']!=w or out['video']['height']!=h:raise ValueError('Watermark repair changed the timeline or picture geometry')
    if info['audio'] and audio_hash(str(source))!=audio_hash(str(output)):raise ValueError('Watermark repair changed the audio bitstream')
    media.ff(['-xerror','-i',str(output),'-f','null','-'])
    tech=report.with_name(report.stem+'.technical.json')
    media.save(tech,{'path':str(output),'full_decode_passed':True,'frame_count':out['frame_count'],'fps':out['fps'],'has_audio':bool(out['audio']),'audio_bitstream_preserved':True})
    media.save(report,{'type':'lymphloop_watermark_cleanup','target':read(scan_path).get('target'),'source':ref(source),'output':ref(output),'scan':ref(scan_path),'technical_verification':ref(tech),'modified_frames':len(groups),
                      'method':'opencv_telea_per_frame' if groups else 'no_overlay_copy','mask_policy':'measured_fixed_positions_with_unmatched_ocr_fallback','review':None,'post_scan':None})
    return read(report)

def validate(proof,source,roots):
    if proof.get('type')!='lymphloop_watermark_cleanup' or not proof.get('target') or proof.get('source')!=source:raise ValueError('Watermark cleanup must bind this exact scanned source video')
    refs=[proof.get(k,{}) for k in ('source','output','scan','technical_verification','review','post_scan')]
    paths=[bound(x,roots) for x in refs];info=media.probe(str(paths[0]),include_frames=True);out=media.probe(str(paths[1]),include_frames=True)
    media.require_cfr(info);media.require_cfr(out);scan=read(paths[2]);validate_scan(scan,info,source)
    if not scan.get('overlay_classification_reviewed') or not scan.get('reviewer') or not scan.get('review_note'):raise ValueError('Initial watermark detections need reviewed overlay classification')
    if any(x.get('classification') not in {'floating_overlay','printed_label_keep','original_caption_keep'} for x in scan['detections']):raise ValueError('Unresolved watermark candidates remain')
    if info['frame_count']!=out['frame_count'] or media.ratio(info['fps'])!=media.ratio(out['fps']) or [(info['video'][k]) for k in ('width','height')]!=[(out['video'][k]) for k in ('width','height')]:raise ValueError('Watermark cleanup changed timing or geometry')
    tech=read(paths[3])
    if tech.get('path')!=str(paths[1]) or tech.get('full_decode_passed')is not True or tech.get('frame_count')!=out['frame_count'] or tech.get('audio_bitstream_preserved')is not True:raise ValueError('Cleaned master technical verification missing')
    if bool(info['audio'])!=bool(out['audio']) or (info['audio'] and audio_hash(str(paths[0]))!=audio_hash(str(paths[1]))):raise ValueError('Final cleanup cannot alter original final audio')
    review=read(paths[4]);post=read(paths[5]);validate_scan(post,out,proof['output'])
    if review.get('type')!='lymphloop_watermark_review' or review.get('source')!=source or review.get('output')!=proof['output'] or not review.get('reviewer') or not review.get('review_note') or not all(review.get(k)is True for k in ['floating_watermark_absent','repair_regions_checked','faces_hands_packaging_preserved','other_text_preserved']):raise ValueError('Actual watermark removal and repair-impact review required')
    if any(x.get('classification') not in {'printed_label_keep','original_caption_keep'} for x in post['detections']):raise ValueError('Floating competitor watermark still detected or not reviewed after cleanup')
    evidence=review.get('evidence',[])
    if not evidence:raise ValueError('Watermark review must include actual frame evidence')
    for e in evidence:bound(e,roots)
    refs.extend(evidence)
    return refs

def preflight():
    """Report whether this interpreter can scan/inpaint; the agent copies the result into runtime.json."""
    result={'python':__import__('sys').executable}
    for name in ('cv2','numpy'):
        try:module=__import__(name);result[name]=getattr(module,'__version__',True)
        except ImportError:result[name]=None
    result['swiftc']=shutil.which('swiftc')
    result['watermark_tools_ready']=bool(result['cv2'] and result['numpy'])
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['render','preflight']);p.add_argument('--source');p.add_argument('--scan');p.add_argument('--output');p.add_argument('--report');a=p.parse_args()
    if a.command=='preflight':print(json.dumps(preflight(),ensure_ascii=False,indent=2))
    else:
        if not all([a.source,a.scan,a.output,a.report]):p.error('render requires --source --scan --output --report')
        print(json.dumps(render(a.source,a.scan,a.output,a.report),ensure_ascii=False,indent=2))
