"""Visible, locally rendered visual quotations and original cartoon sounds.

Face stickers quote a measured frame; they do not classify emotions or generate
a new face. A jaw-based mask includes a conservative forehead cap, not hair
segmentation. No external recording or cloned human voice is used.
"""
import array
import math
import sys
import wave
from PIL import Image, ImageDraw, ImageFilter
from .sticker_beats import sticker, _paste


def display_captions(captions, *, boundaries=(), speech_spans=(), hold_short=False, protected=()):
    """Group short adjacent cues only for display. Keep source edits/IDs intact."""
    result = []
    for row in captions:
        if row.get('hidden'):
            continue
        prev = result[-1] if result else None
        if (prev and row.get('evidence_id') and prev.get('evidence_id') == row['evidence_id']
                and not row.get('subtitle_changed') and not prev.get('subtitle_changed')
                and -.001 <= row['start'] - prev['end'] <= .25
                and row['end'] - prev['start'] <= 3.5
                and len(prev['text']) + len(row['text']) <= 24
                and not any(prev['start']+.001 < cut <= row['start']+.001 for cut in boundaries)
                and not any(s['start'] < row['start']-.001 and s['end'] > prev['end']+.001 for s in speech_spans)
                and not any(a < row['end'] and b > prev['start'] for a,b in protected)):
            separator = ' ' if prev['text'][-1:].isascii() and row['text'][:1].isascii() else ''
            prev['text'] += separator + row['text']
            prev['end'] = row['end']
            prev['cue_ids'].append(row.get('cue_id'))
        else:
            result.append({**row, 'cue_ids': [row.get('cue_id')]})
    if hold_short:
        for row in result:
            row['source_end']=row['end']
            if row.get('subtitle_changed') or any(a<row['end'] and b>row['start'] for a,b in protected):
                continue
            # Reading hold, not a shifted speech onset. Never cross a cut, a
            # following voice (including hidden captions), or another subtitle.
            stops=[cut for cut in boundaries if cut>=row['end']-.001]
            stops += [s['start'] for s in speech_spans if s['start']>=row['end']-.001]
            stops += [s['start'] for s in result if s is not row and s['start']>=row['end']-.001]
            if any(s['start'] < row['end'] and s['end'] > row['end']+.001 for s in speech_spans):
                continue
            wanted=max(row['end'],min(row['end']+.55,row['start']+.9))
            row['end']=max(row['end'],min([wanted]+stops))
    for row in result:
        # Source times may exceed the encoded frame boundary by one frame.
        # Always report and display the actual shot limit, even for edited cues.
        cuts=[cut for cut in boundaries if cut>row['start']+.001]
        if cuts:row['end']=min(row['end'],min(cuts))
    return result


def bound_effects(events, boundaries):
    """A graphic quotes this shot only; never carry its face onto the next cut."""
    bounded=[]
    for event in events:
        row=dict(event)
        end=min([row['end']]+[cut for cut in boundaries if cut>row['start']+.001])
        if end-row['start']<.3 or row.get('peak',row['start'])>=end-.15:
            continue
        row['end']=end
        bounded.append(row)
    return bounded


def face_sticker(frame, face):
    points = face.get('contour', [])
    if len(points) < 8 or not all(len(p) == 2 and all(math.isfinite(v) and 0 <= v <= 1 for v in p) for p in points):
        return None
    w, h = frame.size
    x, y, fw, fh = face['box']
    if face.get('contour_kind') == 'jaw':
        # Vision provides a jaw arc. Close it across the measured face box,
        # leaving hair/background out rather than pretending to segment them.
        points = points + [[x + fw*.9, y + fh*.12], [x + fw*.7, y],
                           [x + fw*.3, y], [x + fw*.1, y + fh*.12]]
    # Native landmarks can reverse their traversal order between adjacent frames.
    # A convex facial envelope avoids a self-intersecting, hollow sticker after
    # interpolation. Include measured eye/lip points, never invent hidden pixels.
    points = points + [p for eye in face['eyes'] for p in eye] + face['mouth']
    def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    ordered=sorted(set(tuple(p) for p in points))
    lower=[];upper=[]
    for p in ordered:
        while len(lower)>1 and cross(lower[-2],lower[-1],p)<=0:lower.pop()
        lower.append(p)
    for p in reversed(ordered):
        while len(upper)>1 and cross(upper[-2],upper[-1],p)<=0:upper.pop()
        upper.append(p)
    points=lower[:-1]+upper[:-1]
    mask = Image.new('L', frame.size)
    ImageDraw.Draw(mask).polygon([(px*w, py*h) for px, py in points], fill=255)
    bounds = mask.getbbox()
    if not bounds:
        return None
    cut = frame.convert('RGBA')
    cut.putalpha(mask.filter(ImageFilter.GaussianBlur(.6)))
    return sticker(cut.crop(bounds), round(min(w*.39, h*.25)))


def paste_reaction(frame, subject, elapsed, duration, anchor):
    w, h = frame.size
    # Reserve room for the largest pop and rotation, including on wide faces.
    ratio=min(1,w*.40/subject.width,h*.30/subject.height)
    if ratio<1:
        subject=subject.resize((max(1,round(subject.width*ratio)),max(1,round(subject.height*ratio))),Image.Resampling.LANCZOS)
    layer = Image.new('RGBA', frame.size)
    # Place beside the original face, keeping the dialogue lane and action clear.
    cx = w*(.27 if anchor[0] + anchor[2]/2 > .5 else .73)
    cy = h*.23
    pop = max(0, 1-math.exp(-16*elapsed)*math.cos(21*elapsed))
    alpha = min(1, max(0, (duration-elapsed)/.16))
    angle = 9*math.sin(elapsed*30)*math.exp(-elapsed*1.7)
    draw = ImageDraw.Draw(layer)
    for i in range(9):
        a = i*math.tau/9
        rx, ry = subject.width*.61, subject.height*.58
        draw.line((cx+math.cos(a)*rx, cy+math.sin(a)*ry,
                   cx+math.cos(a)*(rx+w*.045), cy+math.sin(a)*(ry+w*.045)),
                  fill=(255,225,80,round(230*alpha)), width=max(2,round(w*.008)))
    _paste(layer, subject, cx, cy, min(1.15,pop), angle, alpha)
    return Image.alpha_composite(frame.convert('RGBA'), layer).convert('RGB')


def write_sounds(path, duration, events, speech_spans):
    """Original nonhuman boing/pluck, reduced during speech; never a fake scream."""
    rate = 48000
    samples = array.array('h', [0])*math.ceil(duration*rate)
    cues = []
    for event in events:
        start = event['audible_start']
        if not 0 <= start < duration or any(abs(start-c['start']) < .7 for c in cues):
            continue
        seconds = .46 if event['kind'] == 'eyes' else .22
        spoken = any(s['start'] < start+seconds and s['end'] > start for s in speech_spans)
        gain = .055 if spoken else .14
        count = round(seconds*rate)
        for i in range(min(count,len(samples)-round(start*rate))):
            t, u = i/rate, i/count
            envelope = min(1,t/.008)*math.exp(-5*u)*min(1,(1-u)/.12)
            phase = (330*t + 130*(1-math.cos(24*t))/24) if event['kind']=='eyes' else (920*t-1100*t*t)
            value = gain*envelope*(.75*math.sin(math.tau*phase)+.2*math.sin(math.tau*phase*2))
            at = round(start*rate)+i
            samples[at] = max(-32767,min(32767,samples[at]+round(value*32767)))
        cues.append({'start':start,'kind':'cartoon_boing' if event['kind']=='eyes' else 'cartoon_pluck',
                     'gain':gain,'ducked_for_speech':spoken})
    if sys.byteorder != 'little':samples.byteswap()
    with wave.open(str(path),'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(rate);audio.writeframes(samples.tobytes())
    return cues
