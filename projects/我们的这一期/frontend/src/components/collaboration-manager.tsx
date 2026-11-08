'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState, type FormEvent, type MouseEvent } from 'react';
import { ArrowLeft, ArrowUpRight, Check, Copy, MessageCircle, Plus, RefreshCw, Users } from 'lucide-react';
import { api, AppError, mediaUrl } from '@/lib/api';
import type { Episode, Job } from '@/lib/types';
import type { Invitation, InvitationCreated, Invitations, Objections, Revision, Submission, Submissions } from '@/lib/collaboration-types';
import { Button, Dialog, MediaAccessContext, MediaPlayer, Notice, useMediaAccess } from './ui';

type Bundle = { episode: Episode; invitations: Invitation[]; submissions: Submission[]; objections: Objections['items'] };
type Feedback = { text: string; error?: boolean } | null;
type RevisionDraft = { submission: Submission; text: string; start: string; end: string };
type Confirmation = { title: string; description: string; label: string; path: string; body?: unknown; submissionId?: string; revisionId?: string };
const roles = { contribute: '朋友供材', interview: '事后采访', review: '片段核对' };
const states: Record<string, string> = { proposed: '等本人确认', confirmed: '本人已确认', rejected: '本人未同意', withdrawn: '已撤回', pending: '正在准备', ready: '已加入素材', revoked: '已停用', failed: '准备失败', queued: '排队中', running: '准备中', succeeded: '已处理' };
const message = (error: unknown) => error instanceof Error ? error.message : '未能完成，请重新读取状态。';
const hasControl = (value: string) => /[\p{Cc}\p{Cf}]/u.test(value);
const time = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });

export default function CollaborationManager({ episodeId }: { episodeId: string }) {
  const access = useMediaAccess();
  const [data, setData] = useState<Bundle | null>(null);
  const [fresh, setFresh] = useState(false);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [busy, setBusy] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [role, setRole] = useState<Invitation['role']>('contribute');
  const [label, setLabel] = useState('');
  const [withContext, setWithContext] = useState(false);
  const [assetId, setAssetId] = useState('');
  const [start, setStart] = useState('0');
  const [end, setEnd] = useState('5');
  const [questions, setQuestions] = useState('');
  const [formDirty, setFormDirty] = useState(false);
  const [created, setCreated] = useState<InvitationCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [revision, setRevision] = useState<RevisionDraft | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const mounted = useRef(false);
  const epoch = useRef(0);
  const sequence = useRef(0);
  const reader = useRef<AbortController | null>(null);
  const working = useRef(false);
  const invalidateRead = useCallback(() => { sequence.current++; reader.current?.abort(); }, []);
  const blocked = !fresh || !access.allowed || !!busy || uncertain;
  const dirty = formDirty || !!revision || !!busy || uncertain;
  const hasContext = role !== 'contribute' || withContext;
  const source = data?.episode.assets.find(item => item.id === assetId);
  const revisionSource = revision && data?.submissions.find(item => item.id === revision.submission.id && item.state === 'submitted');
  const confirmationSource = confirmation?.submissionId && data?.submissions.find(item => item.id === confirmation.submissionId && item.state === 'submitted');
  const confirmedRevision = confirmationSource && confirmationSource.revisions.find(item => item.id === confirmation?.revisionId && item.state === 'confirmed');
  const confirmedAdoption = confirmationSource && confirmationSource.adoptions.find(item => item.revision_id === confirmation?.revisionId);
  const confirmationValid = !confirmation?.submissionId || !!(confirmationSource && confirmedRevision && (!confirmedAdoption || confirmedAdoption.state === 'failed'));

  const refresh = useCallback(async () => {
    const generation = ++sequence.current;
    const pageEpoch = epoch.current;
    reader.current?.abort();
    const controller = new AbortController(); reader.current = controller;
    try {
      const [episode, invitations, submissions, objections] = await Promise.all([
        api<Episode>(`/episodes/${episodeId}`, { signal: controller.signal }),
        api<Invitations>(`/episodes/${episodeId}/collaboration/invitations`, { signal: controller.signal }),
        api<Submissions>(`/episodes/${episodeId}/collaboration/submissions`, { signal: controller.signal }),
        api<Objections>(`/episodes/${episodeId}/collaboration/objections`, { signal: controller.signal }),
      ]);
      if (!mounted.current || pageEpoch !== epoch.current || generation !== sequence.current || controller.signal.aborted) return;
      setData({ episode, invitations: invitations.items, submissions: submissions.items.filter(item => ['submitted', 'withdrawn'].includes(item.state)), objections: objections.items });
      setFresh(!document.hidden); setError('');
    } catch (cause) {
      if (mounted.current && pageEpoch === epoch.current && generation === sequence.current && !controller.signal.aborted) {
        setFresh(false); setError(message(cause));
      }
      throw cause;
    }
  }, [episodeId]);

  useEffect(() => {
    mounted.current = true; const pageEpoch = ++epoch.current;
    return () => { mounted.current = false; epoch.current = pageEpoch + 1; invalidateRead(); window.dispatchEvent(new Event('episode:dirty-release')); };
  }, [invalidateRead]);
  useEffect(() => {
    let disposed = false, failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function tick() {
      if (disposed || document.hidden || !access.allowed) return;
      let delay = 5000;
      if (!working.current) {
        try { await refresh(); failures = 0; }
        catch { failures++; delay = Math.min(2000 * 2 ** (failures - 1), 30000); }
      }
      if (!disposed && !document.hidden) timer = setTimeout(tick, delay);
    }
    const visibility = () => {
      clearTimeout(timer); invalidateRead(); setFresh(false); failures = 0;
      if (!document.hidden && access.allowed) void tick();
    };
    queueMicrotask(() => { if (!disposed) { setFresh(false); void tick(); } });
    document.addEventListener('visibilitychange', visibility);
    return () => { disposed = true; clearTimeout(timer); invalidateRead(); document.removeEventListener('visibilitychange', visibility); };
  }, [access.allowed, access.revision, refresh, invalidateRead]);
  useEffect(() => { window.dispatchEvent(new CustomEvent('episode:dirty', { detail: dirty })); }, [dirty]);

  function leave(event: MouseEvent<HTMLAnchorElement>) {
    if (dirty && !window.confirm('还有未保存输入或结果待核对。离开会放弃未保存输入，服务端已保存内容保留。继续离开？')) event.preventDefault();
  }
  async function perform(label: string, operation: () => Promise<void>): Promise<boolean> {
    if (working.current || blocked || document.hidden) return false;
    working.current = true; setBusy(label); setFeedback(null);
    const pageEpoch = epoch.current;
    const current = () => mounted.current && epoch.current === pageEpoch;
    try {
      await operation();
      if (!current()) return false;
      try { await refresh(); }
      catch { if (current()) { setUncertain(true); setFeedback({ text: '操作已返回成功，但最新状态还没读到。请重新读取后核对，不要重复提交。', error: true }); } return true; }
      if (current()) setFeedback({ text: '已保存，当前显示服务端最新状态。' });
      return true;
    } catch (cause) {
      if (current()) {
        const unknown = cause instanceof AppError && cause.uncertain;
        if (unknown) setUncertain(true);
        setFeedback({ text: message(cause) + (unknown ? ' 结果不确定，已暂停操作；先核对列表，不会自动重发。' : ''), error: true });
        try { await refresh(); } catch { /* The original write outcome remains visible. */ }
      }
      return false;
    } finally { working.current = false; if (current()) setBusy(''); }
  }
  function request(path: string, body: unknown = {}) { return api(path, { method: 'POST', body }); }

  async function create(event: FormEvent) {
    event.preventDefault();
    const lines = questions.split('\n').map(value => value.trim()).filter(Boolean);
    const from = Number(start), to = Number(end);
    if (!label.trim() || Array.from(label.trim()).length > 40 || hasControl(label)) { setFeedback({ text: '请填写不超过40字、不含换行的邀请称呼。', error: true }); return; }
    if (lines.length > 8 || lines.some(value => Array.from(value).length > 200)) { setFeedback({ text: '最多8个问题，每行一个，每个不超过200字。', error: true }); return; }
    if (hasContext && (!source || !start.trim() || !end.trim() || !Number.isFinite(from) || !Number.isFinite(to) || from < 0 || to <= from || to > source.duration || to - from > 30)) {
      setFeedback({ text: '请选择有效素材和不超过30秒的片段，结束时间不能超过原片。', error: true }); return;
    }
    const pageEpoch = epoch.current;
    await perform('正在准备邀请', async () => {
      const result = await api<InvitationCreated>(`/episodes/${episodeId}/collaboration/invitations`, { method: 'POST', body: { role, label: label.trim(), context: hasContext ? { asset_id: assetId, start: from, end: to } : null, questions: lines } });
      if (!mounted.current || epoch.current !== pageEpoch) return;
      setCreated(result); setCopied(false); setFormDirty(false); setLabel(''); setQuestions('');
    });
  }
  async function copyInvite() {
    if (!created || !fresh || !access.allowed) return;
    try { await navigator.clipboard.writeText(`${window.location.origin}/participate#code=${encodeURIComponent(created.invite_code)}`); setCopied(true); }
    catch { setFeedback({ text: '浏览器没有允许复制。可以选中下面的邀请链接手动复制。', error: true }); }
  }
  function propose(submission: Submission) {
    setFeedback(null);
    const last = [...submission.revisions].reverse().find(item => item.state === 'confirmed');
    setRevision({ submission, text: last?.text ?? submission.text, start: String(last?.start ?? 0), end: String(last?.end ?? submission.duration) });
  }
  async function saveRevision(event: FormEvent) {
    event.preventDefault(); if (!revision || !revisionSource) return;
    const from = Number(revision.start), to = Number(revision.end);
    if (Array.from(revision.text).length > 100 || hasControl(revision.text)) { setFeedback({ text: '表达最多100字，不含换行或控制字符。', error: true }); return; }
    const textOnly = revision.submission.kind === 'text';
    if (!textOnly && (!revision.start.trim() || !revision.end.trim() || !Number.isFinite(from) || !Number.isFinite(to) || from < 0 || to <= from || to > revision.submission.duration)) {
      setFeedback({ text: '请检查片段起止时间，不能超过本人提交的媒体。', error: true }); return;
    }
    const result = await perform('等待保存修订', async () => { await request(`/collaboration/submissions/${revision.submission.id}/revisions`, { text: revision.text, start: textOnly ? 0 : from, end: textOnly ? 0 : to }); });
    if (result && mounted.current) setRevision(null);
  }
  function adopt(submission: Submission, selected: Revision) {
    setConfirmation({ title: `采用${submission.display_name}的这段表达？`, description: `只使用本人已确认的${selected.start.toFixed(1)}–${selected.end.toFixed(1)}秒及文字“${selected.text || '无附加文字'}”。准备完成后会加入本期素材；此后若本人撤回或提出异议，相关内容会停用。`, label: '确认采用这一修订', path: `/collaboration/revisions/${selected.id}/adopt`, submissionId: submission.id, revisionId: selected.id });
  }
  async function unlock() {
    if (working.current || !access.allowed || document.hidden) return;
    try {
      await refresh();
      if (window.confirm('请核对已有邀请、表达修订和采用状态，确认没有仍在处理的同一操作。解除后不会重发任何请求；若邀请创建已保存但未取得码，请关闭旧邀请后另建。已核对？')) {
        setUncertain(false); setFeedback({ text: '已解除操作锁定；没有重发请求。' });
      }
    } catch { /* Refresh error is shown above. */ }
  }

  if (!data) return <section className="panel portal-loading"><h1>朋友，一起入镜</h1>{error ? <><Notice tone="error">{error}</Notice><Button onClick={() => void refresh().catch(() => {})}>重新读取</Button></> : <p role="status">正在读取这一期的邀请和已提交内容…</p>}</section>;
  const openObjections = data.objections.filter(item => item.state === 'open').length;
  const createdInvite = created && data.invitations.find(item => item.id === created.id);
  const link = created ? `${typeof window !== 'undefined' ? window.location.origin : ''}/participate#code=${encodeURIComponent(created.invite_code)}` : '';
  return <MediaAccessContext.Provider value={{ allowed: access.allowed && fresh, revision: access.revision, revalidate: async () => { await access.revalidate?.(); await refresh(); } }}>
    <div className="portal-page cc-page" aria-busy={!!busy}>
      <nav className="portal-breadcrumb" aria-label="本期导航"><Link href={`/episodes/${episodeId}`} onClick={leave}><ArrowLeft size={16} />返回制作</Link><Link href={`/episodes/${episodeId}/sharing`} onClick={leave}>分享这一期<ArrowUpRight size={16} /></Link></nav>
      <header className="portal-hero"><div><span className="portal-kicker">FRIENDS ON AIR · 熟人特邀</span><h1>朋友，一起<span>入镜。</span></h1><p>{data.episode.title} · 让每个人决定自己的表达。</p></div><div className="portal-stamp" aria-hidden="true"><Users size={32} /><span>这次，大家都有戏</span></div></header>
      <div className="portal-status-row"><span>{data.invitations.length} 份邀请</span><span>{data.submissions.filter(item => item.state === 'submitted').length} 份已提交</span><span className={openObjections ? 'portal-warning' : ''}>{openObjections} 项异议待处理</span><Button variant="ghost" disabled={!!busy} onClick={() => void refresh().catch(() => {})}><RefreshCw size={16} />重新读取</Button></div>
      {error && <Notice tone="error">{error} 媒体和写入已暂停，未保存输入保留。</Notice>}
      {feedback && <Notice tone={feedback.error ? 'error' : 'success'}>{feedback.text}</Notice>}
      {uncertain && <Notice tone="error">上次操作结果待核对。<Button onClick={() => void unlock()} disabled={!!busy || !access.allowed}>核对记录后解除锁定</Button></Notice>}
      <div className="portal-columns cc-columns">
        <section className="portal-card cc-invite"><div className="portal-section-title"><span className="portal-number">01</span><div><h2>留一张特邀席</h2><p>一人一份邀请，7天内可加入。</p></div></div>
          <form onSubmit={create} className="portal-form">
            <label className="field">邀请用途<select value={role} disabled={blocked} onChange={event => { setRole(event.target.value as Invitation['role']); setFormDirty(true); }}><option value="contribute">朋友供材 · 补充现场片段</option><option value="interview">事后采访 · 说说当时的想法</option><option value="review">片段核对 · 仅核对与提出异议</option></select></label>
            <label className="field">邀请称呼<input value={label} maxLength={40} disabled={!!busy} placeholder="比如：给小满的特邀席" onChange={event => { setLabel(event.target.value); setFormDirty(true); }} required /></label>
            {role === 'contribute' && <label className="check-field"><input type="checkbox" checked={withContext} disabled={blocked} onChange={event => { setWithContext(event.target.checked); setFormDirty(true); }} />附上一段需要核对的现场片段</label>}
            {hasContext && <fieldset className="portal-fieldset"><legend>只让对方看这一小段</legend><label className="field">授权上下文素材<select value={assetId} disabled={blocked} onChange={event => { const asset = data.episode.assets.find(item => item.id === event.target.value); setAssetId(event.target.value); setStart('0'); setEnd(String(Math.min(30, asset?.duration ?? 5))); setFormDirty(true); }} required><option value="">选择本期素材</option>{data.episode.assets.map(item => <option key={item.id} value={item.id}>{item.filename} · {item.duration.toFixed(1)}秒</option>)}</select></label><div className="portal-two-fields"><label className="field">上下文开始（秒）<input type="number" min="0" step="0.1" value={start} disabled={!!busy} onChange={event => { setStart(event.target.value); setFormDirty(true); }} required /></label><label className="field">上下文结束（秒）<input type="number" min="0.1" step="0.1" value={end} disabled={!!busy} onChange={event => { setEnd(event.target.value); setFormDirty(true); }} required /></label></div><p className="muted">最多30秒。对方不会因此看到整期素材或其他人的草稿。</p>{source && <MediaPlayer src={mediaUrl(source.media_url)} title="待授权的上下文片段" start={Number(start) || 0} end={Number(end) || undefined} />}</fieldset>}
            {role !== 'review' && <label className="field">想问的问题（可选）<textarea rows={3} value={questions} disabled={!!busy} placeholder={'一行一个，最多8个\n比如：你当时在想什么？'} onChange={event => { setQuestions(event.target.value); setFormDirty(true); }} /><span className="muted">对方可改问题或跳过，不用回答每一个。</span></label>}
            <Button type="submit" variant="primary" disabled={blocked || !label.trim()} busy={busy === '正在准备邀请'}><Plus size={17} />创建邀请</Button><p className="portal-fineprint">生成后由你决定是否交给本人。本机地址目前只在这台电脑可用，不会自动发送。</p>
          </form>
          {created && <div className="portal-result"><strong>特邀席准备好了</strong>{createdInvite?.accepting_new ? <><p>邀请链接只在当前页面保留；请妥善交给本人。退出或刷新后不再显示。</p><label className="field">本次邀请链接<input readOnly value={link} type="password" aria-label="本次邀请链接" onFocus={event => event.target.select()} /></label><div className="toolbar"><Button disabled={blocked} onClick={() => void copyInvite()}>{copied ? <Check size={16} /> : <Copy size={16} />}{copied ? '已复制邀请链接' : '复制邀请链接'}</Button><a className="button secondary" href={link} target="_blank" rel="noreferrer">在本机打开邀请<ArrowUpRight size={15} /></a></div></> : <p>这份邀请已关闭或不再接收新内容。</p>}</div>}
        </section>
        <div className="portal-stack"><section className="portal-card"><div className="portal-section-title"><span className="portal-number mint">02</span><div><h2>已留好的位置</h2><p>关闭邀请后，本人仍可管理已提交表达与异议。</p></div></div>
          {!data.invitations.length ? <div className="portal-empty"><Users size={30} /><p>还没有邀请。先给一位朋友留个位置。</p></div> : <ul className="portal-list">{[...data.invitations].reverse().map(item => <li key={item.id}><div className="portal-item-heading"><h3>{item.label}</h3><span className={`portal-chip ${item.accepting_new ? 'mint' : ''}`}>{item.closed ? '已关闭' : item.accepting_new ? '可加入' : '已到期'}</span></div><p className="muted">{roles[item.role]} · {time(item.expires_at)} 截止</p>{item.context && <p className="portal-fineprint">授权片段 {item.context.start.toFixed(1)}–{item.context.end.toFixed(1)}秒 · {item.context_state === 'ready' ? '已准备好' : states[item.context_state] || '状态待核对'}</p>}{item.context_error && <Notice tone="error">{item.context_error.message}</Notice>}<div className="toolbar">{item.context_state === 'failed' && <Button disabled={blocked} onClick={() => void perform('重试授权片段', async () => { await api<Job>(`/collaboration/invitations/${item.id}/retry`, { method: 'POST', body: {} }); })}>重试片段准备</Button>}{!item.closed && <Button variant="ghost" disabled={blocked} onClick={() => setConfirmation({ title: `关闭给“${item.label}”的邀请？`, description: '关闭后不再接收新参与或新内容；已提交内容不会自动撤回，本人仍可撤回和处理异议。关闭后不能重新开启。', label: '确认关闭邀请', path: `/collaboration/invitations/${item.id}/close` })}>关闭邀请</Button>}</div></li>)}</ul>}
        </section><section className="portal-card"><div className="portal-section-title"><span className="portal-number pink">03</span><div><h2>本人确认后，再入片</h2><p>这里只显示已提交内容。私密草稿留在本人那里。</p></div></div>
          {!data.submissions.length ? <div className="portal-empty"><MessageCircle size={30} /><p>等朋友说完，再一起把故事补齐。</p></div> : <div className="portal-stack">{[...data.submissions].reverse().map(item => <article className="cc-submission" key={item.id}><div className="portal-item-heading"><h3>{item.display_name}<small>{item.kind === 'text' ? '文字表达' : item.kind === 'audio' ? '声音片段' : '视频片段'}</small></h3><span className="portal-chip">{item.state === 'submitted' ? '已提交' : '已撤回'}</span></div>{item.state === 'withdrawn' ? <p className="muted">本人已撤回，内容和媒体不再展示；关联采用会停用。</p> : <><p className="cc-expression">{item.text || '未附加文字表达'}</p>{item.kind !== 'text' && <MediaPlayer src={mediaUrl(item.media_url)} title={`${item.display_name}提交的原片`} />}{item.kind === 'text' && <p className="portal-fineprint">文字供本期参考，不自动转换成旁白或视频素材。</p>}<div className="cc-revisions">{item.revisions.map(rev => { const adoption = item.adoptions.find(entry => entry.revision_id === rev.id); return <div key={rev.id} className="cc-revision"><div className="portal-item-heading"><span className={`portal-chip ${rev.state === 'confirmed' ? 'mint' : ''}`}>{states[rev.state]}</span>{item.kind !== 'text' && <small>{rev.start.toFixed(1)}–{rev.end.toFixed(1)}秒</small>}</div><p>{rev.text || '无附加文字'}</p>{adoption && <><p className="portal-fineprint">采用状态：{states[adoption.state] || '待核对'}</p>{adoption.error && <Notice tone="error">{adoption.error.message}</Notice>}</>}{rev.state === 'confirmed' && item.kind !== 'text' && (!adoption || adoption.state === 'failed') && <Button disabled={blocked} onClick={() => adopt(item, rev)}>{adoption ? '重试准备这段素材' : '采用这段已确认表达'}</Button>}</div>; })}</div><Button variant="ghost" disabled={blocked} onClick={() => propose(item)}>提出文字或选段修订</Button></>}</article>)}</div>}
        </section></div>
      </div>
      <section className="portal-card cc-objections"><div className="portal-section-title"><span className="portal-number">!</span><div><h2>有意见，先听本人说</h2><p>待处理异议会限制相关版本。本人确认解决，或在制作页移除关联内容后重新确认安全版本；分享需你再明确更新。</p></div></div>{!data.objections.length ? <p className="muted">目前没有收到异议。</p> : <ul className="portal-list">{data.objections.map(item => <li key={item.id}><div className="portal-item-heading"><h3>{item.display_name}</h3><span className={`portal-chip ${item.state === 'open' ? 'pink' : 'mint'}`}>{item.state === 'open' ? '待本人确认解决' : item.state === 'resolved' ? '本人已确认解决' : '状态待核对'}</span></div><p className="cc-expression">{item.reason}</p>{item.start !== null && <p className="portal-fineprint">关联原片时间：{item.start.toFixed(1)}–{item.end?.toFixed(1)}秒</p>}<p className="portal-fineprint">发起人不能替提出者标记解决。</p></li>)}</ul>}</section>
      <Dialog open={!!revision} onClose={() => { if (!busy && (!revision || window.confirm('放弃这份尚未发送的修订？'))) setRevision(null); }} title="先请本人确认修订">{revision && <form onSubmit={saveRevision} className="portal-form"><p>旧的已确认表达保留。这份新文字或选段须本人同意后才能采用。</p><label className="field">建议表达（最多100字）<input value={revision.text} disabled={!!busy} maxLength={100} onChange={event => setRevision({ ...revision, text: event.target.value })} /></label>{revision.submission.kind !== 'text' && <><MediaPlayer src={mediaUrl(revisionSource?.media_url ?? null)} title="待确认修订的片段" start={Number(revision.start) || 0} end={Number(revision.end) || undefined} /><div className="portal-two-fields"><label className="field">修订开始（秒）<input type="number" min="0" step="0.1" value={revision.start} disabled={!!busy} onChange={event => setRevision({ ...revision, start: event.target.value })} required /></label><label className="field">修订结束（秒）<input type="number" min="0" step="0.1" value={revision.end} disabled={!!busy} onChange={event => setRevision({ ...revision, end: event.target.value })} required /></label></div></>}{feedback?.error && <Notice tone="error">{feedback.text}</Notice>}<Button type="submit" variant="primary" disabled={blocked || !revisionSource} busy={!!busy}>保存修订，等待本人确认</Button>{!revisionSource && <Notice tone="error">这份内容已撤回或不可用，不能继续提交修订。</Notice>}</form>}</Dialog>
      <Dialog open={!!confirmation} onClose={() => { if (!busy) setConfirmation(null); }} title={confirmation?.title ?? '确认操作'}>{confirmation && <><p>{confirmationValid ? confirmation.description : '这份表达已撤回、不可用或已进入采用流程，请关闭弹窗并核对最新列表。'}</p>{feedback?.error && <Notice tone="error">{feedback.text}</Notice>}<div className="dialog-actions"><Button disabled={!!busy} onClick={() => setConfirmation(null)}>取消</Button><Button variant="primary" disabled={blocked || !confirmationValid} busy={!!busy} onClick={() => { if (!confirmationValid) return; const selected = confirmation; void perform(selected.label, async () => { await request(selected.path, selected.body); }).then(ok => { if (ok && mounted.current) setConfirmation(null); }); }}>{confirmation.label}</Button></div></>}</Dialog>
    </div>
  </MediaAccessContext.Provider>;
}
