from types import SimpleNamespace
import pytest
from app.services.source_review import inspect_source


def episode(confirmed=True):
    return SimpleNamespace(evidence_revision=1, evidence_confirmed_revision=1 if confirmed else None,
        evidence=[dict(id='e', asset_id='a', kind='speech', start=1., end=5., note='一句完整原话')])


def segment(start, end, **extra):
    return dict(asset_id='a', start=start, end=end, **extra)


def test_partial_utterance_is_advice_not_a_transcript_verdict():
    report = inspect_source(episode(), [segment(2., 4.)])
    assert report['issue_count'] == 1 and report['speech_checked'] == 1
    assert report['issues'][0]['code'] == 'SPEECH_EDGE_CUT'
    assert report['issues'][0]['source_start'] == 2.
    assert report['scope'] == 'source_metadata' and report['not_checked']


@pytest.mark.parametrize('segments', [[segment(0., 6.)], [segment(0., 3.), segment(3., 6.)],
    [segment(5., 6.)], [dict(asset_id='b', start=2., end=4.)]])
def test_full_sentence_contiguous_cuts_and_unrelated_sources_are_not_flagged(segments):
    assert inspect_source(episode(), segments)['issue_count'] == 0


def test_noncontiguous_cuts_do_not_hide_missing_words():
    report = inspect_source(episode(), [segment(0., 2.), segment(4., 6.)])
    assert report['issue_count'] == 2
    assert [row['segment_number'] for row in report['issues']] == [1, 2]


def test_unconfirmed_evidence_is_not_used():
    report = inspect_source(episode(False), [segment(2., 4.)])
    assert not report['evidence_confirmed'] and report['speech_checked'] == 0 and not report['issues']


def test_hidden_and_reviewed_captions_are_respected_but_reading_speed_still_checked():
    lines = [dict(text='嗓子嗓子', start=1., end=3., alignment='asr', reviewed=True),
             dict(text='这里已经被用户隐藏', start=3., end=3.1, alignment='asr', hidden=True),
             dict(text='这句字幕显示得实在太快了', start=3., end=3.2, alignment='asr', reviewed=True)]
    report = inspect_source(episode(), [segment(0., 6., _dialogue=lines)])
    assert report['captions_checked'] == 2 and report['hidden_captions'] == 1
    assert report['issue_count'] == 1 and report['issues'][0]['message'] == '显示偏快'


def test_report_is_bounded_and_does_not_copy_full_user_words():
    report = inspect_source(episode(), [segment(2., 4.) for _ in range(20)])
    assert report['issue_count'] == 20 and len(report['issues']) == 8
    assert '一句完整原话' not in str(report)


def test_same_caption_warning_is_grouped_without_losing_total_count():
    lines = [dict(text='显示时间非常短的一句', start=n, end=n+.1, alignment='asr') for n in (1.,2.,3.)]
    report = inspect_source(episode(), [segment(0.,6.,_dialogue=lines)])
    assert report['issue_count'] == 3 and len(report['issues']) == 1
    assert report['issues'][0]['count'] == 3
