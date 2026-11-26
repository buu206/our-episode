from dataclasses import dataclass, field
from pathlib import Path
import os
import json
import re
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
PRODUCT = BACKEND.parent


def configured_invites() -> dict[str, str]:
    """Private operator-managed invitations. Never report credential contents."""
    filename = os.getenv('APP_INVITES_FILE', '')
    if not filename:
        code = os.getenv('APP_INVITE_CODE', '')
        return {code: 'local-owner'} if code else {}
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate')
                result[key] = value
            return result
        payload = json.loads(Path(filename).read_text(encoding='utf-8'), object_pairs_hook=unique_pairs)
        if not isinstance(payload, dict) or not 1 <= len(payload) <= 1000:
            raise ValueError('shape')
        if any(not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', code)
               or not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', owner)
               or owner == 'local-owner' for code, owner in payload.items()):
            raise ValueError('format')
        if len(set(payload.values())) != len(payload):
            raise ValueError('shared owner')
        return payload
    except (OSError, ValueError, UnicodeError):
        raise ValueError('APP_INVITES_FILE 无效：需要为每位用户配置独立邀请和归属标识。') from None


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: PRODUCT / 'data')
    invites: dict[str, str] = field(default_factory=dict, repr=False)
    local_demo_entry: bool = False
    worker_enabled: bool = True
    session_seconds: int = 86400
    max_upload_bytes: int = 1024 ** 3
    max_total_seconds: float = 600
    max_assets: int = 8
    cloud: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls):
        load_dotenv(BACKEND / '.env', override=False)
        invites = configured_invites()
        from .services.provider_config import cloud_configuration
        cloud = cloud_configuration(os.environ)
        return cls(data_dir=Path(os.getenv('APP_DATA_DIR', str(PRODUCT / 'data'))).resolve(),
                   invites=invites, cloud=cloud,
                   local_demo_entry=os.getenv('LOCAL_DEMO_ENTRY') == '1')
