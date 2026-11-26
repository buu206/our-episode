"""Reviewed freeze-cutouts and kinetic lettering, rendered as transparent frames.

Typography/outline layering adapted from motion-palette-15 (MIT; bundled notice).
No segmentation model: the contour belongs to the exact source_time frame.
The original moving shot and its audio continue beneath this graphic sticker.
"""
from __future__ import annotations

import io
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import media
from .editing import VisualBeat

INK = '#262142'
PINK = '#ff5292'
YELLOW = '#ffe55b'
FONT = Path(__file__).resolve().parents[1] / 'assets/fonts/ZCOOLKuaiLe-Regular.ttf'
PALETTES = {'candy':(YELLOW,'#ff90b5',YELLOW,'#82e8e6'),
            'amber':('#ff9500','#f14a16'), 'mint':('#00ab80','#007db6'),
            'coral':('#f52570','#c92f91')}


@lru_cache(maxsize=32)
def font(size):
    return ImageFont.truetype(str(FONT), size=max(8, size))


def letter(char: str, size: int, color: str, *, vivid: bool = False) -> Image.Image:
    """Fat letter + deep extrusion + colour outer edge + white inner edge."""
    f = font(size)
    box = f.getbbox(char)
    margin = max(10, round(size*.23))
    mask = Image.new('L', (box[2]-box[0]+margin*2, box[3]-box[1]+margin*2))
    ImageDraw.Draw(mask).text((margin-box[0], margin-box[1]), char, font=f, fill=255,
                              stroke_width=max(1, round(size*.012)))
    def grown(radius):
        return mask.filter(ImageFilter.MaxFilter(2*max(1, round(radius))+1))
    out = Image.new('RGBA', mask.size)
    edge = grown(size*(.12 if vivid else .105))
    shadow = Image.new('RGBA', mask.size, INK); shadow.putalpha(edge)
    out.alpha_composite(shadow, (round(size*.04), round(size*.065)))
    for alpha, c in ((edge,INK),(grown(size*.075),color),(grown(size*(.025 if vivid else .035)),'#fffaf1')):
        p = Image.new('RGBA', mask.size,c); p.putalpha(alpha); out.alpha_composite(p)
    fill = Image.new('RGBA',mask.size,color)
    # Subtle light at the top; keep the letter itself saturated and readable.
    overlay = Image.new('RGBA',mask.size); d=ImageDraw.Draw(overlay)
    for y in range(mask.height):
        d.line((0,y,mask.width,y),fill=(255,255,255,round((24 if vivid else 90)*max(0,1-y/mask.height))))
    fill=Image.alpha_composite(fill,overlay); fill.putalpha(mask); out.alpha_composite(fill)
    return out


def cutout(frame: Image.Image, beat: VisualBeat) -> Image.Image:
    """Mask only the visible reviewed silhouette; never hallucinate hidden parts."""
    if beat.kind != 'cutout' or not beat.contour:
        raise ValueError('reviewed silhouette required')
    frame=frame.convert('RGBA')
    mask=Image.new('L',frame.size)
    ImageDraw.Draw(mask).polygon([(p.x*frame.width,p.y*frame.height) for p in beat.contour],fill=255)
    bounds=mask.getbbox()
    if not bounds:
        raise ValueError('empty silhouette')
    frame.putalpha(mask.filter(ImageFilter.GaussianBlur(.55)))
    return frame.crop(bounds)


def sticker(image: Image.Image, height: int) -> Image.Image:
    scale=height/image.height
    image=image.resize((max(1,round(image.width*scale)),height),Image.Resampling.LANCZOS)
    pad=max(12,round(height*.055))
    src=Image.new('RGBA',(image.width+2*pad,image.height+2*pad));src.alpha_composite(image,(pad,pad))
    mask=src.getchannel('A');out=Image.new('RGBA',src.size)
    # Actual alpha silhouette dilation, not a rectangle/circular magnifier.
    for radius,color in ((pad*.8,INK),(pad*.62,PINK),(pad*.27,'#fff8ed')):
        layer=Image.new('RGBA',src.size,color)
        layer.putalpha(mask.filter(ImageFilter.MaxFilter(2*max(1,round(radius))+1)))
        out.alpha_composite(layer)
    out.alpha_composite(src)
    return out


def _pop(t):
    return 0 if t <= 0 else max(0,1-math.exp(-12*t)*math.cos(19*t))


def _paste(canvas, image, x, y, scale=1, angle=0, alpha=1):
    if scale < .015 or alpha <= 0: return
    p=image.resize((max(1,round(image.width*scale)),max(1,round(image.height*scale))),Image.Resampling.BICUBIC)
    if angle: p=p.rotate(angle,Image.Resampling.BICUBIC,expand=True)
    if alpha < 1:p.putalpha(p.getchannel('A').point(lambda a:round(a*alpha)))
    canvas.alpha_composite(p,(round(x-p.width/2),round(y-p.height/2)))


def render_frames(beat: VisualBeat, source: Path, folder: Path, width: int, height: int, *, safe_bottom=None) -> Path:
    """Bounded 30 fps sequence. Contents survive only in the render temp directory."""
    folder.mkdir(exist_ok=False)
    unit=min(width,height)/720*beat.scale
    subject=None
    if beat.kind=='cutout':
        data=media._run('ffmpeg',['-v','error','-nostdin','-protocol_whitelist','file,pipe',
            '-ss',str(beat.source_time),'-i',str(source),'-frames:v','1','-vf','scale=720:1280:force_original_aspect_ratio=decrease',
            '-f','image2pipe','-vcodec','png','-'],timeout=30,code='MEDIA_RENDER_FAILED')
        with Image.open(io.BytesIO(data)) as raw:
            subject=sticker(cutout(raw,beat),round(min(height*.35,width*.65)*beat.scale))
    chars=list(beat.text)
    size=round(min(106*unit,width*.75/max(1,len(chars))))
    if subject:size=round(min(78*unit,width*.5/max(1,len(chars))))
    colors=PALETTES[beat.palette]
    letters=[letter(c,size,colors[i%len(colors)],vivid=beat.palette!='candy') for i,c in enumerate(chars)]
    left=beat.position.endswith('left')
    top=beat.position.startswith('top')
    cx=width*(.30 if left else .70) if subject else width*(beat.target.x+beat.target.width/2)
    cy=height*(.30 if top else .70) if subject else height*(beat.target.y+beat.target.height/2)
    if not subject:
        margin=min(width*.48,len(chars)*size*.52+size*.22)
        cx=max(margin,min(width-margin,cx))
        cy=max(size*.85,min(height-size*.85,cy))
    if subject:
        cx=max(subject.width*.65,min(width-subject.width*.65,cx))
    for n in range(math.ceil(beat.duration*30)):
        t=n/30
        layer=Image.new('RGBA',(width,height));draw=ImageDraw.Draw(layer)
        opacity=min(1,max(0,(beat.duration-t)/.14))
        if subject:
            k=_pop(t)
            # Short rays around the cutout, with open space behind the real subject.
            if .12<t<.65:
                for i in range(9):
                    a=i*math.tau/9
                    rx,ry=subject.width*.65,subject.height*.56
                    draw.line((cx+math.cos(a)*rx,cy+math.sin(a)*ry,
                               cx+math.cos(a)*(rx+22*unit),cy+math.sin(a)*(ry+22*unit)),
                              fill=YELLOW,width=max(1,round(5*unit)))
            _paste(layer,subject,cx,cy,k,-9+2*math.sin(t*7),opacity)
            label_y=cy+subject.height*.59
            # Clearly identify a frozen visual quotation, not a live tracked object.
            badge='定格放大';bf=font(round(22*unit));bb=bf.getbbox(badge)
            bx=cx-(bb[2]-bb[0])/2;by=cy-subject.height*.62
            draw.text((bx,by),badge,font=bf,fill='#fff8ed',stroke_width=max(1,round(2*unit)),stroke_fill=INK)
        else:
            label_y=cy
            if t<.55:
                for side in (-1,1):
                    xx=cx+side*(len(chars)*size*.5+24*unit)
                    draw.line((xx,label_y-25*unit,xx+side*22*unit,label_y-43*unit),fill=YELLOW,width=max(1,round(5*unit)))
        advance=size*.94
        for i,p in enumerate(letters):
            k=_pop(t-.045*i-(.1 if subject else 0))
            # Deliberate alternating baseline and scale; each word stays in safe space.
            x=cx+(i-(len(chars)-1)/2)*advance
            y=label_y+((-1)**i)*size*.07
            _paste(layer,p,x,y,k,(-1)**i*7+math.sin(t*5+i)*1.5,opacity)
        if safe_bottom is not None:
            # Fit the whole animated overlay, including overshoot/rays/label,
            # above actual wrapped subtitle bounds; never cover text with a sticker.
            bbox=layer.getchannel('A').getbbox()
            if bbox and bbox[3]>safe_bottom:
                patch=layer.crop(bbox)
                if patch.height>safe_bottom-8:
                    ratio=max(1,safe_bottom-8)/patch.height
                    patch=patch.resize((max(1,round(patch.width*ratio)),max(1,round(patch.height*ratio))),Image.Resampling.LANCZOS)
                layer=Image.new('RGBA',(width,height))
                layer.alpha_composite(patch,(max(0,min(width-patch.width,bbox[0])),max(0,round(safe_bottom-patch.height))))
        layer.save(folder/f'{n:04d}.png')
    return folder/'%04d.png'
