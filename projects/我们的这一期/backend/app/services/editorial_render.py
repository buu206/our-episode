"""Execute bounded editorial choices: whole-frame card, short accent, or silence.

Whole-frame cards fit the entire moving source into a designed frame. They do
not crop a face, fabricate an event, extend a shot, or replace its sound.
"""
from pathlib import Path
import math
from PIL import Image, ImageDraw
from . import media
from .editorial_plan import compile_editorial
from .sticker_beats import font, letter, _paste, _pop, INK, PINK, YELLOW


def chapter_layout(width, height):
    if height > width:
        box=(width*.14,height*.23,width*.72,height*.61)
    else:
        box=(width*.035,height*.16,width*.60,height*.76)
    x,y,w,h=box
    fit=min(w/width,h/height)
    vw,vh=int(width*fit)//2*2,int(height*fit)//2*2
    return round(x+(w-vw)/2),round(y+(h-vh)/2),vw,vh


def _words(image, text, cx, cy, size, max_width, color, tilt=0):
    size=min(size, max_width/max(1,len(text))*.96)
    glyphs=[letter(c,max(12,round(size)),color) for c in text]
    for i,glyph in enumerate(glyphs):
        _paste(image,glyph,cx+(i-(len(glyphs)-1)/2)*size,cy,angle=tilt*((-1)**i))


def chapter_card(beat,width,height):
    image=Image.new('RGBA',(width,height),'#fff0bd')
    d=ImageDraw.Draw(image);u=min(width,height)/720
    d.ellipse((-width*.35,-height*.13,width*.55,height*.20),fill='#ff9bb1')
    d.ellipse((width*.60,height*.68,width*1.35,height*1.12),fill='#ffcd58')
    for x in range(-height,width+height,round(42*u)):
        d.line((x,height*.94,x+height*.13,height),fill='#f5b94e',width=max(1,round(3*u)))
    portrait=height>width
    cx=width*.5 if portrait else width*.81
    cap_width=width*.83 if portrait else width*.32
    label='现场说明' if beat.role=='setup' else '本段收尾'
    f=font(round(24*u));label_y=height*(.055 if portrait else .20)
    d.text((cx,label_y),'在场 / '+label,font=f,fill=INK,anchor='mt')
    title_y=height*(.14 if portrait else .37)
    _words(image,beat.text,cx,title_y,90*u,cap_width,PINK,4)
    x,y,w,h=chapter_layout(width,height)
    pad=round(10*u)
    d.rounded_rectangle((x-pad+6*u,y-pad+9*u,x+w+pad+6*u,y+h+pad+9*u),radius=round(12*u),fill=INK)
    d.rounded_rectangle((x-pad,y-pad,x+w+pad,y+h+pad),radius=round(12*u),fill='#fffdf7')
    # Bounded multiline explanation; no automatic paraphrase of the approved text.
    detail_y=height*(.90 if portrait else .55)
    detail_font=font(round(34*u))
    lines=media._wrap(beat.detail,d,detail_font,cap_width-20*u)
    for i,line in enumerate(lines):
        d.text((cx,detail_y+i*43*u),line,font=detail_font,fill=INK,anchor='mt')
    return image


def accent_frames(beat,folder,width,height):
    folder.mkdir()
    u=min(width,height)/720
    size=min(100*u,width*.7/max(1,len(beat.text)))
    # A screen-space lower margin, not guessed face coordinates. Reaction text
    # is a source-linked quotation/annotation, never a fabricated inner thought.
    cx,cy=width*.5,height*.79
    colors=(YELLOW,'#ff8eac','#8ee5e1') if beat.role=='reaction' else (PINK,'#5483cb',PINK)
    glyphs=[letter(c,round(size),colors[i%len(colors)]) for i,c in enumerate(beat.text)]
    for n in range(math.ceil(beat.duration*30)):
        t=n/30;image=Image.new('RGBA',(width,height))
        opacity=min(1,max(0,(beat.duration-t)/.12))
        for i,glyph in enumerate(glyphs):
            # Slide-and-settle, not the same spring preset on every type of beat.
            delay=.035*i
            k=_pop(t-delay) if beat.role=='reaction' else min(1,max(0,(t-delay)*8))
            _paste(image,glyph,cx+(i-(len(glyphs)-1)/2)*size*.96+32*u*math.exp(-14*max(0,t-delay)),
                   cy+((-1)**i)*size*.06,k,(-1)**i*5,opacity)
        image.save(folder/f'{n:04d}.png')
    return folder/'%04d.png'


def intro_frames(beat, folder, width, height):
    """Keep the source full frame throughout; only the title enters/leaves."""
    folder.mkdir()
    u=min(width,height)/720
    card=Image.new('RGBA',(width,round(190*u)))
    draw=ImageDraw.Draw(card)
    draw.rounded_rectangle((width*.1,20*u,width*.9,160*u),radius=24*u,fill='#ffdc70')
    _words(card,beat.text,width*.5,67*u,58*u,width*.73,INK,2)
    draw.text((width*.5,118*u),beat.detail,font=font(round(min(27*u,width*.73/max(1,len(beat.detail))))),fill=INK,anchor='mm')
    for n in range(math.ceil(beat.duration*30)):
        t=n/30
        alpha=min(1,t/.18,max(0,(beat.duration-t)/.25))
        tile=card.copy();tile.putalpha(tile.getchannel('A').point(lambda a:round(a*alpha)))
        image=Image.new('RGBA',(width,height))
        image.alpha_composite(tile,(0,round(height*.045-30*u*(1-alpha))))
        image.save(folder/f'{n:04d}.png')
    return folder/'%04d.png'


def render_editorial(base:Path,output:Path,segments,temp:Path,offset=0,layouts=None):
    plan=compile_editorial(segments,offset)
    if layouts is None:
        from .editorial_layout import prepare_layout
        layouts=prepare_layout(base,segments,temp,offset)
    active=[x for x in plan if x['beat'].treatment!='none' and
            (x['beat'].layout!='adaptive' or layouts.get(x['segment'],{}).get('status')=='placed')]
    summary=[{'segment':x['segment'],'role':x['beat'].role,'treatment':x['beat'].treatment,
              'start':round(x['start'],3),'end':round(x['end'],3),
              **({'layout':layouts[x['segment']]} if x['segment'] in layouts else {})} for x in plan]
    if not active:
        return base,{'editorial_revision':'editorial-v3.1-vivid','editorial_summary':summary}
    info=media._inspect(base);w,h=info.width,info.height
    if output.exists() or output.is_symlink():
        raise media.MediaError('MEDIA_OUTPUT_EXISTS','目标已存在，请另存新版本。')
    if any(x['end']>info.duration+.04 for x in active):
        raise media.MediaError('INVALID_EDITORIAL','编导安排超出成片。')
    filters=['[0:v]null[v0]']
    args=['-nostdin','-v','error','-xerror','-n','-threads','2','-filter_complex_threads','1',
          '-protocol_whitelist','file,pipe','-i',str(base)]
    for i,item in enumerate(active):
        beat,start,end=item['beat'],item['start'],item['end']
        if beat.layout=='adaptive':
            from .editorial_layout import graphic_beat
            from .sticker_beats import render_frames
            layout=layouts[item['segment']]
            graphic=render_frames(graphic_beat(beat,layout),base,temp/f'editorial-accent-{i}',w,h,
                                  safe_bottom=layout['safe_bottom'])
        else:
            graphic=(intro_frames(beat,temp/f'editorial-intro-{i}',w,h) if beat.treatment=='chapter'
                     else accent_frames(beat,temp/f'editorial-accent-{i}',w,h))
        args+=['-framerate','30','-t',str(beat.duration),'-i',str(graphic)]
        filters.append(f'[{i+1}:v]format=rgba,setpts=PTS-STARTPTS+{start:.6f}/TB[g{i}]')
        filters.append(f'[v{i}][g{i}]overlay=eof_action=pass:repeatlast=0[v{i+1}]')
    args+=['-filter_complex',';'.join(filters),'-map',f'[v{len(active)}]','-map','0:a:0',
           '-t',str(info.duration),'-c:v','libx264','-preset','veryfast','-crf','20','-threads','2',
           '-pix_fmt','yuv420p','-c:a','copy','-map_metadata','-1','-movflags','+faststart',str(output)]
    media._run('ffmpeg',args,timeout=90+info.duration*8,code='MEDIA_RENDER_FAILED')
    result=media.probe(output)
    if abs(result['duration']-info.duration)>.12 or (result['width'],result['height'])!=(w,h) or not result['has_audio']:
        raise media.MediaError('MEDIA_RENDER_FAILED','编导版式的时长、画幅或音轨校验失败。')
    return output,{'editorial_revision':'editorial-v3.1-vivid','editorial_summary':summary}
