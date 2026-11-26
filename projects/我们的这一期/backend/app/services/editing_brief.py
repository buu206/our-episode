"""Source inventory for the director; no inferred events or editing mandate.

Only typed evidence already validated by prepare_input enters this index. It
distinguishes available source ranges from continuity containers. File count
does not establish the same occasion, synchronous cameras, or story quality.
"""
from collections import defaultdict


def build_editing_brief(spans, windows):
    if not spans or any(span.id is None for span in spans):
        return None  # Preserve the untyped legacy request contract.
    grouped = defaultdict(list)
    for span in spans:
        grouped[span.asset_id].append(span)
    inventory = []
    for asset_id in sorted(grouped):
        rows = sorted(grouped[asset_id], key=lambda s: (s.start, s.end, s.id))
        inventory.append({
            'asset_id': asset_id,
            'observations': [{'evidence_id': s.id, 'kind': s.kind,
                              'start': s.start, 'end': s.end} for s in rows],
            'continuity_container_ids': [w['id'] for w in windows if w['asset_id'] == asset_id],
        })
    return {
        'revision': 'story-first-1',
        'source_count': len(inventory),
        'inventory': inventory,
        'task': 'compare_supported_shot_roles_before_packaging',
        'same_event_verified': False,
        'synchronous_cameras_verified': False,
        'constraints': [
            'File count is not camera-angle or same-event evidence; consult the original observations.',
            'Preserve complete speech and unobserved necessary action; never cut an unknown gap just to add pace.',
            'When related observations support distinct roles, compare setup, action, visible response and closing.',
            'Adjacent ranges from the same file do not create a new camera angle.',
            'No minimum shot count or mandatory effect quota; a continuous fallback is not an improved edit.',
            'Choose the story first, then text, sparse sound and optional short director narration.',
        ],
    }


def inspect_directions(directions):
    """Non-blocking diagnostics, never a proxy for viewing or quality approval.

    Coalesce adjacent same-source splits: adding packaging boundaries must not
    inflate the number of actual edit decisions. No mandatory effects quota.
    """
    from .editing import timeline
    reports = []
    for direction in directions:
        plan = direction.edit_plan
        if plan is None:
            reports.append({'direction_id': direction.id, 'status': 'legacy_without_plan'})
            continue
        segments = [s.model_dump() for s in plan.segments]
        actual_cuts, adjacent_splits, graphic_roles, graphics = 0, 0, set(), 0
        for index, shot in enumerate(segments):
            if index:
                previous = segments[index-1]
                adjacent = (shot['asset_id'] == previous['asset_id'] and
                            abs(shot['start']-previous['end']) < 1e-6 and shot['transition'] == 'cut')
                adjacent_splits += int(adjacent)
                actual_cuts += int(not adjacent)
            beat = shot.get('editorial')
            if beat and beat['treatment'] != 'none':
                graphics += 1
                graphic_roles.add(beat['role'])
            if shot.get('caption','').strip():
                graphics += 1
                graphic_roles.add('unclassified')
            graphics += len(shot.get('visual_beats',[]))
            if shot.get('visual_beats'):
                graphic_roles.add('unclassified')
        flags = []
        if actual_cuts == 0: flags.append('continuous_take_not_editing_gain')
        if graphics == 0: flags.append('no_planned_graphics')
        elif graphic_roles == {'setup'}: flags.append('graphics_only_at_setup')
        if len(plan.narration_text.strip()) > 35: flags.append('long_opening_narration')
        timings = timeline(segments)
        reports.append({'direction_id': direction.id, 'status': 'needs_editorial_review',
            'source_count': len({s['asset_id'] for s in segments}),
            'segments': len(segments), 'actual_cuts': actual_cuts,
            'adjacent_packaging_splits': adjacent_splits,
            'duration': round(timings[-1].end, 3), 'planned_graphics': graphics,
            'flags': flags, 'quality_accepted': False})
    return reports
