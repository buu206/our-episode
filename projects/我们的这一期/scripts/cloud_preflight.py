#!/usr/bin/env python3
"""Offline configuration check; never enables cloud processing or calls a model.

Exit 0 means local configuration matches the existing adapters. Exit 2 means
configuration is incomplete or could not be checked. Neither result verifies
provider retention/region evidence, available budget, or actual model quality.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

PRODUCT = Path(__file__).resolve().parents[1]
SWITCHES = ("CLOUD_ENABLED", "CLOUD_RETENTION_VERIFIED", "CLOUD_REGION_VERIFIED")
NOTICE = "配置就绪不等于供应商留存、删除及实际处理地区证据已核实，也不等于真实模型效果通过。"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str


def read_configuration(env_file: Path, environ: Mapping[str, str] | None = None) -> dict:
    """Read the same file/environment precedence as Settings without mutation.

    DotEnv.dict() does not call set_as_environment_variables(). override=False
    also matches Settings' environment-first interpolation precedence.
    """
    from dotenv.main import DotEnv

    # The production call uses the actual environment, including interpolation.
    # Tests may inject a mapping when no interpolation is involved.
    environment = os.environ if environ is None else environ
    if environment.get("PYTHON_DOTENV_DISABLED", "").casefold() in {"1", "true", "t", "yes", "y"}:
        return dict(environment)
    values = {key: value for key, value in
              DotEnv(dotenv_path=env_file, override=False, verbose=False).dict().items() if value is not None}
    return {**values, **environment}


def check_configuration(values: Mapping, *, product: Path = PRODUCT) -> list[Check]:
    """Inspect configuration and directory metadata only; no DB/media reads."""
    backend = str(PRODUCT / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.services.director import CloudDirector
    from app.services.vision import CloudVision
    from app.services.speech import ASR_MODEL, TTS_MODEL, CloudSpeech

    from app.services.provider_config import cloud_configuration, adapter
    if any(values.get(key, 'bailian') != 'bailian' for key in
           ('VISION_PROVIDER', 'DIRECTOR_PROVIDER', 'ASR_PROVIDER', 'TTS_PROVIDER')):
        cloud = cloud_configuration(values)
        checks = []
        for kind in ('vision', 'director', 'asr', 'tts'):
            try:
                ready, reason = adapter(cloud, kind).ready()
                checks.append(Check(kind.upper() + '_ADAPTER', 'ready' if ready else 'missing', reason))
            except Exception:
                checks.append(Check(kind.upper() + '_ADAPTER', 'invalid', '供应商配置无效；未显示配置值。'))
        directory = Path(values.get('APP_DATA_DIR', str(product / 'data'))).resolve()
        demo = (product / 'data/demo').resolve()
        valid = not (directory == demo or directory.is_relative_to(demo)) and (not directory.exists() or directory.is_dir())
        checks.append(Check('APP_DATA_DIR', 'ready' if valid else 'invalid', '仅检查目录，不读取业务数据。'))
        checks.append(Check('BUDGET', 'not_checked', '每期3元、累计30元由服务端调用前预留检查。'))
        return checks

    checks: list[Check] = []
    cloud = {name: values.get(name, "") for name in ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME")}
    for name in SWITCHES:
        value = values.get(name, "")
        enabled = isinstance(value, str) and value.lower() == "true"
        cloud[name] = enabled
        checks.append(Check(name, "ready" if enabled else "missing",
                            "已配置启用标记；本检查不核实标记背后的授权或供应商证据。" if enabled
                            else "缺少启用标记；请先完成对应确认，不要为通过检查自动开启。"))

    key = cloud["MODEL_API_KEY"]
    valid_key = isinstance(key, str) and bool(key) and not any(char.isspace() for char in key)
    checks.append(Check("MODEL_API_KEY", "ready" if valid_key else "missing" if not key else "invalid",
                        "已配置，未验证凭据有效性。" if valid_key else "缺少有效的密钥配置；不显示配置值。"))

    raw_url = cloud["MODEL_BASE_URL"]
    valid_url = False
    if isinstance(raw_url, str):
        try:
            parsed = urlsplit(raw_url)
            valid_url = (raw_url.rstrip("/") == CloudDirector._base_url
                         and parsed.scheme == "https" and parsed.hostname == "dashscope.aliyuncs.com"
                         and parsed.username is None and parsed.password is None
                         and parsed.port is None and not parsed.query and not parsed.fragment)
        except ValueError:
            pass
    checks.append(Check("MODEL_BASE_URL", "ready" if valid_url else "missing" if not raw_url else "invalid",
                        "符合现有导演/视觉与语音适配器的共同地址限制；不证明实际推理地域。" if valid_url
                        else "未配置或不符合现有适配器的共同 HTTPS 地址白名单。"))

    cloud["ASR_MODEL"] = values.get("ASR_MODEL", ASR_MODEL)
    cloud["TTS_MODEL"] = values.get("TTS_MODEL", TTS_MODEL)
    for name, expected in (("MODEL_NAME", CloudDirector._model), ("ASR_MODEL", ASR_MODEL), ("TTS_MODEL", TTS_MODEL)):
        valid = cloud[name] == expected
        checks.append(Check(name, "ready" if valid else "missing" if not cloud[name] else "invalid",
                            "与当前适配器允许的型号匹配；未验证账号可用性。" if valid
                            else "未配置或型号不在当前适配器允许范围内。"))

    for name, adapter in (("DIRECTOR_ADAPTER", CloudDirector), ("VISION_ADAPTER", CloudVision),
                          ("SPEECH_ADAPTER", CloudSpeech)):
        try:
            ready, _ = adapter(cloud).ready()
            checks.append(Check(name, "ready" if ready is True else "missing",
                                "现有适配器本地配置检查通过。" if ready is True else "现有适配器配置未就绪。"))
        except Exception:
            # Never propagate provider/configuration exception text or values.
            checks.append(Check(name, "invalid", "适配器本地检查失败；未判定为就绪。"))

    try:
        raw_directory = values.get("APP_DATA_DIR", str(product / "data"))
        if not isinstance(raw_directory, str) or "\0" in raw_directory:
            raise ValueError("invalid directory")
        directory = Path(raw_directory).resolve()
        demo = (product / "data" / "demo").resolve()
        if directory == demo or directory.is_relative_to(demo):
            checks.append(Check("APP_DATA_DIR", "invalid", "数据目录与本产品演示目录混用；不能作为正常云端模式目录。"))
        elif directory.exists() and not directory.is_dir():
            checks.append(Check("APP_DATA_DIR", "invalid", "数据目录位置不是目录。"))
        else:
            checks.append(Check("APP_DATA_DIR", "ready", "目录路径可解析且未使用本产品演示目录；未读取数据、测试写入或创建目录。"))
    except (OSError, ValueError, RuntimeError, TypeError):
        checks.append(Check("APP_DATA_DIR", "invalid", "无法安全检查数据目录路径；不显示路径值。"))

    checks.append(Check("BUDGET", "not_checked", "未打开数据库或查询余额；每期 3 元、累计 30 元及未知费用阻断由服务端调用前事务强制检查。"))
    return checks


def main(argv: list[str] | None = None, *, env_file: Path | None = None,
         environ: Mapping[str, str] | None = None, product: Path = PRODUCT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("current", "deepseek-volc", "deepseek-volc-minimax", "deepseek-minimax"), default="current")
    args = parser.parse_args(argv)
    print("离线云端准入配置检查：不访问网络、不读取素材、不写配置或冒烟记录。")
    try:
        values = read_configuration(env_file or product / "backend/.env", environ)
        if args.profile in ("deepseek-volc", "deepseek-volc-minimax", "deepseek-minimax"):
            values = {**values, "VISION_PROVIDER": "deepseek", "DIRECTOR_PROVIDER": "deepseek",
                      "ASR_PROVIDER": "minimax" if args.profile == "deepseek-minimax" else "volc",
                      "TTS_PROVIDER": "minimax" if args.profile.endswith("minimax") else "volc"}
            print("仅在本次检查中选择候选供应商组合；未写入配置、启用标记或密钥。")
        checks = check_configuration(values, product=product)
    except ImportError:
        print("[invalid] DEPENDENCIES：依赖未就绪，请使用产品 backend/.venv/bin/python。")
        print(NOTICE)
        return 2
    except Exception:
        print("[invalid] CONFIGURATION：无法完成本地配置读取或检查；不显示异常内容。")
        print(NOTICE)
        return 2
    for item in checks:
        print(f"[{item.status}] {item.name}：{item.message}")
    ready = all(item.status in ("ready", "not_checked") for item in checks)
    print("本地配置检查就绪；预算仍由服务端检查。" if ready else "本地配置未就绪；没有开启任何开关或发送请求。")
    print(NOTICE)
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
