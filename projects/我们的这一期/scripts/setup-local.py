#!/usr/bin/env python3
"""Create private local settings once. Never print or replace the invite code."""

import os
from pathlib import Path
import secrets


def main():
    backend = Path(__file__).resolve().parents[1] / "backend"
    destination = backend / ".env"
    content = (
        "# 本地凭据：不要提交、截图或粘贴到聊天。\n"
        f"APP_INVITE_CODE={secrets.token_urlsafe(24)}\n"
        "CLOUD_ENABLED=false\n"
        "CLOUD_RETENTION_VERIFIED=false\n"
        "CLOUD_REGION_VERIFIED=false\n"
        "MODEL_API_KEY=\n"
        "MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
        "MODEL_NAME=qwen3-vl-plus\n"
    )
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("本地配置已存在，保持原文件。")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    print("已创建 backend/.env（仅本人可读写）；云模型默认关闭，邀请码未显示。")


if __name__ == "__main__":
    main()
