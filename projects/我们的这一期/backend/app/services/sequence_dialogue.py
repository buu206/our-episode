"""Confirmed speech alongside sampled pictures, not a fresh audio judgement."""
import json
from pathlib import Path

from pydantic import Field

from . import story_evidence
from .director import DirectorError, _invalid_constant, _unique_object
from .sequence_review import Observation, Review

PROMPT = Path(__file__).with_name('prompts') / 'sequence_dialogue.txt'


def packet(episode, version, window):
    mapped = story_evidence.context(episode, version)
    if mapped is None:
        return None
    evidence = {row['id']: row for row in episode.evidence}
    items = []
    for row in mapped['speech']:
        if row['output_start'] >= window['end'] or row['output_end'] <= window['start']:
            continue
        items.append({'speech_id': f'u{len(items)+1}', 'evidence_id': row['evidence_id'],
            'start': row['output_start'], 'end': row['output_end'],
            'text': evidence[row['evidence_id']]['note'],
            'whole_utterance_in_output': row['whole_utterance'],
            'whole_utterance_in_window': row['whole_utterance']
                and row['output_start'] >= window['start'] and row['output_end'] <= window['end']})
    if not items:
        return None
    return {'scope': 'confirmed_speech_text', 'audio_analyzed': False,
            'items': items, 'omitted': mapped['omitted']}


def wire(packet):
    # Local evidence identifiers are only needed for binding the persisted report.
    return {**packet, 'items': [{k: v for k, v in row.items() if k != 'evidence_id'}
                               for row in packet['items']]}


class LinkedObservation(Observation):
    speech_ids: list[str] = Field(max_length=64)


class LinkedReview(Review):
    observations: list[LinkedObservation] = Field(max_length=3)


def overlapping(packet, start, end):
    return [row for row in packet['items'] if row['start'] < end and row['end'] > start]


def parse_model(raw, frames, packet):
    # The model selects observable frame pairs. Exact temporal joins belong to
    # deterministic code, and must not rely on the model remembering every ID.
    from .sequence_review import parse as parse_visual
    try:
        decoded = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, TypeError):
        raise DirectorError('INVALID_MODEL_OUTPUT', '观察回复格式无法读取，未据此生成剪法。',
                            usage={'validation_reason': 'json'}) from None
    try:
        Review.model_validate(decoded)
    except (ValueError, TypeError):
        raise DirectorError('INVALID_MODEL_OUTPUT', '观察回复未符合约定结构，未据此生成剪法。',
                            usage={'validation_reason': 'schema'}) from None
    try:
        result = parse_visual(raw, frames)
    except DirectorError as exc:
        exc.usage = {'validation_reason': 'frame_positions'}
        raise
    positions = {f['frame_id']: f['time'] for f in frames}
    for item in result['observations']:
        item['speech_ids'] = [row['speech_id'] for row in overlapping(
            packet, positions[item['first_frame']], positions[item['last_frame']])]
    return parse(json.dumps(result), frames, packet)


def parse(raw, frames, packet):
    try:
        value = LinkedReview.model_validate(json.loads(raw, object_pairs_hook=_unique_object,
                                                      parse_constant=_invalid_constant))
        positions = {f['frame_id']: f['time'] for f in frames}
        pairs = set()
        for item in value.observations:
            first, last = positions[item.first_frame], positions[item.last_frame]
            pair = (item.first_frame, item.last_frame)
            if first >= last or pair in pairs:
                raise ValueError('invalid ordered pair')
            pairs.add(pair)
            expected = {row['speech_id'] for row in overlapping(packet, first, last)}
            if len(set(item.speech_ids)) != len(item.speech_ids) or set(item.speech_ids) != expected:
                raise ValueError('speech coverage mismatch')
        return value.model_dump()
    except (KeyError, ValueError, TypeError):
        raise DirectorError('INVALID_MODEL_OUTPUT', '动作与对白的引用未通过对应检查，未据此生成剪法。') from None
