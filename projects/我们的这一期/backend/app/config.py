from dataclasses import dataclass, field
from pathlib import Path
import os
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
PRODUCT = BACKEND.parent


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: PRODUCT / 'data')
    invites: dict[str, str] = field(default_factory=dict, repr=False)
    worker_enabled: bool = True
    session_seconds: int = 86400
    max_upload_bytes: int = 1024 ** 3
    max_total_seconds: float = 600
    max_assets: int = 8
    cloud: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls):
        load_dotenv(BACKEND / '.env', override=False)
        code = os.getenv('APP_INVITE_CODE', '')
        cloud = {k: os.getenv(k, '') for k in ('MODEL_API_KEY', 'MODEL_BASE_URL', 'MODEL_NAME')}
        cloud['ASR_MODEL'] = os.getenv('ASR_MODEL', 'fun-asr-flash-2026-06-15')
        cloud['TTS_MODEL'] = os.getenv('TTS_MODEL', 'qwen3-tts-flash')
        for name in ('CLOUD_ENABLED', 'CLOUD_RETENTION_VERIFIED', 'CLOUD_REGION_VERIFIED'):
            cloud[name] = os.getenv(name, '').lower() == 'true'
        return cls(data_dir=Path(os.getenv('APP_DATA_DIR', str(PRODUCT / 'data'))).resolve(),
                   invites={code: 'local-owner'} if code else {}, cloud=cloud)
