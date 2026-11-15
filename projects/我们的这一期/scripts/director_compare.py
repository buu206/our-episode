#!/usr/bin/env python3
"""Offline prompt packs and blind review. No network, keys, DB or smoke records."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / "backend"))
from app.services.director import (  # noqa: E402
    DirectorError, PlannedDirectionResult, _invalid_constant, _unique_object,
    check_grounding, parse_result, prepare_input,
)


def read_text(path: Path) -> str:
    with path.open("rb") as handle:
        raw = handle.read(100_001)
    if len(raw) > 100_000:
        raise ValueError("file too large")
    return raw.decode("utf-8")


def read_case(path: Path) -> tuple[dict, str]:
    case = json.loads(read_text(path), object_pairs_hook=_unique_object,
                      parse_constant=_invalid_constant)
    if (not isinstance(case, dict) or set(case) != {
            "assets", "verified_evidence", "background_context"}):
        raise ValueError("invalid case")
    user_text = prepare_input(case["verified_evidence"], case["assets"], case["background_context"])
    return case, user_text


def checked_candidate(path: Path, case: dict):
    result = parse_result(read_text(path), case["assets"], require_edit_plan=True)
    check_grounding(result, case["verified_evidence"])
    return result


def save_json(path: Path, value) -> None:
    # Exclusive writes: don't replace existing reviews or supplied files.
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def prepare(case_path: Path, destination: Path) -> None:
    case, user_text = read_case(case_path)
    prompt = (PRODUCT / "backend/app/services/prompts/director.txt").read_text(encoding="utf-8")
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": user_text}]
    digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    save_json(destination / "case.json", case)
    save_json(destination / "messages.json", messages)
    save_json(destination / "response-schema.json", PlannedDirectionResult.model_json_schema())
    save_json(destination / "manifest.json", {
        "mode": "offline-preparation-only", "messages_sha256": digest,
        "real_calls": 0, "quality_result": "待实际模型输出及人工评分",
        "instructions": "两家候选使用同一 messages。先核实供应商条件、预算与计费，再另行接入；此工具不会调用模型。",
    })


def review(case_path: Path, candidate_paths: list[Path], destination: Path) -> None:
    case, _ = read_case(case_path)
    candidates = [(index, checked_candidate(path, case)) for index, path in enumerate(candidate_paths)]
    secrets.SystemRandom().shuffle(candidates)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    mapping = {}
    for label, (index, result) in zip(("A", "B"), candidates, strict=True):
        mapping[label] = {"input_position": index + 1,
                          "response_sha256": hashlib.sha256(read_text(candidate_paths[index]).encode()).hexdigest()}
        save_json(destination / f"候选{label}.json", result.model_dump())
    save_json(destination / "评分后再打开-对应关系.json", mapping)
    with (destination / "评分表.md").open("x", encoding="utf-8") as handle:
        handle.write("""# 编导方案盲评（本地材料，不是真实模型验收记录）

先看候选 A/B，评分后再打开对应关系。仅隐去输入文件名和厂商标识，不保证正文无法猜出来源。
工具只检查结构、来源区间和时长；不判断字幕或旁白是否真实，不证明这些文件来自模型。

| 判断项 | 候选 A | 候选 B |
| --- | --- | --- |
| 原话、人物关系、反应归属是否准确（编造即不通过） | 待回看核对 | 待回看核对 |
| 故事是否清楚（1–5 分，附原因） | 待评分 | 待评分 |
| 花字是否合适（1–5 分，附原因） | 待评分 | 待评分 |
| 旁白是否自然、有事实依据（1–5 分，附原因） | 待评分 | 待评分 |
| 需人工修改多少处 | 待统计 | 待统计 |
| 实际模型/版本、用量、耗时和含重试费用（揭盲后填） | 待实测 | 待实测 |

空 directions 是模型选择不提案，应评价证据是否真的不足，不能仅凭未报错就判它更好。
最终需用同一套合成器预览、听原声，再决定模型；结构通过不等于成片质量通过。
""")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    pack = subs.add_parser("prepare")
    pack.add_argument("--case", type=Path, required=True)
    pack.add_argument("--out", type=Path, required=True)
    check = subs.add_parser("check")
    check.add_argument("--case", type=Path, required=True)
    check.add_argument("--candidate", type=Path, required=True)
    blind = subs.add_parser("review")
    blind.add_argument("--case", type=Path, required=True)
    blind.add_argument("--candidate", type=Path, action="append", required=True)
    blind.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            prepare(args.case, args.out)
        elif args.action == "check":
            case, _ = read_case(args.case)
            result = checked_candidate(args.candidate, case)
            print(json.dumps({"structural_check": "passed", "directions": len(result.directions),
                              "real_model_verified": False}))
            return 0
        else:
            if len(args.candidate) != 2:
                raise ValueError("two candidates required")
            review(args.case, args.candidate, args.out)
    except DirectorError as exc:
        print(f"未完成：{exc.code}。未调用模型。", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("未完成：输入格式/文件或输出目录无效；输出目录必须是新目录。未调用模型。", file=sys.stderr)
        return 2
    print("本地材料已生成；真实调用 0 次，模型效果待验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
