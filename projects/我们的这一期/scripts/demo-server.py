#!/usr/bin/env python3
"""Opt-in local demo server; never loads backend/.env or cloud configuration."""
from pathlib import Path
import sys

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / "backend"))


def build_app():
    from app.config import Settings
    from app.main import create_app
    from demo_provider import DemoServices

    settings = Settings(data_dir=PRODUCT / "data" / "demo",
                        invites={"demo": "demo-viewer"}, worker_enabled=True, cloud={})
    return create_app(settings, services=DemoServices())


if __name__ == "__main__":
    import uvicorn

    print("本地流程演示：http://127.0.0.1:8790", flush=True)
    print("固定测试结果；不调用 AI、不外发、不扣费。提示音不是人声旁白。", flush=True)
    print("仅本机演示的公开登录码：demo", flush=True)
    uvicorn.run(build_app(), host="127.0.0.1", port=8790, workers=1, access_log=False)
