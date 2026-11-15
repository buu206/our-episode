"""Offline source/utterance/transition contracts, not an editorial quality score."""
from copy import deepcopy
import json

import pytest

from app.services.director import DirectorError, check_grounding, parse_result, prepare_input
from app.services.editing import timeline
from test_pipeline import (
    advance, analyze_and_review, client, create_with_asset, episode_state,
    no_network, request, services, settings, voiced_video_bytes,
)


ASSETS = {"a": 20.0, "b": 12.0}
EVIDENCE = [
    {"id": "visual-a", "kind": "visual", "asset_id": "a", "start": 0.0,
     "end": 8.0, "note": "人工核对的合成测试画面范围；非真实模型观察。"},
    {"id": "speech-a", "kind": "speech", "asset_id": "a", "start": 1.0,
     "end": 6.0, "note": "虚构测试话轮，必须完整保留。"},
    {"id": "visual-b", "kind": "visual", "asset_id": "b", "start": 0.0,
     "end": 8.0, "note": "另一份合成测试画面。"},
    {"id": "speech-b", "kind": "speech", "asset_id": "b", "start": 2.0,
     "end": 5.0, "note": "第二个虚构测试话轮。"},
]


def span(row):
    return {key: row[key] for key in ("asset_id", "start", "end", "note")}


def proposal():
    return {"directions": [{"id": "test-story", "title": "测试现场回顾",
        "summary": "保持完整测试话轮，用留白分隔两个已核对片段。",
        "evidence": [span(row) for row in EVIDENCE],
        "edit_plan": {"segments": [
            {"asset_id": "a", "start": 0.0, "end": 8.0, "caption": "",
             "purpose": "交代第一段，保留完整测试原声。", "evidence_ids": ["visual-a", "speech-a"]},
            {"asset_id": "b", "start": 0.0, "end": 8.0, "caption": "",
             "purpose": "从留白处叠化到第二段，不掩盖测试原声。",
             "evidence_ids": ["visual-b", "speech-b"], "transition": "dissolve", "transition_duration": .3},
        ], "narration_text": ""}}], "uncertainties": ["以上只是结构测试，不是内容效果评审。"]}


def validate(data, evidence=EVIDENCE):
    result = parse_result(json.dumps(data, ensure_ascii=False), ASSETS, require_edit_plan=True)
    check_grounding(result, evidence)
    return result


def test_preserves_source_identity_and_kind_without_changing_legacy_payload():
    typed = json.loads(prepare_input(deepcopy(EVIDENCE), ASSETS))
    assert typed["verified_evidence"] == EVIDENCE
    old = [span(row) for row in EVIDENCE]
    assert json.loads(prepare_input(old, ASSETS))["verified_evidence"] == old


@pytest.mark.parametrize("mutation", ["missing_kind", "missing_id", "duplicate_id", "mixed", "unknown_kind", "blank_id"])
def test_invalid_identity_cannot_downgrade_typed_grounding(mutation):
    rows = deepcopy(EVIDENCE)
    if mutation == "missing_kind":
        rows[0].pop("kind")
    elif mutation == "missing_id":
        rows[0].pop("id")
    elif mutation == "duplicate_id":
        rows[1]["id"] = rows[0]["id"]
    elif mutation == "mixed":
        rows[0] = span(rows[0])
    elif mutation == "unknown_kind":
        rows[0]["kind"] = "inferred_event"
    else:
        rows[0]["id"] = " "
    with pytest.raises(DirectorError) as error:
        prepare_input(rows, ASSETS)
    assert error.value.code == "INVALID_EVIDENCE"


def test_complete_utterances_with_transition_in_silence_are_accepted():
    result = validate(proposal())
    segments = result.directions[0].edit_plan.segments
    assert segments[1].transition == "dissolve"
    assert timeline(segments)[-1].end == pytest.approx(15.7)


@pytest.mark.parametrize("change", [
    {"start": 2.0}, {"end": 5.7},
    {"start": 5.0, "end": 7.0, "evidence_ids": ["visual-a"]},
])
def test_any_overlapping_speech_must_be_complete_even_for_visual_only_citation(change):
    data = proposal()
    data["directions"][0]["edit_plan"]["segments"][0].update(change)
    with pytest.raises(DirectorError) as error:
        validate(data)
    assert error.value.code == "INVALID_EDIT_PLAN"


@pytest.mark.parametrize("change", [
    {"evidence_ids": []}, {"evidence_ids": ["invented-id"]},
    {"evidence_ids": ["visual-b"]}, {"evidence_ids": ["speech-a"]},
    {"purpose": " "}, {"purpose": ""},
])
def test_typed_plan_requires_grounded_ids_full_coverage_and_mirror_purpose(change):
    data = proposal()
    data["directions"][0]["edit_plan"]["segments"][0].update(change)
    with pytest.raises(DirectorError):
        validate(data)


@pytest.mark.parametrize("transition", ["dissolve", "fade_black"])
@pytest.mark.parametrize("edge", ["incoming", "outgoing"])
def test_transition_must_not_hide_either_side_of_a_complete_utterance(transition, edge):
    data = proposal()
    shots = data["directions"][0]["edit_plan"]["segments"]
    shots[1]["transition"] = transition
    if edge == "incoming":
        shots[1]["start"] = 2.0
    else:
        shots[0]["end"] = 6.0
    with pytest.raises(DirectorError) as error:
        validate(data)
    assert error.value.code == "INVALID_EDIT_PLAN"
    shots[1].update(transition="cut", transition_duration=0)
    validate(data)


def test_rounding_tolerance_preserves_a_complete_speech_turn():
    data = proposal()
    shots = data["directions"][0]["edit_plan"]["segments"]
    shots[0].update(start=1.02, end=5.98)
    shots[1].update(transition="cut", transition_duration=0)
    validate(data)


def test_legacy_untyped_plan_remains_readable_without_new_fields():
    data = proposal()
    for shot in data["directions"][0]["edit_plan"]["segments"]:
        for field in ("purpose", "evidence_ids", "transition", "transition_duration"):
            shot.pop(field, None)
    data["directions"][0]["edit_plan"]["segments"][0].update(start=2.0, end=3.0)
    validate(data, [span(row) for row in EVIDENCE])


def test_cannot_refer_to_an_id_that_the_direction_does_not_cite():
    data = proposal()
    other = {"id": "outside", "kind": "visual", "asset_id": "a", "start": 10.0,
             "end": 12.0, "note": "与当前片段无关的合成依据。"}
    data["directions"][0]["edit_plan"]["segments"][0]["evidence_ids"].append("outside")
    with pytest.raises(DirectorError) as error:
        validate(data, [*EVIDENCE, other])
    assert error.value.code == "UNGROUNDED_EVIDENCE"


def test_pipeline_checks_grounding_again_after_adapter_return(client, services, voiced_video_bytes, monkeypatch):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    received = []

    async def faulty_adapter(evidence, assets, background, reservation, permit):
        services._permission("directions", reservation, permit)
        received.extend(deepcopy(evidence))
        source = next(row for row in evidence if row["kind"] == "speech")
        return {"result": {"directions": [{"id": "bad-reference", "title": "测试错误引用",
            "summary": "模拟适配器漏校验，但应用边界仍必须拒绝。", "evidence": [span(source)],
            "edit_plan": {"segments": [{"asset_id": source["asset_id"], "start": source["start"],
                "end": source["end"], "caption": "", "purpose": "保留完整话轮",
                "evidence_ids": ["invented-source-id"]}], "narration_text": ""}}],
            "uncertainties": []}, "usage": services._usage("directions")}

    monkeypatch.setattr(services, "directions", faulty_adapter)
    job = request(client, f"/episodes/{eid}/directions", auth)
    failed = advance(client, job["id"], expected="failed")
    assert failed["error"]["code"] == "UNGROUNDED_EVIDENCE"
    assert received and all(row.get("id") and row.get("kind") in ("visual", "speech") for row in received)
    assert episode_state(client, eid)["directions"] == []
