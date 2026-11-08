'use client';
import { useState } from 'react';
import { api, mediaUrl } from '@/lib/api';
import type { Invitation, Revision, Submission } from '@/lib/collaboration-types';
import { Button, MediaPlayer, Notice, useMediaAccess } from '../ui';
import { labelState, literal, useParticipantDirty, type Ask, type Command, type DirtyReport } from './contracts';

type Props = { submission: Submission; invitation: Invitation; busy: boolean; command: Command; ask: Ask; report: DirtyReport };
function RevisionPanel({ revision, submission, busy, command, ask }: { revision: Revision } & Omit<Props, 'invitation' | 'report'>) {
  const [approvedKey, setApprovedKey] = useState('');
  const [playedKey, setPlayedKey] = useState('');
  const access = useMediaAccess();
  const media = mediaUrl(submission.media_url, 'participant');
  const key = JSON.stringify([revision, submission.state, submission.preview_state, media, access.revision]);
  const available = submission.state === 'submitted' && access.allowed && (submission.kind === 'text' || (submission.preview_state === 'ready' && !!media));
  const played = playedKey === key, approved = approvedKey === key;
  if (revision.state !== 'proposed') return <p className="portal-fineprint">表达修订：{labelState(revision.state)} · {revision.text || '不添加文字说明'}{submission.kind !== 'text' && ` · ${revision.start}–${revision.end}秒`}</p>;
  return <div className="portal-result"><h4>发起人提出的修订，等你决定</h4><p>文字：{revision.text || '不添加文字说明'}</p>{submission.kind !== 'text' && <><p>使用你原始稿件的 {revision.start}–{revision.end} 秒。</p><MediaPlayer src={available ? media : null} title="待确认修订的原声区间" start={revision.start} end={revision.end} onEnded={() => setPlayedKey(key)} /></>}
    <label className="check-field"><input type="checkbox" checked={approved} disabled={busy || !available || (submission.kind !== 'text' && !played)} onChange={event => setApprovedKey(event.target.checked ? key : '')} />我已核对这份文字和区间，符合我的意思。</label>
    {submission.kind !== 'text' && !played && <p className="muted">请播放此区间至结束后，再勾选本人确认。</p>}
    <div className="portal-actions"><Button disabled={busy || !available || !approved} onClick={async () => {
      if (await ask('确认这个具体修订？', `文字“${revision.text || '不添加文字说明'}”${submission.kind === 'text' ? '' : `，区间 ${revision.start}–${revision.end} 秒`}。只确认这一份，不授权未来任意改写。`, '确认这份修订')) await command(signal => api(`/participant/revisions/${revision.id}/decision`, { method: 'POST', body: { decision: 'confirmed' }, signal }), '这份修订已由你确认，是否采用仍由发起人决定。');
    }}>这份修订符合我的意思</Button><Button disabled={busy} onClick={async () => {
      if (await ask('不接受这份修订？', '将记录你对这份修订的拒绝，发起人不能采用它。此决定不能改写为确认；需要另建修订。', '不接受')) await command(signal => api(`/participant/revisions/${revision.id}/decision`, { method: 'POST', body: { decision: 'rejected' }, signal }), '已记录不接受这份修订。');
    }}>不接受这份修订</Button></div>
  </div>;
}

export default function SubmissionCard({ submission: sub, invitation, busy, command, ask, report }: Props) {
  const [edit, setEdit] = useState<string | null>(null);
  const [approvedKey, setApprovedKey] = useState('');
  const [playedKey, setPlayedKey] = useState('');
  const access = useMediaAccess();
  const text = edit ?? sub.text;
  const dirty = edit !== null && text !== sub.text;
  useParticipantDirty(`submission:${sub.id}`, dirty, report);
  const key = JSON.stringify([sub.id, sub.text, sub.duration, sub.media_url, sub.preview_state, sub.state, access.revision]);
  const media = mediaUrl(sub.media_url, 'participant');
  const ready = access.allowed && (sub.kind === 'text' ? !!sub.text.trim() : sub.preview_state === 'ready' && !!media);
  const canSubmit = invitation.accepting_new && invitation.role !== 'review' && sub.state === 'draft' && ready && !dirty && approvedKey === key && (sub.kind === 'text' || playedKey === key);
  const canDelete = sub.can_delete_draft && ['draft', 'failed', 'withdrawn'].includes(sub.state);
  async function remove() {
    if (await ask('删除这份未提交草稿？', '删除本应用保存的文字、原始副本和预览，完成清理后释放额度；你设备中的原文件不受影响。删除后无法恢复这份草稿。', '确认删除草稿')) await command(signal => api(`/participant/submissions/${sub.id}?confirm=true`, { method: 'DELETE', signal }), '草稿应用副本已删除，外部原文件保留。');
  }
  return <article className="portal-card" aria-label={`稿件：${sub.filename || '文字稿件'}`}><h3>{sub.filename || '文字稿件'} · {labelState(sub.state)}</h3>
    {sub.deleted ? <><Notice>{sub.deletion_pending ? '已停止访问，文件清理尚未完成，剩余文件仍占额度。可重试删除。' : '应用副本已清理，设备中的外部原文件仍保留。'}</Notice>{canDelete && <Button disabled={busy} variant="danger" onClick={() => void remove()}>重试删除草稿文件</Button>}</> : <>
      <p>你的文字表达：{sub.text || '未填写文字说明'}</p>
      {sub.kind !== 'text' && (media ? <MediaPlayer src={media} title={`本人稿件预览：${sub.filename || '采访'}`} onEnded={() => setPlayedKey(key)} /> : <Notice>本人预览：{labelState(sub.preview_state)}{sub.preview_error && `。${sub.preview_error.message}`}</Notice>)}
      {sub.kind === 'audio' && <p className="portal-fineprint">这是本人音频的卡片预览，保留原声；不是 AI 配音或转写。</p>}
      {sub.state === 'failed' && <Notice>这份上传没有完成。核对并删除失败草稿后，可以重新选择文件保存一份。</Notice>}
      {sub.state === 'draft' && <>
        <details><summary>修改我的草稿文字</summary><label className="field">草稿文字说明<input maxLength={100} value={text} disabled={busy || !invitation.accepting_new || invitation.role === 'review'} onChange={event => setEdit(event.target.value)} /></label>
          {!literal(text) && <Notice tone="error">文字不能含换行或隐藏控制字符。</Notice>}
          <Button disabled={busy || !dirty || !literal(text) || !invitation.accepting_new || invitation.role === 'review'} onClick={async () => {
            const result = await command(signal => api<Submission>(`/participant/submissions/${sub.id}`, { method: 'PUT', body: { text, question_id: sub.question_id }, signal }), '草稿文字已保存。请重新核对这份表达后再提交。');
            if (result.ok) { setEdit(null); setApprovedKey(''); setPlayedKey(''); }
          }}>保存草稿文字</Button>
        </details>
        {sub.preview_state === 'failed' && <Button disabled={busy} onClick={() => void command(signal => api(`/participant/submissions/${sub.id}/retry`, { method: 'POST', body: {}, signal }), '已重新准备本人预览，不会重复上传原文件。')}>重试本机预览处理</Button>}
        {invitation.accepting_new && <><label className="check-field"><input type="checkbox" checked={approvedKey === key} disabled={busy || !ready || dirty || (sub.kind !== 'text' && playedKey !== key)} onChange={event => setApprovedKey(event.target.checked ? key : '')} />我已完整核对本人内容，同意发起人按这份表达用于本期。</label>
          {sub.kind !== 'text' && playedKey !== key && <p className="muted">请播放本人预览至结束，再勾选确认。文字修改后需要重新核对。</p>}
          <Button variant="primary" disabled={busy || !canSubmit} onClick={async () => {
            // Capture before the dialog opens: the server compares this exact draft atomically.
            const expectedDraft = { text: sub.text, question_id: sub.question_id };
            if (await ask('提交这份本人表达？', `文字“${expectedDraft.text || '不添加文字说明'}”。发起人将能查看并决定是否采用；未来的改写或截短仍需你确认。`, '本人确认，提交')) await command(signal => api(`/participant/submissions/${sub.id}/submit`, { method: 'POST', body: { confirmed: true, expected_draft: expectedDraft }, signal }), '已本人确认提交，是否采用由发起人决定。');
          }}>确认并提交给发起人</Button></>}
      </>}
      {sub.state === 'submitted' && sub.revisions.map(revision => <RevisionPanel key={revision.id} revision={revision} submission={sub} busy={busy} command={command} ask={ask} />)}
      {sub.adoptions.map(adoption => <Notice key={adoption.id}>发起人采用状态：{labelState(adoption.state)}{adoption.error && `。${adoption.error.message}`}</Notice>)}
      <div className="portal-actions">{['draft', 'submitted'].includes(sub.state) && <Button variant="danger" disabled={busy} onClick={async () => {
        const draft = sub.state === 'draft';
        if (await ask(draft ? '放弃这份私密草稿？' : '撤回这份内容的使用授权？', draft ? '草稿不会向发起人公开。本机副本继续保留并占额度；如需清理，请使用删除未提交草稿。' : '相关候选和旧版本将受到使用限制。本机文件仍保留；已导出到其他设备的副本无法自动收回。', draft ? '放弃草稿，保留副本' : '确认撤回授权')) await command(signal => api(`/participant/submissions/${sub.id}/withdraw`, { method: 'POST', body: { confirmed: true }, signal }), draft ? '私密草稿已放弃，本机副本仍保留。' : '使用授权已撤回，相关内容受到限制，本机文件仍保留。');
      }}>{sub.state === 'draft' ? '放弃这份草稿，保留副本' : '撤回使用授权'}</Button>}{canDelete && <Button variant="danger" disabled={busy} onClick={() => void remove()}>删除未提交草稿及应用副本</Button>}</div>
    </>}
  </article>;
}
