"""Conservative literal accents from complete, confirmed speech cues.

This selects readable quotations, not jokes or emotional diagnoses. It never
rewrites a sentence, stitches speakers, or invents word-level timestamps.
"""
import re


def complete_quote(line):
    if (line.get('alignment') != 'asr' or not line.get('evidence_id')
            or line.get('subtitle_changed') or line.get('hidden')):
        return None
    text = line['text'].strip()
    context = line.get('context', '').strip()
    # An ASR display cue can be only half a sentence. Require an entire clause
    # delimited in the confirmed context, not just a substring match.
    clauses = [s.strip() for s in re.split(r'[，。！？；,.!?;]', context) if s.strip()]
    literal = text.rstrip('。！.!')
    if literal not in clauses or not 2 <= len(literal) <= 8:
        return None
    if not re.fullmatch(r'[\u3400-\u9fff]+', literal):
        return None
    if any(word in context for word in ('不是', '别说', '别以为', '没说', '不一定', '假如', '如果')):
        return None
    if any(mark in text for mark in ('?', '？')) or literal.endswith(('吗', '么', '呢')):
        return None
    if all(char in '嗯啊哦呃唉诶哎好对是的呀吧' for char in literal):
        return None
    duration = line['end'] - line['start']
    # Long utterances and sub-second bursts are subtitle material; do not
    # squeeze them into the bounded accent treatment.
    if not .45 <= duration <= 2.5 or len(literal) / duration > 8:
        return None
    return literal
