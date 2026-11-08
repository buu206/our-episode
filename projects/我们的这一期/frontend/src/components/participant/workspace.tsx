'use client';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { api, mediaUrl, uploadSubmission } from '@/lib/api';
import type { Invitation, Objection, Question, Submission } from '@/lib/collaboration-types';
import { Button, MediaPlayer, Notice } from '../ui';
import SubmissionCard from './submission-card';
import { labelState, literal, useParticipantDirty, type Ask, type Command, type DirtyReport, type ParticipantData } from './contracts';
import styles from './participant.module.css';

type Common = { busy: boolean; command: Command; ask: Ask; report: DirtyReport };
function Questions({ invitation, busy, command, report }: Omit<Common, 'ask'> & { invitation: Invitation }) {
  const [edited, setEdited] = useState<Question[] | null>(null);
  const items = edited || invitation.questions;
  const dirty = !!edited && JSON.stringify(edited) !== JSON.stringify(invitation.questions);
  useParticipantDirty('questions', dirty, report);
  const allowed = invitation.accepting_new && invitation.role !== 'review';
  if (!items.length) return null;
  return <section className="portal-card"><h2>你想怎么回答</h2><p>可以改成更愿意回答的问法，也可以跳过。跳过不代表同意公开。</p>
    {items.map((question, index) => <fieldset className="portal-fieldset" key={question.id} disabled={busy || !allowed}><legend>问题 {index + 1}</legend>
      <label className="field">问题文字<input maxLength={200} value={question.text} onChange={event => setEdited(items.map(item => item.id === question.id ? { ...item, text: event.target.value } : item))} /></label>
      <label className="check-field"><input type="checkbox" checked={question.skipped} onChange={event => setEdited(items.map(item => item.id === question.id ? { ...item, skipped: event.target.checked } : item))} />我选择跳过这个问题</label>
    </fieldset>)}
    {allowed && <Button disabled={busy || !dirty || items.some(item => !item.text.trim())} onClick={async () => {
      const result = await command(signal => api<Invitation>('/participant/invitation/questions', { method: 'PUT', body: { questions: items.map(item => ({ ...item, text: item.text.trim() })) }, signal }), '问题修改与跳过状态已保存。');
      if (result.ok) setEdited(null);
    }}>保存问题修改与跳过</Button>}
    {invitation.role === 'review' && <p className="muted">这份邀请用于核对片段，问题只读。</p>}
  </section>;
}

function Compose({ invitation, busy, command, ask, report, refresh }: Common & { invitation: Invitation; refresh: () => Promise<boolean> }) {
  const [kind, setKind] = useState<'text' | 'audio' | 'video'>(() => invitation.role === 'contribute' ? 'video' : 'text');
  const [text, setText] = useState('');
  const [questionId, setQuestionId] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<{ sent: number; total: number } | null>(null);
  const [localError, setLocalError] = useState('');
  const [unknown, setUnknown] = useState(false);
  const requestId = useRef<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const dirty = !!text || !!file || unknown;
  useParticipantDirty('new-submission', dirty, report);
  const allowed = invitation.role !== 'review' && invitation.accepting_new;
  if (!allowed) return null;
  const change = () => { requestId.current = null; setLocalError(''); };
  const clear = () => { setText(''); setFile(null); setProgress(null); setUnknown(false); requestId.current = null; if (fileInput.current) fileInput.current.value = ''; };
  async function save(event?: FormEvent) {
    event?.preventDefault(); if (busy) return;
    if (!literal(text) || Array.from(text).length > 100) { setLocalError('表达最多100字，请整理为一句文字，不含换行或隐藏控制字符。'); return; }
    if (kind === 'text' && !text.trim()) { setLocalError('请先填写你的文字表达。'); return; }
    if (kind !== 'text') {
      const extensions = kind === 'audio' ? /\.(wav|mp3|m4a)$/i : /\.(mp4|mov)$/i;
      const limit = invitation.role === 'interview' || kind === 'audio' ? 100_000_000 : 1024 ** 3;
      if (!file || !extensions.test(file.name) || file.size === 0 || file.size > limit) { setLocalError(`请选择有效的${kind === 'audio' ? ' WAV / MP3 / M4A' : ' MP4 / MOV'}文件，大小不超过${limit === 100_000_000 ? '100 MB' : '1 GiB'}。真实格式和时长会在接收时核对。`); return; }
    }
    const id = requestId.current || crypto.randomUUID(); requestId.current = id; setLocalError(''); setProgress(null);
    const fields = { text, question_id: questionId || null, client_request_id: id };
    const result = await command(signal => kind === 'text'
      ? api<Submission>('/participant/submissions', { method: 'POST', body: fields, signal })
      : uploadSubmission({ ...fields, kind, file: file! }, (sent, total) => { if (alive.current) setProgress({ sent, total }); }, signal), '草稿已保存，发起人仍看不到。请在下方核对，再由你确认提交。');
    if (!alive.current) return;
    if (result.ok) clear();
    else { setUnknown(result.error.uncertain || result.error.code === 'UPLOAD_INACTIVE'); if (!result.error.uncertain && result.error.code !== 'UPLOAD_INACTIVE') requestId.current = null; }
  }
  return <section className="portal-card"><h2>保存草稿，先自己看</h2><p>确认提交前，发起人看不到你的草稿。文字是你本人填写的表达，不是自动转写。</p>
    <form className="portal-form" onSubmit={save}><fieldset disabled={busy || unknown} className={styles.fields}>
      <label className="field">回答哪个问题<select value={questionId} onChange={event => { setQuestionId(event.target.value); change(); }}><option value="">不指定问题</option>{invitation.questions.filter(item => !item.skipped).map(item => <option key={item.id} value={item.id}>{item.text}</option>)}</select></label>
      <label className="field">草稿形式<select value={kind} onChange={event => { setKind(event.target.value as typeof kind); setFile(null); setProgress(null); if (fileInput.current) fileInput.current.value = ''; change(); }}><option value="text">文字</option><option value="video">视频</option>{invitation.role === 'interview' && <option value="audio">本人语音文件</option>}</select></label>
      {kind !== 'text' && <label className="field">选择本人文件<input ref={fileInput} type="file" accept={kind === 'audio' ? '.wav,.mp3,.m4a' : '.mp4,.mov'} onChange={event => { setFile(event.target.files?.[0] || null); change(); }} /></label>}
      <label className="field">{kind === 'text' ? '我的文字表达' : '本人文字说明（选填）'}<input maxLength={100} value={text} onChange={event => { setText(event.target.value); change(); }} placeholder="最多100字，由你自己填写" /></label>
    </fieldset>
      <p className="portal-fineprint">{kind === 'text' ? '文字供核对保留，不会自动生成配音或采访视频。' : invitation.role === 'interview' ? '每份采访最多60秒、100 MB，必须有本人原声，将明确标为事后采访。' : '现场视频与本期所有原始来源共用8份、10分钟、1 GiB额度，私密草稿也计入。'}</p>
      {file && <p>{file.name} · {(file.size / 1024 ** 2).toFixed(1)} MiB</p>}
      {progress && <div role="status"><progress aria-label="草稿文件传输进度" value={progress.sent} max={progress.total || file?.size || 1} />{progress.total > 0 && progress.sent >= progress.total ? '传输完成，等待服务端校验和保存。' : '正在传输文件。'}</div>}
      {localError && <Notice tone="error">{localError}</Notice>}
      {!unknown && <Button type="submit" variant="primary" busy={busy} disabled={kind === 'text' ? !text.trim() : !file}>保存为私密草稿</Button>}
    </form>
    {unknown && <Notice>本次保存结果待核对。输入和请求编号仍保留，不会自动再传。请先检查“我的稿件”，尤其是仍在接收或已失败的记录。<div className="portal-actions">
      <Button disabled={busy} onClick={() => void refresh()}>刷新核对我的稿件</Button><Button disabled={busy} onClick={() => void save()}>重试同一个请求</Button>
      <Button disabled={busy} onClick={async () => { if (await ask('结束本次等待？', '仅在已找到这份草稿，或确认原上传已经失败后继续。这里会清除未保存的文字和所选文件；不会提交、删除或撤回服务器上的稿件。', '已核对，清空本次输入')) clear(); }}>已核对，清空本次输入</Button>
    </div></Notice>}
  </section>;
}

function ObjectionsPanel({ data, busy, command, ask, report, refresh }: Common & { data: ParticipantData; refresh: () => Promise<boolean> }) {
  const choices = data.submissions.filter(item => !item.deleted && (item.state === 'submitted' || (item.state === 'withdrawn' && item.revisions.length > 0)));
  const [target, setTarget] = useState<string | null>(null);
  const [range, setRange] = useState<{ start: string; end: string } | null>(null);
  const [reason, setReason] = useState('');
  const [unknown, setUnknown] = useState(false);
  const [localError, setLocalError] = useState('');
  const chosen = target || (data.invitation.context ? 'context' : choices[0]?.id || '');
  const source = choices.find(item => item.id === chosen);
  const context = chosen === 'context' ? data.invitation.context : null;
  const textOnly = source?.kind === 'text';
  const start = range?.start ?? String(context?.start ?? 0), end = range?.end ?? String(context?.end ?? source?.duration ?? 0);
  useParticipantDirty('objection', !!reason || unknown, report);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy || unknown) return;
    const a = Number(start), b = Number(end);
    if (!reason.trim() || !chosen || (!context && !source)) { setLocalError('请指定本人内容，并填写异议原因。'); return; }
    if (!textOnly && (!Number.isFinite(a) || !Number.isFinite(b) || !(a >= (context?.start ?? 0) && b > a && b <= (context?.end ?? source?.duration ?? 0)))) { setLocalError('异议区间必须落在本次授权片段或本人稿件内。'); return; }
    if (!await ask('提交这条表达异议？', '相关内容在解决或移除前会受到使用限制。只有你能确认自己提出的异议已经解决。', '登记我的异议')) return;
    const body = { reason: reason.trim(), ...(context ? {} : { submission_id: chosen }), ...(textOnly ? {} : { start: a, end: b }) };
    const result = await command(signal => api<Objection>('/participant/objections', { method: 'POST', body, signal }), '异议已登记，相关内容受到使用限制。');
    if (result.ok) { setReason(''); setRange(null); setLocalError(''); } else if (result.error.uncertain) setUnknown(true);
  }
  return <section className="portal-card"><h2>表达异议与处理</h2><p>仅针对邀请里开放的片段，或你已提交过的稿件。发起人不能代你确认解决。</p>
    {chosen && <form className="portal-form" onSubmit={submit}><fieldset className={styles.fields} disabled={busy || unknown}>
      <label className="field">异议针对哪一项<select value={chosen} onChange={event => { setTarget(event.target.value); setRange(null); }}>{data.invitation.context && <option value="context">邀请中开放的原片区间</option>}{choices.map(item => <option key={item.id} value={item.id}>{item.filename || '我的文字稿件'}</option>)}</select></label>
      {!textOnly && <div className="portal-two-fields"><label className="field">异议开始（原片秒）<input type="number" min={context?.start ?? 0} step="0.01" value={start} onChange={event => setRange({ start: event.target.value, end })} /></label><label className="field">异议结束（原片秒）<input type="number" step="0.01" value={end} onChange={event => setRange({ start, end: event.target.value })} /></label></div>}
      {context && <p className="portal-fineprint">播放器0秒对应原片{context.start}秒；这里填写原片{context.start}–{context.end}秒内的区间。</p>}
      <label className="field">哪里不符合你的意思<textarea maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} required /></label>
    </fieldset>{localError && <Notice tone="error">{localError}</Notice>}<Button type="submit" disabled={busy || unknown || !reason.trim()}>提交这条异议</Button></form>}
    {unknown && <Notice>异议提交结果尚未确定，先核对列表，避免重复登记。<div className="portal-actions"><Button disabled={busy} onClick={() => void refresh()}>刷新异议记录</Button><Button disabled={busy} onClick={async () => { if (await ask('解除重复提交保护？', '请先核对下面的异议记录。只有确认没有同一条异议，才再次点击提交；这里不会自动提交。', '已核对，允许重新提交')) setUnknown(false); }}>已核对，允许重新提交</Button></div></Notice>}
    <div className="portal-list">{data.objections.map(item => <article className="portal-item" key={item.id}><h3>{labelState(item.state)}</h3><p>{item.reason}</p>{item.start !== null && <p className="muted">原片区间：{item.start}–{item.end} 秒</p>}{item.state === 'open' && <Button disabled={busy} onClick={async () => {
      if (await ask('确认这条异议已解决？', '仅在你认可处理结果后确认。这不会自动恢复已关闭的分享，也不替代重新核对成片。', '本人确认已解决')) await command(signal => api(`/participant/objections/${item.id}/resolve`, { method: 'POST', body: { confirmed: true }, signal }), '你已确认这条异议解决。');
    }}>我确认这条问题已解决</Button>}</article>)}</div>
    {!data.objections.length && <p className="portal-empty">你还没有提出异议。</p>}
  </section>;
}

export default function ParticipantWorkspace({ data, name, busy, command, ask, onDirty, refresh }: { data: ParticipantData; name: string; busy: boolean; command: Command; ask: Ask; onDirty: (dirty: boolean) => void; refresh: () => Promise<boolean> }) {
  const dirtySections = useRef(new Set<string>());
  const report: DirtyReport = useCallback((key, dirty) => { if (dirty) dirtySections.current.add(key); else dirtySections.current.delete(key); onDirty(dirtySections.current.size > 0); }, [onDirty]);
  const invitation = data.invitation;
  const context = mediaUrl(invitation.context_preview_url, 'participant');
  return <div className={`portal-stack ${styles.workspace}`}>
    <section className="portal-card"><div className="portal-section-title"><h2>{name} · {({ contribute: '补充现场视频', interview: '事后采访', review: '核对指定片段' })[invitation.role]}</h2><Button disabled={busy} onClick={() => void refresh()}>刷新任务状态</Button></div>
      <Notice>你只能查看本任务的必要片段和自己的稿件，不包括整期原片或其他人的采访。问题与表达均由人填写，本页面不调用 AI。</Notice>
      <p className="portal-fineprint">{invitation.accepting_new ? `可新增内容，邀请有效至 ${new Date(invitation.expires_at * 1000).toLocaleString('zh-CN')}` : '邀请已关闭或到期，不能新增、修改草稿或提交。仍可决定已有修订、撤回授权、删除未提交草稿和处理异议。'}</p>
      {invitation.context ? <><p>授权原片区间：{invitation.context.start}–{invitation.context.end} 秒。下方是独立片段，计时从0开始。</p>{context ? <MediaPlayer src={context} title="邀请中授权的上下文片段" /> : <Notice>上下文预览：{labelState(invitation.context_state)}{invitation.context_error && `。${invitation.context_error.message}`}。你可刷新状态；失败时请联系发起人重新准备授权片段。</Notice>}</> : <p>这次邀请仅用于补充现场视频，不开放原片。</p>}
    </section>
    <Questions invitation={invitation} busy={busy} command={command} report={report} />
    <Compose invitation={invitation} busy={busy} command={command} ask={ask} report={report} refresh={refresh} />
    <section className="portal-stack" aria-labelledby="my-submissions"><h2 id="my-submissions">我的稿件与本人确认</h2>{data.submissions.length ? data.submissions.map(submission => <SubmissionCard key={submission.id} submission={submission} invitation={invitation} busy={busy} command={command} ask={ask} report={report} />) : <p className="portal-empty">还没有稿件。你可以跳过，不会因此授权公开任何表达。</p>}</section>
    <ObjectionsPanel data={data} busy={busy} command={command} ask={ask} report={report} refresh={refresh} />
  </div>;
}
