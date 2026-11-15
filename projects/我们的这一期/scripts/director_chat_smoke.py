"""Bounded real chat checks for the existing three-material authorization only.

No proposal is accepted, no voice/render/upload occurs. Stable client IDs make
reruns reconcile the same jobs. Public evidence contains usage metadata only.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('baseline', 'plain-language', 'personality', 'story-proposal'), default='baseline')
    parser.add_argument('--revision', type=int, choices=(1, 2, 3), default=1,
                        help='Separately recorded bounded plain-language evaluations; never creates a random retry ID.')
    args = parser.parse_args()
    settings = Settings.from_env()
    grant_path = settings.data_dir / '授权记录/真实素材首测-20261112.json'
    if grant_path.is_symlink() or not grant_path.is_file():
        raise SystemExit('缺少已批准的三素材测试授权，未发送。')
    grant = json.loads(grant_path.read_text())
    episode_id = grant.get('episode_id')
    if grant.get('scope') != 'real-materials-20261112' or grant.get('unknown_vendor_retention_accepted') is not True:
        raise SystemExit('授权范围不符，未发送。')
    # Server rechecks the full grant, owner, three assets and fingerprints.
    base = 'http://127.0.0.1:8788'
    route = '/episodes/' + episode_id
    records = []
    with httpx.Client(base_url=base, headers={'Origin': base}, timeout=30) as client:
        def request(method, path, **kwargs):
            response = client.request(method, '/api/v1' + path, **kwargs)
            if response.status_code >= 400:
                error = response.json().get('error', {})
                raise SystemExit(f'接口拒绝：{response.status_code} / {error.get("code", "ERROR")}；未自动重试。')
            return response.json()
        code = next((key for key, owner in settings.invites.items() if owner == grant.get('owner_id')), None)
        if not code:
            raise SystemExit('未配置当前归属的登录，未发送。')
        auth = request('POST', '/auth/login', json={'invite_code': code})
        client.headers['X-CSRF-Token'] = auth['csrf_token']
        state = request('GET', route + '/director/chat')
        if not state['can_chat']:
            raise SystemExit('本期导演对话未就绪，未发送。')
        episode = request('GET', route)
        confirmed = [v for v in episode['versions'] if v['state'] == 'confirmed']
        if not confirmed:
            raise SystemExit('没有既有确认版，未发送。')
        version = max(confirmed, key=lambda v: v['created_at'])
        original_id = version['id']
        questions = [
            '我觉得当前成片没有故事感。请只依据本期已确认内容和当前版本，指出最影响连贯性的一个问题，先解释，不要修改或编造剧情。',
            '接着刚才的建议，先把开场旁白改得更短、更干脆。给我一句不超过12个汉字的新文案，并提供另存旁白草案的建议；不要直接生成音频。',
        ]
        if args.case == 'plain-language':
            questions = ['先根据这一期已有内容，告诉我最该改哪里，暂时不要生成新版本。']
            if args.revision == 3:
                questions = ['刚才的回复太专业、太长了，我不懂剪辑。请用普通人能看懂的短段落，讲清一个问题和一个建议，不要素材编号、秒数或操作流程，先不改片。']
        elif args.case == 'personality':
            questions = [
                '这片子现在像把视频排队放了一遍，有点无聊。你自己更想把这期拍成什么感觉？先聊想法，不要修改。',
                '我想热闹一点，能不能找个别处的反应接到这句后面，显得大家都在笑？先讨论，不生成。',
                '那就不硬凑笑点了，先保留朋友聊天那种随意的感觉。别急着做，跟我说一句你的想法就行。',
            ]
        elif args.case == 'story-proposal':
            questions = ['继续验证这期的故事和连贯性。请根据已确认素材，提出一条重做方案的具体建议：优先留下完整原话和同一件事的前后过程，不再每段只截两秒；先让人看懂发生了什么，再用少量花字和合适的转场增加综艺感。不要编笑声、反应或人物状态，不强行把三个视频全用上。先给我一张修改建议卡，等我看过再生成方案，保留原确认成片。']
        for index, question in enumerate(questions, 1):
            client_id = (f'director-chat-plain-20261114-v{args.revision}-{index}' if args.case == 'plain-language'
                         else f'director-chat-real-20261113-v2-{index}')
            if args.case == 'personality':
                client_id = f'director-chat-personality-20261114-v{args.revision}-{index}'
            elif args.case == 'story-proposal':
                client_id = f'director-chat-story-proposal-20261114-v{args.revision}-{index}'
            job = request('POST', route + '/director/chat', json={
                'client_id': client_id, 'text': question, 'version_id': original_id,
                'step': 'preview', 'confirm_send': True})
            print(json.dumps({'turn': index, 'job': job['id'], 'state': job['state']}, ensure_ascii=False), flush=True)
            deadline = time.monotonic() + 150
            while job['state'] in ('queued', 'running') and time.monotonic() < deadline:
                time.sleep(1)
                job = request('GET', '/jobs/' + job['id'])
            if job['state'] != 'succeeded':
                raise SystemExit(f'第{index}轮状态：{job["state"]}，错误码：{(job.get("error") or {}).get("code", "PENDING")}；不重发。')
            with sqlite3.connect(f'file:{settings.data_dir / "app.sqlite3"}?mode=ro', uri=True) as db:
                row = db.execute('select created_at, usage, elapsed from model_calls where job_id=? and kind=?',
                                 (job['id'], 'director_chat')).fetchall()
            if len(row) != 1:
                raise SystemExit('调用账目不唯一，停止并核对。')
            created, usage_json, elapsed = row[0]
            usage = json.loads(usage_json)
            if not usage.get('real_call') or usage.get('cost_status') != 'known':
                raise SystemExit('费用或真实调用状态未确认，停止。')
            records.append({'turn': index, 'time': created, 'model': usage.get('model'),
                'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens'),
                'cost_yuan': usage.get('cost_yuan'), 'elapsed': elapsed, 'structure': 'passed',
                'retries': usage.get('retries', 0), 'job_id': job['id']})
            state = request('GET', route + '/director/chat')
            if args.case == 'story-proposal':
                question_message = next(m for m in state['messages'] if m.get('client_id') == client_id)
                answer_id = question_message['id'].removesuffix('-user') + '-assistant'
                proposals = [p for p in state['proposals'] if p['message_id'] == answer_id]
                records[-1]['proposal_checks'] = {
                    'single_direction_proposal': len(proposals) == 1 and proposals[0]['kind'] == 'revise_directions',
                    'pending_user_choice': len(proposals) == 1 and proposals[0]['status'] == 'pending',
                }
                print(json.dumps({'proposal_checks': records[-1]['proposal_checks']}, ensure_ascii=False), flush=True)
            if args.case in ('plain-language', 'personality'):
                question_message = next(m for m in state['messages'] if m.get('client_id') == client_id)
                answer = next(m for m in state['messages'] if m['id'] == question_message['id'].removesuffix('-user') + '-assistant')
                text = answer['text']
                paragraphs = [part for part in text.split('\n\n') if part.strip()]
                checks = {'characters': len(text), 'paragraphs': len(paragraphs),
                          'short_and_paragraphed': len(text) <= 160 and 2 <= len(paragraphs) <= 3,
                          'no_technical_jargon': not any(word in text for word in ('选段', '话轮', '骨架', '因果重构', '重新生成方向')),
                          'no_action_proposed': not any(p['message_id'] == answer['id'] for p in state['proposals'])}
                records[-1]['presentation_checks'] = checks
                if args.case == 'personality':
                    checks['short_and_paragraphed'] = len(text) <= 160 and 1 <= len(paragraphs) <= 3
                    checks['one_sentence_size'] = index != 3 or (len(text) <= 50 and len(paragraphs) == 1)
                    checks['no_service_phrases'] = not any(word in text for word in
                        ('您的需求已收到', '为您优化', '从专业角度', '综上所述'))
                print(json.dumps({'presentation_checks': checks}, ensure_ascii=False), flush=True)
            print(json.dumps({'turn': index, 'state': 'succeeded', 'messages': len(state['messages']),
                              'cost_yuan': usage.get('cost_yuan')}, ensure_ascii=False), flush=True)
        latest = request('GET', route)
        assert next(v for v in latest['versions'] if v['id'] == original_id) == version
        evidence = ROOT / ('docs/evidence/阶段3-2/导演口语回复实调记录.md' if args.case == 'plain-language'
                           else 'docs/evidence/阶段3-2/悬浮导演实调记录.md')
        if args.case == 'plain-language' and args.revision == 2:
            evidence = ROOT / 'docs/evidence/阶段3-2/导演口语回复复测记录.md'
        elif args.case == 'plain-language' and args.revision == 3:
            evidence = ROOT / 'docs/evidence/阶段3-2/导演口语反馈纠正实调记录.md'
        elif args.case == 'personality':
            evidence = ROOT / ('docs/evidence/阶段3-2/导演个性对话实调记录.md' if args.revision == 1
                               else f'docs/evidence/阶段3-2/导演个性对话复测-v{args.revision}.md')
        elif args.case == 'story-proposal':
            evidence = ROOT / f'docs/evidence/阶段3-2/故事改法提案实调-v{args.revision}.md'
        lines = ['# 悬浮导演文字对话实调', '', f'由 scripts/director_chat_smoke.py 自动写入；覆盖{len(records)}轮文字对话，不替代完整阶段AI冒烟或成片质量验收。固定请求ID重跑仅核对既有任务，不算新增实调。', '',
                 '| 轮次 | 时间 | 模型 | 输入/输出tokens | 耗时秒 | 费用元 | 结构 | 重试 |', '|---|---|---|---|---|---|---|---|']
        for row in records:
            lines.append(f'| {row["turn"]} | {row["time"]} | {row["model"]} | {row["input_tokens"]}/{row["output_tokens"]} | {row["elapsed"]:.3f} | {row["cost_yuan"]} | {row["structure"]} | {row["retries"]} |')
        if args.case == 'plain-language':
            checks = records[0]['presentation_checks']
            lines.extend(['', f'可读性自动检查：{checks["characters"]}字符，{checks["paragraphs"]}段；短回复分段={checks["short_and_paragraphed"]}；无已列专业词={checks["no_technical_jargon"]}；未提出修改动作={checks["no_action_proposed"]}。',
                          '以上仅为长度、格式和动作检查；是否好懂、事实是否准确仍需内容评审。'])
        elif args.case == 'personality':
            lines.extend(['', '检查场景：回应不满意并表达偏好；讨论反应镜头的真实关系；接受新的创作偏好并遵守暂不执行。',
                          '', '| 轮次 | 字符/段落 | 简短 | 无已列术语 | 无已列客服短语 | 未提出动作 | 一句请求篇幅 |',
                          '|---|---|---|---|---|---|---|'])
            for row in records:
                c = row['presentation_checks']
                lines.append(f'| {row["turn"]} | {c["characters"]}/{c["paragraphs"]} | {c["short_and_paragraphed"]} | {c["no_technical_jargon"]} | {c["no_service_phrases"]} | {c["no_action_proposed"]} | {c["one_sentence_size"]} |')
            lines.extend(['', '自动检查只覆盖格式、部分字面表达与动作；个性自然程度、不附和虚假反应及是否理解上下文须另作内容评审，不能用字数代替。'])
        elif args.case == 'story-proposal':
            checks = records[0]['proposal_checks']
            lines.extend(['', f'单条重做方向建议={checks["single_direction_proposal"]}；待用户具体选择={checks["pending_user_choice"]}。',
                          '此记录只证明建议生成和持久化；未接受建议、未生成新方向或成片。故事依据、完整原声和剪辑效果须在后续候选中验证。'])
        lines.extend(['', '回复已持久化；未接受模型提案、未调用TTS/渲染/上传新素材，既有确认版本接口数据保持一致。',
                      '对话与素材内容只存应用私有数据库；本文件不记录正文、凭据或个人信息。', ''])
        evidence.write_text('\n'.join(lines))
        print(json.dumps({'recorded': True, 'turns': len(records), 'cost_yuan': sum(r['cost_yuan'] for r in records),
                          'original_confirmed_version_preserved': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
