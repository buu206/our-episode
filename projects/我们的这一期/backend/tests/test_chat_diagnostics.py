import json
import pytest
from app.services.director import DirectorError
from app.services.director_chat_model import parse_chat_reply


def error(data, allowed=('revise_cut',)):
    with pytest.raises(DirectorError) as result:
        parse_chat_reply(json.dumps(data), available_actions=allowed)
    assert result.value.code == 'INVALID_MODEL_OUTPUT'
    return result.value.message


def test_identifies_bad_field_without_exposing_the_value():
    message = error({'reply': 'hello', 'action': {'kind': 'revise_cut', 'title': '剪',
        'explanation': '试剪', 'selections': [{'shot': 'PRIVATE-SOURCE', 'start': 0., 'end': 1., 'reason': '保留'}]}})
    assert 'action.revise_cut.selections.0.shot' in message
    assert 'int_type' in message and 'PRIVATE-SOURCE' not in message


def test_unknown_keys_and_unknown_discriminators_stay_private():
    for data in ({'reply': 'hello', 'PRIVATE-KEY-NAME': 'PRIVATE-VALUE'},
                 {'reply': 'hello', 'action': {'kind': 'PRIVATE-ACTION'}}):
        message = error(data)
        assert 'PRIVATE' not in message


def test_missing_selection_field_distinguished_from_unavailable_action():
    message = error({'reply':'hello', 'action':{'kind':'revise_cut', 'title':'剪', 'explanation':'试剪'}})
    assert 'action.revise_cut' in message and 'value_error' in message  # Neither cut format supplied.
    message = error({'reply':'hello', 'action':{'kind':'navigate', 'title':'看', 'explanation':'查看', 'target_step':'preview'}})
    assert '不可用' in message and 'action.kind' in message


@pytest.mark.parametrize('raw', ['{"reply":"PRIVATE","reply":"PRIVATE2"}', '{"reply":NaN}', 'PRIVATE-RAW'])
def test_invalid_json_diagnostic_does_not_leak_content(raw):
    with pytest.raises(DirectorError) as result:
        parse_chat_reply(raw)
    assert 'JSON' in result.value.message and 'PRIVATE' not in result.value.message
