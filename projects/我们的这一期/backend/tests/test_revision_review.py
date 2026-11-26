from app.services.revision_review import compare


def shot(start, end, asset='a', **extra):
    return {'asset_id': asset, 'start': float(start), 'end': float(end), 'caption': '', **extra}


def test_splitting_and_relabelling_continuous_take_is_not_a_new_cut():
    result = compare([shot(0, 35)], [shot(0, 12, caption='开场'), shot(12, 33), shot(33, 35)], [])
    assert result['change'] == 'same_source_sequence'
    assert result['removed'] == result['added'] == []
    assert result['quality'] == 'not_assessed'


def test_order_and_replay_are_detected_even_when_coverage_matches():
    for new in ([shot(5, 10), shot(0, 5)], [shot(0, 10), shot(0, 2)]):
        result = compare([shot(0, 10)], new, [])
        assert result['change'] == 'order_or_repeat_changed'
        assert not result['removed'] and not result['added']


def test_reports_exact_removed_intervals_and_speech_loss_without_silence_inference():
    evidence = [{'id': 's1', 'kind': 'speech', 'asset_id': 'a', 'start': 2., 'end': 5.},
                {'id': 's2', 'kind': 'speech', 'asset_id': 'a', 'start': 7., 'end': 8.}]
    result = compare([shot(0, 10)], [shot(0, 3), shot(5, 6), shot(8, 10)], evidence)
    assert result['removed'] == [{'asset_id': 'a', 'start': 3., 'end': 5.},
                                 {'asset_id': 'a', 'start': 6., 'end': 8.}]
    assert [s['impact'] for s in result['affected_speech']] == ['partially_removed', 'removed']
    assert result['source_duration_after'] == 6.
    assert result['quality'] == 'not_assessed'


def test_same_seconds_in_different_assets_are_not_equivalent():
    result = compare([shot(0, 2)], [shot(0, 2, 'b')], [])
    assert result['change'] == 'selection_changed'
    assert result['removed'][0]['asset_id'] == 'a' and result['added'][0]['asset_id'] == 'b'


def test_no_double_counting_overlaps_and_only_new_speech_loss_reported():
    speech = [{'id': 's', 'kind': 'speech', 'asset_id': 'a', 'start': 0., 'end': 10.}]
    result = compare([shot(1, 7), shot(5, 9)], [shot(1, 9)], speech)
    assert not result['affected_speech'] and not result['removed']
    assert result['change'] == 'order_or_repeat_changed'
    assert result['source_duration_before'] == 10.


def test_boundaries_preserve_subframe_precision():
    result = compare([shot(0, 1)], [shot(0, .5), shot(.51, 1)], [])
    assert result['removed'] == [{'asset_id': 'a', 'start': .5, 'end': .51}]


def test_adjacent_source_slices_preserve_whole_speech_coverage():
    result = compare([shot(0, 10)], [shot(0, 5), shot(5, 10)],
        [{'id': 's', 'kind': 'speech', 'asset_id': 'a', 'start': 2., 'end': 8.}])
    assert not result['affected_speech']
    # Existing grounding/boundary validation still decides if that split is legal.
    assert result['quality'] == 'not_assessed'


def test_user_description_reports_actual_speech_loss_not_model_claims():
    from app.services.revision_review import describe_cut
    speech = [{'id': 's', 'kind': 'speech', 'asset_id': 'a', 'start': 2., 'end': 3.}]
    summary, reply, _ = describe_cut([shot(0, 5)], [shot(1, 5)], speech)
    assert '5秒缩到4秒' in reply and '已确认对白全部保留' in summary
    summary, reply, _ = describe_cut([shot(0, 5)], [shot(3, 5)], speech)
    assert '整句舍弃1条' in reply and '原版保留' in reply
