"""Episode-scoped public-fixture review grants; original private grant unchanged.

These local operator records document already obtained authorization. There is
no API which can mint a grant and no inference of permission from a public URL.
Only output-frame reviews are covered, never audio, directions or publication.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit
from ..models import Episode
from ..repository import DomainError
from .material_trial import read_grant, verify_sources

ID = re.compile(r'^[a-f0-9]{32}$')
HASH = re.compile(r'^[a-f0-9]{64}$')


def grant_path(root, episode_id):
    if not ID.fullmatch(episode_id):
        raise DomainError('GRANT_INVALID', '节目授权标识无效。', 409)
    return root / '授权记录' / f'public-review-{episode_id}.json'


def _public(repo, episode_id, owner_id, path):
    try:
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 20_000:
            raise ValueError('path')
        from .director import _unique_object, _invalid_constant
        grant = json.loads(path.read_text(), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if (grant['scope'] != 'public-fixture-output-review-v1'
                or grant['episode_id'] != episode_id or grant['owner_id'] != owner_id
                or grant['providers'] != ['deepseek']
                or grant['operations'] != ['visual_review', 'sequence_review']
                or grant['task_limit_yuan'] != 3 or grant['total_limit_yuan'] != 30
                or grant['retention_verified'] is not False
                or not isinstance(grant['approval'], str) or not grant['approval'].strip()
                or not isinstance(grant['approved_at'], str) or not grant['approved_at'].strip()):
            raise ValueError('scope')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise DomainError('GRANT_INVALID', '本期公共素材的画面审阅授权缺失或不完整。', 409) from None
    validate_sources(repo, episode_id, owner_id, grant.get('assets'))
    return grant


def validate_sources(repo, episode_id, owner_id, assets):
    """Validate source identity without granting any provider operation."""
    try:
        if not isinstance(assets, list) or not 1 <= len(assets) <= 8:
            raise ValueError('assets')
        for item in assets:
            if (not ID.fullmatch(item['id']) or not HASH.fullmatch(item['sha256'])
                    or not re.fullmatch(r'assets/[a-f0-9]{32}\.(mp4|mov)', item['path'])
                    or type(item['size']) is not int or not 0 < item['size'] <= 1024**3
                    or urlsplit(item['source_url']).scheme != 'https'
                    or not urlsplit(item['source_url']).hostname):
                raise ValueError('asset')
        if len({a['id'] for a in assets}) != len(assets) or sum(a['size'] for a in assets) > 1024**3:
            raise ValueError('asset set')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise DomainError('GRANT_INVALID', '本期公共素材的画面审阅授权缺失或不完整。', 409) from None

    from .pipeline import active_assets
    with repo.factory() as db:
        episode = repo.owned(db, Episode, episode_id, owner_id)
        current = active_assets(db, episode)
        if {a.id for a in current} != {a['id'] for a in assets}:
            raise DomainError('SCOPE_CHANGED', '本期素材已变化，请重新核对审阅授权。', 409)
        expected = {a['id']: a for a in assets}
        for asset in current:
            spec = expected[asset.id]
            source = repo.data_dir / asset.path
            if (any(getattr(asset, key) != spec[key] for key in ('path', 'size'))
                    or source.is_symlink() or source.parent.is_symlink() or not source.is_file()
                    or source.stat().st_size != spec['size']
                    or hashlib.sha256(source.read_bytes()).hexdigest() != spec['sha256']):
                raise DomainError('SCOPE_CHANGED', '公共素材指纹不符，未发送审阅。', 409)


def for_version(repo, version, operation='visual_review'):
    path = grant_path(repo.data_dir, version.episode_id)
    # A corrupt/revoked public record never silently inherits another grant.
    if path.exists() or path.is_symlink():
        grant = _public(repo, version.episode_id, version.owner_id, path)
        if operation not in grant['operations']:
            raise DomainError('REVIEW_NOT_AUTHORIZED', '本期授权仅用于画面审阅，不包含该操作。', 409)
        return grant
    grant = read_grant(repo.data_dir)
    if version.episode_id != grant['episode_id'] or version.owner_id != grant['owner_id']:
        raise DomainError('SCOPE_CHANGED', '当前节目没有对应的画面审阅授权。', 409)
    verify_sources(repo, grant)
    return grant
