'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowLeft, Copy, ExternalLink, LockKeyhole, RefreshCw, Share2 } from 'lucide-react';
import { api, AppError } from '@/lib/api';
import type { Auth, Episode } from '@/lib/types';
import type { Share, ShareCreated, Shares } from '@/lib/collaboration-types';
import { Button, Dialog, Notice, useMediaAccess } from './ui';
import styles from './sharing.module.css';

type Pending = {
  kind: 'create' | 'update' | 'close';
  path: string;
  body: Record<string, string | number | boolean>;
  target: string;
  label: string;
  oldTitle?: string;
  title: string;
  checked: boolean;
};
// Current-document memory only: no code, Cookie or CSRF. A real owner id scopes
// recovery across client-side navigation; a full reload starts with a read.
const pendingRequests = new Map<string, Pending>();
const names: Record<Share['state'], string> = { active: '可观看', restricted: '内容受限', closed: '已关闭', expired: '已过期' };
const stateName = (value: string) => names[value] || '状态待核对';
const date = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });
const message = (cause: unknown) => cause instanceof Error ? cause.message : '操作未完成，请先刷新核对状态。';

export function ShareManager({ episodeId }: { episodeId: string }) {
  const access = useMediaAccess();
  const [episode, setEpisode] = useState<Episode | null>(null);
  const [shares, setShares] = useState<Share[]>([]);
  const [readError, setReadError] = useState('');
  const [feedback, setFeedback] = useState<{ text: string; error?: boolean } | null>(null);
  const [fresh, setFresh] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [label, setLabel] = useState('');
  const [days, setDays] = useState<1 | 3 | 7>(7);
  const [versionId, setVersionId] = useState('');
  const [consent, setConsent] = useState(false);
  const [proposal, setProposal] = useState<Pending | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [updates, setUpdates] = useState<Record<string, string>>({});
  const [secret, setSecret] = useState<{ id: string; code: string } | null>(null);
  const mounted = useRef(false);
  const generation = useRef(0);
  const reader = useRef<AbortController | null>(null);
  const writer = useRef<AbortController | null>(null);
  const working = useRef(false);
  const journalKey = useRef('');
  const confirmed = episode?.versions.filter(item => item.state === 'confirmed') || [];
  const selectedId = versionId || confirmed.at(-1)?.id || '';
  const disabled = busy || !!pending || !fresh || !access.allowed;
  const dirty = !!secret || !!pending || busy || !!label.trim() || consent || days !== 7 || !!versionId || Object.values(updates).some(Boolean);

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    reader.current?.abort();
    const controller = new AbortController(); reader.current = controller;
    setRefreshing(true);
    try {
      const [auth, ep, data] = await Promise.all([
        api<Auth>('/auth/me', { signal: controller.signal }),
        api<Episode>(`/episodes/${episodeId}`, { signal: controller.signal }),
        api<Shares>(`/episodes/${episodeId}/shares`, { signal: controller.signal }),
      ]);
      if (!mounted.current || current !== generation.current || controller.signal.aborted) return;
      const key = JSON.stringify([auth.user_id, episodeId]);
      if (journalKey.current && journalKey.current !== key) setSecret(null);
      journalKey.current = key;
      setEpisode(ep); setShares(data.items); setFresh(!document.hidden); setReadError('');
      const old = pendingRequests.get(key);
      if (old) {
        const share = data.items.find(item => item.id === old.target);
        if (old.kind === 'close' && share?.state === 'closed') {
          pendingRequests.delete(key); setPending(null);
          setFeedback({ text: '已核对服务端：这条邀请已经关闭，未重复提交。' });
        } else {
          const checked = { ...old, checked: true };
          pendingRequests.set(key, checked); setPending(checked);
        }
      } else setPending(null);
    } catch (cause) {
      if (!mounted.current || current !== generation.current || controller.signal.aborted) return;
      setFresh(false); setReadError(message(cause));
      if (cause instanceof AppError && cause.status === 401) { setEpisode(null); setShares([]); setSecret(null); }
    } finally {
      if (mounted.current && current === generation.current) setRefreshing(false);
    }
  }, [episodeId]);

  useEffect(() => {
    mounted.current = true;
    queueMicrotask(() => { if (mounted.current) void refresh(); });
    const visible = () => { if (document.hidden) { setFresh(false); reader.current?.abort(); } else void refresh(); };
    const expired = () => {
      generation.current++; reader.current?.abort(); writer.current?.abort(); journalKey.current = '';
      setFresh(false); setEpisode(null); setShares([]); setSecret(null); setPending(null); setProposal(null);
    };
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('episode:auth-expired', expired);
    return () => {
      mounted.current = false; reader.current?.abort(); writer.current?.abort();
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('episode:auth-expired', expired);
      window.dispatchEvent(new Event('episode:dirty-release'));
    };
  }, [refresh]);
  useEffect(() => {
    window.dispatchEvent(new CustomEvent('episode:dirty', { detail: dirty }));
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [dirty]);

  function prepareCreate(event: FormEvent) {
    event.preventDefault();
    if (disabled) return;
    const version = confirmed.find(item => item.id === selectedId);
    if (!version || !label.trim() || !consent) { setFeedback({ error: true, text: '请选择已确认版本、填写观看称呼，并确认观看范围。' }); return; }
    setProposal({ kind: 'create', path: `/versions/${version.id}/shares`, target: version.id, label: label.trim(), title: version.title, checked: false,
      body: { label: label.trim(), expires_days: days, confirmed: true, client_request_id: crypto.randomUUID() } });
  }
  function prepareUpdate(share: Share) {
    if (disabled) return;
    const target = confirmed.find(item => item.id === (updates[share.id] || share.version_id));
    if (!target) { setFeedback({ error: true, text: '请先完整预览并确认一个可用版本。' }); return; }
    setProposal({ kind: 'update', path: `/shares/${share.id}/update`, target: share.id, label: share.label, oldTitle: share.title, title: target.title, checked: false,
      body: { version_id: target.id, expected_revision: share.revision, confirmed: true, client_request_id: crypto.randomUUID() } });
  }
  function prepareClose(share: Share) {
    if (disabled) return;
    setProposal({ kind: 'close', path: `/shares/${share.id}/close`, target: share.id, label: share.label, title: share.title, checked: false,
      body: { expected_revision: share.revision, confirmed: true } });
  }

  async function submit(operation: Pending) {
    if (working.current || !access.allowed || !fresh || !journalKey.current) return;
    working.current = true; setBusy(true); setFeedback(null); setProposal(null);
    const key = journalKey.current;
    const record = { ...operation, checked: false };
    pendingRequests.set(key, record); setPending(record);
    const controller = new AbortController(); writer.current = controller;
    try {
      const result = await api<ShareCreated>(operation.path, { method: 'POST', body: operation.body, signal: controller.signal });
      if (controller.signal.aborted) return;
      pendingRequests.delete(key);
      if (!mounted.current || key !== journalKey.current || controller.signal.aborted) return;
      setPending(null);
      if (operation.kind === 'create') {
        setLabel(''); setConsent(false);
        setSecret(result.share_code ? { id: result.id, code: result.share_code } : null);
        setFeedback({ text: result.share_code ? '观看邀请已创建，代码仅在这里显示一次。请自行保管；不会代你发送。'
          : '已核对并复用此前创建的邀请。一次性观看码不能再次读取；若未保管，请关闭该邀请后明确新建。' });
      } else setFeedback({ text: operation.kind === 'close' ? '邀请已关闭，不能重新开放。设备已接收的副本无法收回。' : `已核对本次更新，当前邀请${stateName(result.state)}。原有效期不延长。` });
      await refresh();
    } catch (cause) {
      const unknown = cause instanceof AppError && cause.uncertain;
      if (!unknown) pendingRequests.delete(key);
      if (!mounted.current || key !== journalKey.current) return;
      setPending(unknown ? record : null);
      setFeedback({ error: true, text: message(cause) });
      if (!unknown) await refresh();
    } finally {
      working.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  async function copyLink() {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(`${window.location.origin}/watch/${secret.id}#code=${encodeURIComponent(secret.code)}`);
      setFeedback({ text: '已复制本机观看链接。只在当前可访问此本机服务的设备使用，不会自动发送。' });
    } catch { setFeedback({ error: true, text: '浏览器未允许复制。可在下方选中观看码手动复制，配合分享编号进入。' }); }
  }

  return <div className="portal-page portal-stack">
    <Link className={styles.back} href={`/episodes/${episodeId}?step=preview`} onClick={event => {
      if (dirty && !window.confirm('还有未保存输入、一次性观看码或结果待核对。离开会丢失未保存输入和本页显示的观看码，服务端已保存内容保留。继续离开？')) event.preventDefault();
    }}><ArrowLeft size={16} />返回本期制作</Link>
    <header className="portal-hero"><div><span className="eyebrow">一起看，确认过的这一版</span><h1>管理观看邀请</h1><p>{episode?.title || '正在读取这一期…'}</p></div></header>
    <Notice><LockKeyhole size={16} />每条邀请只开放指定成片，不开放原片、采访或编辑权限。持码身份不等于实名；本机链接不会由应用代发。关闭只能停止后续访问，无法收回已经保存或录屏的副本。</Notice>
    <div className="toolbar"><Button variant="ghost" busy={refreshing} disabled={busy || !access.allowed} onClick={() => void refresh()}><RefreshCw size={16} />刷新分享状态</Button></div>
    {readError && <Notice tone="error">{readError} 请重新核对状态后操作。</Notice>}
    {feedback && <Notice tone={feedback.error ? 'error' : 'success'}>{feedback.text}</Notice>}
    {!episode && !readError && <p role="status">正在核对节目与观看范围…</p>}
    {pending && <section className="portal-card" aria-label="待核对的分享请求">
      <h2>{busy ? '正在提交' : '这次操作的结果待核对'}</h2><p>观看对象：{pending.label} · 成片：{pending.title}</p>
      <p>先查看服务端列表，再决定是否继续核对这一次请求。恢复使用原请求编号和原参数，不自动新建另一条邀请。</p>
      <div className="toolbar"><Button disabled={busy || refreshing || !access.allowed} onClick={() => void refresh()}>先核对已有邀请</Button>
        <Button disabled={busy || !pending.checked || !fresh || !access.allowed} onClick={() => void submit(pending)}>用原请求继续核对</Button></div>
    </section>}
    {secret && <section className={`portal-card ${styles.invitationReveal}`} aria-label="本次观看代码">
      <h2>这条邀请，准备好了</h2><p>观看码只在首次创建响应显示。先保管，再收起或离开此页。</p>
      <label className="field">分享编号<input readOnly value={secret.id} onFocus={event => event.currentTarget.select()} /></label>
      <label className="field">本次观看码<input type="password" autoComplete="off" readOnly value={secret.code} onFocus={event => event.currentTarget.select()} /></label>
      <div className="toolbar"><Button onClick={() => void copyLink()}><Copy size={16} />复制本机观看链接</Button>
        <a className="button" href={`/watch/${secret.id}#code=${encodeURIComponent(secret.code)}`} target="_blank" rel="noopener noreferrer"><ExternalLink size={16} />在本机打开观看</a>
        <Button variant="ghost" onClick={() => setSecret(null)}>收起观看码</Button></div>
      <p className="muted">同一浏览器的新标签共用观看会话。核对不同观看人的隔离时，请使用独立浏览器配置。</p>
    </section>}
    {episode && <>
      <section className="portal-card"><div className="section-title"><h2>创建观看邀请</h2><Share2 size={21} /></div>
        {!confirmed.length && <Notice>还没有可选的已确认版本。请回到制作页完整预览并确认，再来分享。</Notice>}
        <form onSubmit={prepareCreate}><fieldset className={styles.fields} disabled={disabled || !confirmed.length}>
          <div className="portal-two-fields"><label className="field">给谁看<input maxLength={40} required value={label} placeholder="例如：小林" onChange={event => setLabel(event.target.value)} /></label>
            <label className="field">有效期<select value={days} onChange={event => setDays(Number(event.target.value) as 1 | 3 | 7)}><option value={1}>1天</option><option value={3}>3天</option><option value={7}>7天</option></select></label></div>
          <label className="field">分享哪个已确认版本<select value={selectedId} required onChange={event => setVersionId(event.target.value)}><option value="" disabled>选择已确认版本</option>{confirmed.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
          <label className={styles.check}><input type="checkbox" checked={consent} required onChange={event => setConsent(event.target.checked)} /><span>我已核对这个版本，确认仅开放这份成片的观看权限。</span></label>
          <Button variant="primary" type="submit" disabled={!consent || !label.trim() || !selectedId}>确认版本并创建邀请</Button>
        </fieldset></form>
        <p className="muted">结果不确定时先核对已有邀请；刷新页面不会自动重建请求。新草案和新成片不会自动替换已分享的版本。</p>
      </section>
      <section className="portal-card"><h2>已创建的邀请</h2><p className="muted">更新需再次确认；有效期不延长。受限内容处理后，仍需你明确更新才可能恢复。</p>
        {!shares.length && <div className="portal-empty">还没有观看邀请。确认成片后，为每个观看对象分别创建。</div>}
        <ul className={styles.shareList}>{shares.map(share => <li key={share.id} className={styles.shareCard}>
          <div className="section-title"><h3>{share.label}</h3><span className={`chip ${share.state === 'active' ? 'success' : ''}`}>{stateName(share.state)}</span></div>
          <dl className={styles.facts}><div><dt>指定成片</dt><dd>{share.title}</dd></div><div><dt>发布修订</dt><dd>{share.revision}</dd></div><div><dt>到期</dt><dd>{date(share.expires_at)}</dd></div></dl>
          {share.restricted_reason && <Notice tone="error">{share.restricted_reason.message}</Notice>}
          {['active', 'restricted'].includes(share.state) ? <><details className={styles.update}><summary>明确更新观看版本</summary>
            <label className="field">给{share.label}的新版本<select disabled={disabled || !confirmed.length} value={updates[share.id] || (confirmed.some(item => item.id === share.version_id) ? share.version_id : '')} onChange={event => setUpdates(previous => ({ ...previous, [share.id]: event.target.value }))}>
              <option value="" disabled>选择已确认版本</option>{confirmed.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
            <p className="muted">旧媒体地址会失效，到期时间保持{date(share.expires_at)}。这只更新给“{share.label}”的此条邀请。</p>
            <Button disabled={disabled || !confirmed.length} onClick={() => prepareUpdate(share)}>核对更新影响</Button></details>
            <div className="toolbar"><Button variant="danger" disabled={disabled} onClick={() => prepareClose(share)}>关闭给{share.label}的邀请</Button></div></>
            : <p className="muted">{['closed', 'expired'].includes(share.state) ? '这条邀请不能重新开放；需要观看时请明确新建。' : '当前状态尚未识别，请刷新核对；不会自动开放观看。'}</p>}
        </li>)}</ul>
      </section>
    </>}
    <Dialog open={!!proposal} onClose={() => { if (!busy) setProposal(null); }} title={proposal?.kind === 'create' ? '确认创建这条观看邀请？' : proposal?.kind === 'update' ? '确认更新这条分享？' : '确认关闭这条分享？'}>
      {proposal && <><p>观看对象：{proposal.label}</p><p>{proposal.oldTitle ? `原版本：${proposal.oldTitle}` : `成片：${proposal.title}`}</p>
        {proposal.kind === 'create' && <p>只允许观看所选成片，有效期{proposal.body.expires_days}天。不会自动发送给对方，也不开放原片或采访稿件。</p>}
        {proposal.kind === 'update' && <><p>新版本：{proposal.title}</p><p>确认后旧媒体地址失效，有效期不延长。只有本条邀请改变，其他观看邀请不自动更新。</p></>}
        {proposal.kind === 'close' && <p>此邀请将停止后续观看，且不能重新打开。已经收到或录屏的副本无法收回。</p>}
        <div className="dialog-actions"><Button onClick={() => setProposal(null)}>取消</Button><Button variant={proposal.kind === 'close' ? 'danger' : 'primary'} onClick={() => void submit(proposal)}>确认{proposal.kind === 'create' ? '创建' : proposal.kind === 'update' ? '更新' : '关闭'}</Button></div></>}
    </Dialog>
  </div>;
}
