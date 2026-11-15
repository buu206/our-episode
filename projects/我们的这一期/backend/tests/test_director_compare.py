"""Comparison preparation must be offline and cannot manufacture smoke evidence."""
import json
from pathlib import Path
import socket
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import director_compare as compare
from test_director import ASSETS, EVIDENCE, payload


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("comparison tooling must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(httpx, "Client", denied)
    monkeypatch.setattr(httpx, "AsyncClient", denied)


@pytest.fixture
def case_path(tmp_path):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"assets": ASSETS, "verified_evidence": EVIDENCE,
                               "background_context": "测试输入"}))
    return path


def test_prepare_exports_same_prompt_and_required_schema_without_calls(case_path, tmp_path):
    output = tmp_path / "prepared"
    assert compare.main(["prepare", "--case", str(case_path), "--out", str(output)]) == 0
    messages = json.loads((output / "messages.json").read_text())
    assert json.loads(messages[1]["content"])["verified_evidence"] == EVIDENCE
    schema = json.loads((output / "response-schema.json").read_text())
    assert "edit_plan" in schema["$defs"]["PlannedDirection"]["required"]
    assert json.loads((output / "manifest.json").read_text())["real_calls"] == 0
    assert not list(tmp_path.rglob("冒烟记录.md"))
    before = (output / "messages.json").read_bytes()
    assert compare.main(["prepare", "--case", str(case_path), "--out", str(output)]) == 2
    assert (output / "messages.json").read_bytes() == before


@pytest.mark.parametrize("raw", ["null", "[]", "{", '{"assets":{},"assets":{}}', "x" * 100001])
def test_invalid_case_fails_without_content_or_partial_pack(case_path, tmp_path, raw, capsys):
    case_path.write_text(raw)
    assert compare.main(["prepare", "--case", str(case_path), "--out", str(tmp_path / "out")]) == 2
    assert not (tmp_path / "out").exists()
    assert "Traceback" not in capsys.readouterr().err


def test_invalid_response_is_not_scored_or_published(case_path, tmp_path, capsys):
    candidate = tmp_path / "candidate.json"
    data = payload()
    data["directions"][0]["evidence"][0]["end"] = 10
    candidate.write_text(json.dumps(data))
    assert compare.main(["check", "--case", str(case_path), "--candidate", str(candidate)]) == 2
    assert "UNGROUNDED_EVIDENCE" in capsys.readouterr().err


def test_review_is_blinded_with_recoverable_mapping_not_a_quality_claim(case_path, tmp_path):
    candidates = [tmp_path / "qwen.json", tmp_path / "deepseek.json"]
    for index, candidate in enumerate(candidates):
        data = payload()
        data["directions"][0]["title"] = f"候选标题{index}"
        candidate.write_text(json.dumps(data))
    output = tmp_path / "review"
    assert compare.main(["review", "--case", str(case_path), "--candidate", str(candidates[0]),
                         "--candidate", str(candidates[1]), "--out", str(output)]) == 0
    mapping = json.loads((output / "评分后再打开-对应关系.json").read_text())
    assert {entry["input_position"] for entry in mapping.values()} == {1, 2}
    for label, info in mapping.items():
        candidate = json.loads((output / f"候选{label}.json").read_text())
        assert candidate["directions"][0]["title"] == f'候选标题{info["input_position"] - 1}'
    assert "不是真实模型验收记录" in (output / "评分表.md").read_text()
    assert not list(tmp_path.rglob("冒烟记录.md"))


def test_check_reports_abstention_without_claiming_quality(case_path, tmp_path, capsys):
    candidate = tmp_path / "empty.json"
    candidate.write_text('{"directions":[],"uncertainties":["证据不足"]}')
    assert compare.main(["check", "--case", str(case_path), "--candidate", str(candidate)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"structural_check": "passed", "directions": 0, "real_model_verified": False}
