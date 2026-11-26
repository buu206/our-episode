"""Local typography placement, not action recognition or subject tracking.

Sampled texture/motion ranks whitespace. Detected face boxes and actual subtitle
bounds exclude slots. Missing detector coverage or no free slot omits the accent.
No coordinates are requested from the language model.
"""
import io
from pathlib import Path
from PIL import Image,ImageFilter,ImageStat,ImageChops
from . import media
from .editorial_plan import compile_editorial
from .editing import VisualBeat

PALETTE={'setup':'coral','action':'amber','reaction':'mint','closing':'mint'}


def intersects(a,b):
    return min(a[2],b[2])>max(a[0],b[0]) and min(a[3],b[3])>max(a[1],b[1])


def choose_slot(frames,faces,text,width,height,safe_bottom,previous=None):
    """One stable box for the whole cue; reject face overlap at any sampled time."""
    # One smaller legible size is a fallback, never unlimited shrinking.
    for scale in (.65,.55):
        size=min(106*min(width,height)/720*scale,width*.75/max(1,len(text)))
        halfw=(len(text)*size*.60+size*.30)/width
        halfh=size*1.12/height
        if halfw>.48 or halfh>.2:continue
        xs=sorted(set((max(.22,halfw+.025),.5,min(.78,1-halfw-.025))))
        options=[]
        edges=[f.convert('L').filter(ImageFilter.FIND_EDGES) for f in frames]
        for cy in (max(halfh+.025,.10),.14,.38,.61):
            for cx in xs:
                box=(cx-halfw,cy-halfh,cx+halfw,cy+halfh)
                if box[0]<.015 or box[2]>.985 or box[1]<.02 or box[3]>safe_bottom:continue
                if any(intersects(box,f) for f in faces):continue
                crops=[(round(box[0]*f.width),round(box[1]*f.height),round(box[2]*f.width),round(box[3]*f.height)) for f in frames]
                texture=max(ImageStat.Stat(e.crop(c)).mean[0]/255 for e,c in zip(edges,crops))
                movement=max((ImageStat.Stat(ImageChops.difference(frames[i].convert('L'),frames[i-1].convert('L')).crop(crops[i])).mean[0]/255 for i in range(1,len(frames))),default=0)
                # This is a geometric readability heuristic, not a promise that a
                # low-texture object is unimportant. Keep uncertainty in metadata.
                if texture>.17 or movement>.25:continue
                reuse=.012 if previous and abs(cx-previous[0])<.1 and abs(cy-previous[1])<.1 else 0
                options.append((texture+movement*.6+reuse,cy,cx,box))
        if not options:continue
        score,cy,cx,box=min(options)
        return {'cx':cx,'cy':cy,'box':list(box),'scale':scale,'score':round(score,4)}
    return None


def prepare_layout(base,segments,temp,offset=0):
    plan=compile_editorial(segments,offset)
    items=[x for x in plan if x['beat'].layout=='adaptive']
    if not items:return {}
    from .automatic_treatment import detect_faces,subtitle_layout
    from .dialogue_captions import caption_timeline
    folder=temp/'editorial-layout';folder.mkdir()
    observations,status=detect_faces(base,folder)
    info=media._inspect(base);w,h=info.width,info.height
    captions=caption_timeline(segments,offset)
    result={};previous=None
    for item in items:
        key=item['segment'];beat=item['beat'];start,end=item['start'],item['end']
        record={'status':'skipped','detector':status,'palette':PALETTE[beat.role]}
        result[key]=record
        near=[row for row in observations if start-.25<=row['time']<=end+.25]
        if status not in ('detected','detected_mediapipe') or not near or near[0]['time']>start+.3 or near[-1]['time']<end-.3:
            record['reason']='face_check_unavailable';continue
        boxes=[]
        for row in near:
            for face in row['faces']:
                x,y,fw,fh=face['box'];boxes.append((x-fw*.15,y-fh*.2,x+fw*1.15,y+fh*1.2))
        texts=[c['text'] for c in captions if c['start']<end+.5 and c['end']>start-.5]
        safe=.73
        if texts:
            _,_,size,y=subtitle_layout(w,h,' / '.join(dict.fromkeys(texts)))
            safe=min(safe,max(0,(y-size*.62-12)/h))
        frames=[]
        for at in (start+.04,(start+end)/2,end-.04):
            raw=media._run('ffmpeg',['-nostdin','-v','error','-ss',str(at),'-i',str(base),
                '-frames:v','1','-vf','scale=320:320:force_original_aspect_ratio=decrease',
                '-f','image2pipe','-vcodec','png','-'],timeout=30,code='MEDIA_RENDER_FAILED')
            with Image.open(io.BytesIO(raw)) as image:frames.append(image.convert('RGB'))
        slot=choose_slot(frames,boxes,beat.text,w,h,safe,previous)
        if slot is None:record['reason']='no_readable_free_slot';continue
        previous=(slot['cx'],slot['cy'])
        record.update(status='placed',slot=slot,safe_bottom=round(safe*h),
                      reason='sampled_whitespace_outside_detected_faces_and_subtitles')
    return result


def graphic_beat(beat,layout):
    slot=layout['slot'];cx,cy=slot['cx'],slot['cy']
    return VisualBeat(kind='headline',source_time=beat.source_time,duration=beat.duration,
        text=beat.text,palette=layout['palette'],scale=slot['scale'],sound='none',
        target={'x':cx-.04,'y':cy-.04,'width':.08,'height':.08})


def sound_cues(plan,layouts):
    cues=[];last=-100
    for item in plan:
        beat=item['beat']
        if beat.treatment=='none' or beat.sound=='none':continue
        if beat.layout=='adaptive' and layouts.get(item['segment'],{}).get('status')!='placed':continue
        if beat.sound=='legacy':cues.append((item['start']+.08,'stamp' if beat.treatment=='chapter' else 'aside'));continue
        at=item['start']+beat.sound_delay
        # New automatic accents are sparse even if a model over-requests sound.
        if at-last<5 or len([c for c in cues if c[1] in ('wood','resolve')])>=2:continue
        cues.append((at,beat.sound));last=at
    return cues
