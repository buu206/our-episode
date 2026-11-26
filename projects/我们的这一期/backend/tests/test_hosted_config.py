import json

import pytest

from app.config import configured_invites


def test_distinct_accounts_and_legacy_compatibility(tmp_path, monkeypatch):
    monkeypatch.delenv('APP_INVITES_FILE', raising=False)
    monkeypatch.setenv('APP_INVITE_CODE', 'local-test')
    assert configured_invites() == {'local-test': 'local-owner'}
    source = tmp_path / 'invites.json'
    mapping = {'a' * 32: 'alice', 'b' * 32: 'bob'}
    source.write_text(json.dumps(mapping))
    monkeypatch.setenv('APP_INVITES_FILE', str(source))
    assert configured_invites() == mapping


@pytest.mark.parametrize('payload', [
    {}, [], {'short': 'alice'}, {'a' * 32: 'local-owner'},
    {'a' * 32: 'alice', 'b' * 32: 'alice'}, {'a' * 32: 123},
])
def test_bad_file_fails_closed_without_legacy_fallback(tmp_path, monkeypatch, payload):
    source = tmp_path / 'invites.json'
    source.write_text(json.dumps(payload))
    monkeypatch.setenv('APP_INVITES_FILE', str(source))
    monkeypatch.setenv('APP_INVITE_CODE', 'private-legacy')
    with pytest.raises(ValueError) as error:
        configured_invites()
    assert 'private-legacy' not in str(error.value)
    assert 'a' * 32 not in str(error.value)


def test_missing_and_duplicate_invites_fail_closed(tmp_path, monkeypatch):
    source = tmp_path / 'invites.json'
    monkeypatch.setenv('APP_INVITES_FILE', str(source))
    with pytest.raises(ValueError):
        configured_invites()
    source.write_text('{"' + 'a' * 32 + '":"alice","' + 'a' * 32 + '":"bob"}')
    with pytest.raises(ValueError):
        configured_invites()


def test_hosted_launcher_rejects_local_entry_and_insecure_origin(monkeypatch):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / 'deploy/serve.py'
    spec = importlib.util.spec_from_file_location('hosted_serve', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    valid = {'APP_ORIGIN':'https://studio.example.com','APP_INVITES_FILE':'/run/secrets/invites.json'}
    module.validate(valid)
    for override in [{'APP_ORIGIN':'http://studio.example.com'}, {'LOCAL_DEMO_ENTRY':'1'},
                     {'APP_ORIGIN':'https://studio.example.com/path'}, {'APP_INVITES_FILE':''},
                     {'BACKEND_MODE':'demo'}]:
        with pytest.raises(ValueError):
            module.validate({**valid, **override})
