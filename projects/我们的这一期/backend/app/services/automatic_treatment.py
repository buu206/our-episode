"""Local, bounded dialogue/geometry treatment. Geometry is not event semantics.

The renderer consumes confirmed transcript timings. Vision supplies image-space
landmarks, never identity or a claim that someone laughed/spat. Failure to detect
is recorded; subtitles still render. All times refer to the final source edit.
"""
from pathlib import Path
import hashlib
import json
import math
import platform
import os
import shutil
import subprocess
import threading
from PIL import Image, ImageDraw, ImageFont
from . import media
from .dialogue_captions import caption_timeline

REVISION = 'automatic-treatment-v1.2'


def detect_faces(base, folder):
    if platform.system() != 'Darwin' or not shutil.which('xcrun'):
        from .portable_faces import detect
        return detect(base, folder)
    source = Path(__file__).parent/'native/face_landmarks.swift'
    # Cache code only; sampled faces stay inside the registered job workspace.
    key=hashlib.sha256(source.read_bytes()+platform.platform().encode()).hexdigest()[:20]
    cache=Path(__file__).resolve().parents[3]/'data/runtime/native'
    cache.mkdir(parents=True,exist_ok=True)
    binary=cache/('face-landmarks-'+key)
    try:
        if not binary.is_file():
            compiled=folder/'face-landmarks'
            build = subprocess.run(['xcrun','swiftc','-module-cache-path',str(folder/'module-cache'),
                str(source),'-o',str(compiled)],capture_output=True,timeout=180)
            if build.returncode:return [], 'compiler_failed'
            try:os.link(compiled,binary)
            except FileExistsError:pass
        frames = folder/'observations';frames.mkdir()
        media._run('ffmpeg',['-nostdin','-v','error','-i',str(base),'-vf',
            r'select=not(mod(n\,6)),scale=360:360:force_original_aspect_ratio=decrease',
            '-fps_mode','vfr','-q:v','3',str(frames/'%04d.jpg')],
            timeout=180,code='MEDIA_RENDER_FAILED')
        run = subprocess.run([str(binary),str(frames)],capture_output=True,timeout=180)
        if run.returncode:
            return [], 'detector_failed'
        data=json.loads(run.stdout)
        for item in data:
            item['time']=(int(item['frame'])-1)/5
            item['faces']=[f for f in item['faces'] if valid_face(f)]
        return data, 'detected'
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return [], 'detector_failed'


def valid_face(face):
    try:
        x,y,w,h=face['box']
        points=[p for eye in face['eyes'] for p in eye]+face['mouth']
        # MediaPipe exposes a configured presence threshold, not a per-face
        # probability. Do not manufacture a confidence score for that provider.
        quality = (face.get('provider') == 'mediapipe' and face.get('presence_threshold') == .8
                   if 'confidence' not in face else math.isfinite(face['confidence']) and .8 <= face['confidence'] <= 1)
        return (quality and len(face['eyes']) == 2 and all(len(eye) >= 4 for eye in face['eyes']) and
            .06 <= w <= .7 and .06 <= h <= .7 and
            0 <= x < x+w <= 1.001 and 0 <= y < y+h <= 1.001 and len(points)>12 and
            all(len(p)==2 and all(math.isfinite(v) and 0<=v<=1 for v in p) for p in points))
    except (KeyError,TypeError,ValueError):return False


def overlap(a,b):
    x,y,w,h=a;X,Y,W,H=b
    area=max(0,min(x+w,X+W)-max(x,X))*max(0,min(y+h,Y+H)-max(y,Y))
    return area/max(1e-9,w*h+W*H-area)


def face_at(observations, t, anchor):
    """No bridge across occlusion, face switches or a detector gap."""
    if not observations:return None
    pair=sorted(observations,key=lambda row:abs(row['time']-t))[:2]
    if len(pair)<2 or any(abs(row['time']-t)>.22 for row in pair):return None
    pair.sort(key=lambda row:row['time'])
    faces=[]
    for row in pair:
        candidates=[f for f in row['faces'] if overlap(f['box'],anchor)>.45]
        if not candidates:return None
        faces.append(max(candidates,key=lambda f:overlap(f['box'],anchor)))
    if overlap(faces[0]['box'],faces[1]['box'])<.6:return None
    a,b=faces;k=max(0,min(1,(t-pair[0]['time'])/max(.001,pair[1]['time']-pair[0]['time'])))
    def lerp(p,q):return [(1-k)*x+k*y for x,y in zip(p,q)]
    if any(len(x)!=len(y) for x,y in zip(a['eyes'],b['eyes'])) or len(a['mouth'])!=len(b['mouth']):return None
    return {'box':lerp(a['box'],b['box']), 'eyes':[[lerp(p,q) for p,q in zip(x,y)] for x,y in zip(a['eyes'],b['eyes'])],
            'mouth':[lerp(p,q) for p,q in zip(a['mouth'],b['mouth'])]}


def plan_effects(observations, captions, protected=(), speech_spans=None):
    def allowed(start,end):return not any(a<end and b>start for a,b in protected)
    events=[]
    # Transcript reaction anchors locate an interval, not the identity of its speaker.
    for line in captions:
        if line.get('subtitle_changed'):continue
        if not any(word in line['text'] for word in ('呛','哈哈','哎呀')):continue
        start=line['start'];end=min(start+1.1,line['end']+.35)
        rows=[r for r in observations if start+.1<=r['time']<=end-.1 and r['faces']]
        if not rows or not allowed(start,end):continue
        row=max(rows,key=lambda r:max(f['box'][2]*f['box'][3] for f in r['faces']))
        face=max(row['faces'],key=lambda f:f['box'][2]*f['box'][3])
        if face_at(observations,row['time'],face['box']) is None:continue
        if any(abs(start-e['start'])<6 for e in events):continue
        events.append(dict(kind='eyes',start=start,end=end,anchor=face['box'],
            basis='confirmed_reaction_phrase',evidence_id=line.get('evidence_id'),freeze=0))
        if len(events)>=2:break
    # A visual pause only in a transcript-free gap, at an observed open-mouth peak.
    for row in sorted(observations,key=lambda r:max((mouth_ratio(f) for f in r['faces']),default=0),reverse=True):
        t=row['time'];start=t-.3;end=t+.6
        if start<.5 or not allowed(start,end) or any(a['start']<end+.3 and a['end']>start-.3 for a in (captions if speech_spans is None else speech_spans)):continue
        if any(abs(t-e['start'])<6 for e in events):continue
        candidates=[f for f in row['faces'] if mouth_ratio(f)>.28]
        if not candidates:continue
        face=max(candidates,key=mouth_ratio)
        if face_at(observations,t,face['box']) is None:continue
        # Mouth landmarks alone cannot establish lip visibility behind a bottle.
        # Use the detected eyes; do not warp the possibly occluded mouth.
        events.append(dict(kind='eyes',start=start,end=end,anchor=face['box'],
            basis='mouth_geometry_peak_eye_emphasis',freeze=.3,peak=t))
        break
    quotes=[]
    for line in captions:
        if line.get('subtitle_changed'):continue
        context=line.get('context',line['text'])
        if any(word in context for word in ('不是','别说','别以为','没说','不一定','假如','如果','?','？')):continue
        text=next((word for word in ('吐不了','不用不用','准备','先喝口') if word in line['text']),None)
        start=line['start'];end=min(line['end']+.15,start+1.4)
        if not text or start<3.2 or not allowed(start,end) or any(abs(start-e['start'])<4 for e in quotes):continue
        nearby=[f for r in observations if start<=r['time']<=end for f in r['faces']]
        position=min((.14,.64),key=lambda y:sum(overlap([.2,y-.08,.6,.16],f['box']) for f in nearby))
        quotes.append(dict(kind='quote',text=text,start=start,end=end,freeze=0,position=position,
                           basis='verbatim_confirmed_dialogue',evidence_id=line.get('evidence_id')))
        if len(quotes)==3:break
    return sorted(events[:3]+quotes,key=lambda e:e['start'])


def mouth_ratio(face):
    points=face['mouth']
    if not points:return 0
    xs,ys=zip(*points)
    return (max(ys)-min(ys))/max(.001,max(xs)-min(xs))


def bulge(image, points, strength):
    """Smooth inverse radial warp, with no pasted rectangular face crop."""
    if len(points)<4:return image
    w,h=image.size;xs=[p[0]*w for p in points];ys=[p[1]*h for p in points]
    cx,cy=sum(xs)/len(xs),sum(ys)/len(ys)
    radius=max(max(xs)-min(xs),max(ys)-min(ys))*1.05
    if radius<4:return image
    left=max(0,int(cx-radius));top=max(0,int(cy-radius));right=min(w,int(cx+radius+1));bottom=min(h,int(cy+radius+1))
    mesh=[]
    def mapped(x,y):
        distance=math.hypot(x-cx,y-cy)/radius
        factor=1+strength*max(0,1-distance*distance)**2
        return cx+(x-cx)/factor,cy+(y-cy)/factor
    for y in range(top,bottom,5):
        for x in range(left,right,5):
            X,Y=min(right,x+5),min(bottom,y+5)
            mesh.append(((x,y,X,Y),sum((mapped(*p) for p in ((x,y),(x,Y),(X,Y),(X,y))),())))
    patch=image.transform(image.size,Image.Transform.MESH,mesh,Image.Resampling.BICUBIC)
    image.paste(patch.crop((left,top,right,bottom)),(left,top))
    return image


def subtitle(image,text):
    w,h=image.size;draw=ImageDraw.Draw(image)
    size=round(min(w,h)*.054)
    sans=Path('/System/Library/Fonts/STHeiti Medium.ttc')
    f=ImageFont.truetype(str(sans),size=size) if sans.is_file() else media._font(size,text)
    lines=media._wrap(text,draw,f,w*.86)
    # White, thick dark outline and a small warm accent. No large opaque panel.
    y=h*.88-(len(lines)-1)*size*1.3
    for line in lines:
        draw.text((w*.5,y),line,font=f,fill='#fffdf5',stroke_width=max(2,round(size*.085)),stroke_fill='#292329',anchor='mm')
        y+=size*1.3


def quote_sticker(image,event,t,observations):
    from .sticker_beats import letter, _paste
    w,h=image.size;size=round(min(w,h)*.09)
    glyphs=[letter(c,size,'#ffe55b' if i%2==0 else '#ff87ad') for i,c in enumerate(event['text'])]
    length=size*len(glyphs)
    # Choose one placement for the entire cue, avoiding per-frame corner jumps.
    cx,cy=w*.5,h*event.get('position',.14)
    layer=Image.new('RGBA',image.size);elapsed=t-event['start']
    for i,glyph in enumerate(glyphs):
        local=max(0,elapsed-i*.04)
        k=(1-math.exp(-16*local)*math.cos(18*local))*min(1,max(0,(event['end']-t)/.15))
        _paste(layer,glyph,cx+(i-(len(glyphs)-1)/2)*size,cy,k,(-1)**i*5)
    return Image.alpha_composite(image.convert('RGBA'),layer).convert('RGB')


def render_automatic(base,output,segments,temp,offset=0,observations=None):
    captions=caption_timeline(segments,offset)
    if not captions:return base,{'automatic_revision':REVISION,'subtitle_count':0,'automatic_effects':[],'detector':'no_confirmed_dialogue'}
    info=media._inspect(base);w,h=info.width,info.height
    if observations is None and info.duration < 8:observations,status=[], 'short_clip_subtitles_only'
    elif observations is None:observations,status=detect_faces(base,temp)
    else:status='provided_observations'
    from .editing import timeline
    protected=[(offset+t.start,offset+t.end) for s,t in zip(segments,timeline(segments)) if s.get('_preserve_expression') or s.get('visual_beats')]
    from .editorial_plan import compile_editorial
    protected += [(row['start'],row['end']) for row in compile_editorial(segments,offset) if row['beat'].treatment!='none']
    protected += [(0,offset)] if offset else []
    speech_spans=[{'start':offset+t.start+a-s['start'],'end':offset+t.start+b-s['start']}
        for s,t in zip(segments,timeline(segments)) for a,b in s.get('_speech_intervals',[])]
    events=plan_effects(observations,captions,protected,speech_spans or None)
    decode=[media._binary('ffmpeg'),'-nostdin','-v','error','-i',str(base),'-vf','fps=30','-f','rawvideo','-pix_fmt','rgb24','pipe:1']
    encode=[media._binary('ffmpeg'),'-nostdin','-v','error','-n','-f','rawvideo','-pixel_format','rgb24','-video_size',f'{w}x{h}',
        '-framerate','30','-i','pipe:0','-i',str(base),'-map','0:v','-map','1:a:0','-c:v','libx264','-preset','veryfast',
        '-crf','20','-threads','2','-pix_fmt','yuv420p','-c:a','copy','-t',str(info.duration),'-map_metadata','-1','-movflags','+faststart',str(output)]
    rendered={i:0 for i in range(len(events))};frozen={};index=0
    # No subprocess error text (potential source paths) enters user logs.
    with (temp/'decode-error.txt').open('wb') as de,(temp/'encode-error.txt').open('wb') as ee:
        decoder=subprocess.Popen(decode,stdout=subprocess.PIPE,stderr=de)
        encoder=subprocess.Popen(encode,stdin=subprocess.PIPE,stderr=ee)
        def stop_stalled():
            for process in (decoder,encoder):
                if process.poll() is None:
                    try:process.kill()
                    except ProcessLookupError:pass
        deadline=threading.Timer(180+info.duration*6,stop_stalled)
        deadline.daemon=True;deadline.start()
        try:
            while True:
                raw=decoder.stdout.read(w*h*3)
                if not raw:break
                if len(raw)!=w*h*3:raise ValueError('Incomplete decoded frame')
                image=Image.frombytes('RGB',(w,h),raw);t=index/30
                for i,event in enumerate(events):
                    if not event['start']<=t<event['end']:continue
                    if event['kind']=='quote':
                        image=quote_sticker(image,event,t,observations);rendered[i]+=1;continue
                    ft=t
                    if event['freeze'] and event['peak']<=t<event['peak']+event['freeze']:
                        if i not in frozen:frozen[i]=(image.copy(),t)
                        image,ft=frozen[i][0].copy(),frozen[i][1]
                    face=face_at(observations,ft,event['anchor'])
                    if face is None:continue
                    envelope=min(1,(t-event['start'])/.15,(event['end']-t)/.15)
                    for points in (face['eyes'] if event['kind']=='eyes' else [face['mouth']]):
                        image=bulge(image,points,.65*max(0,envelope))
                    # Short rays follow measured geometry, leaving the dialogue lane free.
                    x,y,fw,fh=face['box'];draw=ImageDraw.Draw(image)
                    for side in (-1,1):
                        cx=(x+fw*(.5+side*.7))*w;cy=(y+fh*.35)*h
                        draw.line((cx,cy,cx+side*16*envelope,cy-12*envelope),fill='#ffdb66',width=max(2,w//180))
                    rendered[i]+=1
                active=[c for c in captions if c['start']<=t<c['end']]
                if active:subtitle(image,' / '.join(dict.fromkeys(c['text'] for c in active)))
                encoder.stdin.write(image.tobytes());index+=1
            encoder.stdin.close()
            if decoder.wait(timeout=30) or encoder.wait(timeout=60):raise ValueError('Encoding failed')
        except BaseException:
            decoder.kill();encoder.kill();decoder.wait();encoder.wait();raise
        finally:
            deadline.cancel()
    result=media.probe(output)
    if abs(result['duration']-info.duration)>.12 or not result['has_audio']:raise ValueError('Output mismatch')
    return output,{'automatic_revision':REVISION,'subtitle_count':len(captions),'detector':status,
        'observed_frames':len(observations),'automatic_effects':[{**e,'rendered_frames':rendered[i]} for i,e in enumerate(events)],
        'mouth_warp':'disabled_without_occlusion_evidence',
        'source_digest':hashlib.sha256(base.read_bytes()).hexdigest()}
