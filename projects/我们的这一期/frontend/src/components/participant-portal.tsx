'use client';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { api, AppError, setCsrf } from '@/lib/api';
import type { Invitation, Objections, ParticipantAuth, Submissions } from '@/lib/collaboration-types';
import { Button, Dialog, MediaAccessContext, Notice } from './ui';
import ParticipantWorkspace from './participant/workspace';
import { literal, pending, type Ask, type Command, type ParticipantData } from './participant/contracts';
import styles from './participant/participant.module.css';

type Confirmation = { title: string; message: string; accept: string };
export default function ParticipantPortal() {
  const [auth, setAuth] = useState<ParticipantAuth | null>(null);
  const [data, setData] = useState<ParticipantData | null>(null);
  const [code, setCode] = useState('');
  const [name, setName] = useState('');
  const [identityChecked, setIdentityChecked] = useState(false);
  const [incoming, setIncoming] = useState(false);
  const [checking, setChecking] = useState(true);
  const [frozen, setFrozen] = useState(true);
  const [visible, setVisible] = useState(true);
  const [mediaRevision, setMediaRevision] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const mounted = useRef(false);
  const readGeneration = useRef(0);
  const writeGeneration = useRef(0);
  const readController = useRef<AbortController | null>(null);
  const reading = useRef(false);
  const writeController = useRef<AbortController | null>(null);
  const currentAuth = useRef<ParticipantAuth | null>(null);
  const working = useRef(false);
  const bootstrapCode = useRef<string | null>(null);
  const confirmResult = useRef<((accepted: boolean) => void) | null>(null);
  const consentSnapshot = useRef('');
  const closeConfirm = useCallback((accepted = false) => { confirmResult.current?.(accepted); confirmResult.current = null; setConfirmation(null); }, []);
  const ask: Ask = useCallback((title, message, accept = '确认') => new Promise(resolve => {
    confirmResult.current?.(false); confirmResult.current = resolve; setConfirmation({ title, message, accept });
  }), []);
  const clearIdentity = useCallback(() => {
    readGeneration.current++; readController.current?.abort(); reading.current = false;
    writeGeneration.current++; writeController.current?.abort(); currentAuth.current = null; working.current = false;
    setCsrf(null, 'participant'); setAuth(null); setData(null); setFrozen(true); setDirty(false); setBusy(false);
    closeConfirm(); window.dispatchEvent(new CustomEvent('episode:dirty', { detail: false }));
  }, [closeConfirm]);

  const refresh = useCallback(async (freeze = false) => {
    if (!mounted.current) return false;
    const generation = ++readGeneration.current;
    reading.current = true;
    readController.current?.abort(); const controller = new AbortController(); readController.current = controller;
    if (freeze) { setFrozen(true); closeConfirm(); }
    const active = () => mounted.current && generation === readGeneration.current && !controller.signal.aborted;
    try {
      const person = await api<ParticipantAuth>('/participant/auth/me', { signal: controller.signal });
      if (!active()) return false;
      const switched = currentAuth.current && currentAuth.current.participant_id !== person.participant_id;
      if (switched) { writeGeneration.current++; writeController.current?.abort(); working.current = false; setBusy(false); closeConfirm(); setDirty(false); setData(null); setNotice('朋友身份已变化，请重新核对当前任务。此前身份的未保存内容已清除。'); }
      if (currentAuth.current?.csrf_token !== person.csrf_token) setCsrf(person.csrf_token, 'participant');
      currentAuth.current = person; setAuth(person);
      const [invitation, submissions, objections] = await Promise.all([
        api<Invitation>('/participant/invitation', { signal: controller.signal }),
        api<Submissions>('/participant/submissions', { signal: controller.signal }),
        api<Objections>('/participant/objections', { signal: controller.signal }),
      ]);
      if (!active()) return false;
      if (invitation.id !== person.invite_id || submissions.items.some(item => item.participant_id !== person.participant_id || item.invite_id !== person.invite_id) || objections.items.some(item => item.participant_id !== person.participant_id || item.invite_id !== person.invite_id)) throw new AppError('INVALID_RESPONSE', '返回内容与当前朋友身份不一致，请重新登录。', 502);
      const snapshot = JSON.stringify([person.participant_id, invitation.accepting_new, invitation.context, invitation.context_preview_url, invitation.questions, submissions.items.map(item => [item.id, item.state, item.deleted, item.text, item.question_id, item.duration, item.media_url, item.preview_state, item.revisions]), objections.items.map(item => [item.id, item.state])]);
      if (confirmResult.current && consentSnapshot.current !== snapshot) closeConfirm();
      consentSnapshot.current = snapshot;
      setData({ invitation, submissions: submissions.items, objections: objections.items });
      setFrozen(document.hidden); setChecking(false); setError(''); setAttempt(0);
      if (freeze || switched) setMediaRevision(value => value + 1);
      return true;
    } catch (cause) {
      if (!active()) return false;
      setChecking(false); setFrozen(true); closeConfirm();
      if (cause instanceof AppError && cause.status === 401) { clearIdentity(); setError(''); }
      else { setError(cause instanceof Error ? cause.message : '暂时无法读取你的任务。'); setAttempt(value => value + 1); }
      return false;
    } finally { if (generation === readGeneration.current) reading.current = false; }
  }, [clearIdentity, closeConfirm]);

  useEffect(() => {
    mounted.current = true;
    if (bootstrapCode.current === null) {
      const fragment = new URLSearchParams(window.location.hash.slice(1));
      bootstrapCode.current = fragment.get('code') || '';
      if (fragment.has('code')) window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
    }
    queueMicrotask(() => { if (mounted.current) { setVisible(!document.hidden); setCode(bootstrapCode.current || ''); setIncoming(!!bootstrapCode.current); void refresh(true); } });
    const visibility = () => {
      setVisible(!document.hidden);
      if (document.hidden) { readGeneration.current++; readController.current?.abort(); setFrozen(true); closeConfirm(); }
      else void refresh(true);
    };
    const expired = () => { readGeneration.current++; readController.current?.abort(); clearIdentity(); setChecking(false); setError('朋友会话已失效，请重新进入。'); };
    document.addEventListener('visibilitychange', visibility); window.addEventListener('episode:participant-auth-expired', expired);
    return () => {
      mounted.current = false; readController.current?.abort(); writeController.current?.abort(); confirmResult.current?.(false);
      document.removeEventListener('visibilitychange', visibility); window.removeEventListener('episode:participant-auth-expired', expired);
      window.dispatchEvent(new Event('episode:dirty-release'));
    };
  }, [refresh, clearIdentity, closeConfirm]);
  const hasPending = data && (pending(data.invitation.context_state) || data.submissions.some(item => pending(item.state) || pending(item.preview_state) || item.adoptions.some(adoption => pending(adoption.state))));
  useEffect(() => {
    if (!auth || busy || incoming || !visible) return;
    const delay = attempt ? Math.min(30000, 2000 * 2 ** Math.min(attempt - 1, 4)) : hasPending ? 2000 : 5000;
    const timer = window.setTimeout(() => { if (!reading.current) void refresh(); }, delay);
    return () => window.clearTimeout(timer);
  }, [auth, busy, incoming, hasPending, attempt, data, frozen, visible, refresh]);
  useEffect(() => { window.dispatchEvent(new CustomEvent('episode:dirty', { detail: dirty || busy })); }, [dirty, busy]);

  const command: Command = useCallback(async (action, success) => {
    if (working.current || !currentAuth.current || document.hidden) return { ok: false, error: new AppError('BUSY', '请等待当前任务完成或回到页面重试。') };
    working.current = true; setBusy(true); setError(''); setNotice('');
    const generation = ++writeGeneration.current, person = currentAuth.current.participant_id;
    const controller = new AbortController(); writeController.current = controller;
    const active = () => mounted.current && generation === writeGeneration.current && currentAuth.current?.participant_id === person && !controller.signal.aborted;
    try {
      const value = await action(controller.signal);
      if (!active()) return { ok: false, error: new AppError('ABORTED', '当前身份或页面已变化。', 0, true) };
      await refresh(); if (active()) setNotice(success);
      return { ok: true, value };
    } catch (cause) {
      const failure = cause instanceof AppError ? cause : new AppError('REQUEST_FAILED', cause instanceof Error ? cause.message : '操作未完成。', 0, true);
      if (active()) {
        await refresh();
        if (active()) setError(failure.message + (failure.uncertain ? ' 结果尚未确定，请核对下方最新状态；不会自动重复操作。' : ''));
      }
      return { ok: false, error: failure };
    } finally { if (generation === writeGeneration.current) { working.current = false; if (mounted.current) setBusy(false); } }
  }, [refresh]);

  async function login(event: FormEvent) {
    event.preventDefault(); if (working.current || !identityChecked || !code.trim() || !name.trim() || !literal(name)) return;
    working.current = true; setBusy(true); setError('');
    const generation = ++writeGeneration.current; const controller = new AbortController(); writeController.current = controller;
    try {
      const person = await api<ParticipantAuth>('/participant/auth/login', { method: 'POST', body: { invite_code: code.trim(), display_name: name.trim() }, signal: controller.signal });
      if (!mounted.current || controller.signal.aborted || generation !== writeGeneration.current) return;
      setCsrf(person.csrf_token, 'participant'); currentAuth.current = person; setAuth(person); setCode(''); bootstrapCode.current = ''; setIncoming(false); setName(''); setIdentityChecked(false);
      await refresh(true);
    } catch (cause) { if (mounted.current && !controller.signal.aborted) { setError(cause instanceof Error ? cause.message : '未能进入邀请。'); if (cause instanceof AppError && cause.uncertain) await refresh(true); } }
    finally { if (generation === writeGeneration.current) { working.current = false; if (mounted.current) setBusy(false); } }
  }
  async function logout() {
    if (working.current) return;
    if ((dirty || busy) && !await ask('退出朋友身份？', '尚未保存的文字或选中文件会丢失。已保存的私密草稿仍会保留，重新进入这份邀请后可以继续。', '退出并放弃未保存输入')) return;
    working.current = true; setBusy(true); setError('');
    const generation = ++writeGeneration.current;
    const controller = new AbortController(); writeController.current = controller;
    try {
      await api('/participant/auth/logout', { method: 'POST', body: {}, signal: controller.signal });
      if (mounted.current && generation === writeGeneration.current && !controller.signal.aborted) { clearIdentity(); setNotice('已退出朋友身份，发起人的会话不受影响。'); }
    } catch (cause) { if (mounted.current && generation === writeGeneration.current && !controller.signal.aborted) setError(cause instanceof Error ? cause.message : '退出未完成，输入仍保留。'); }
    finally { if (generation === writeGeneration.current) { working.current = false; if (mounted.current) setBusy(false); } }
  }
  const allowed = !!auth && !!data && !frozen && !incoming;
  return <div className={`portal-shell ${styles.shell}`}>
    <header className={`portal-header ${styles.header}`}><Link className="portal-brand" href="/" onClick={event => { if ((dirty || busy) && !window.confirm('未保存的输入会丢失，已保存草稿保留。返回开场？')) event.preventDefault(); }}>我们的这一期</Link><span>朋友入口 · 仅受邀范围</span>{auth && <Button disabled={busy || frozen} onClick={() => void logout()}>退出朋友身份</Button>}</header>
    <main id="main" className={`portal-main portal-page ${styles.main}`}>
      <section className="portal-hero"><div><span className="portal-kicker">YOUR SIDE OF THE STORY</span><h1>补充你的<span>那一面</span></h1><p>你确认自己的表达，发起人决定是否采用。</p></div></section>
      {error && <Notice tone="error">{error}<Button onClick={() => void refresh(true)} disabled={busy}>重新核对任务</Button></Notice>}
      {notice && <Notice>{notice}</Notice>}
      {checking && <p role="status">正在核对你的朋友会话…</p>}
      {!checking && !auth && <section className="portal-panel panel"><h2>进入受邀任务</h2><p className="muted">请使用发给你的邀请。邀请码代表受邀身份，不是实名核验；本机地址仅限当前电脑使用。</p><form onSubmit={login} className="form-stack">
        <label className="field">邀请代码<input type="password" autoComplete="off" maxLength={200} value={code} onChange={event => setCode(event.target.value)} disabled={busy} required /></label>
        <label className="field">你的称呼<input maxLength={40} value={name} onChange={event => setName(event.target.value)} disabled={busy} required /></label>
        {!literal(name) && <Notice tone="error">称呼不能含换行或隐藏控制字符。</Notice>}
        <label className="check-field"><input type="checkbox" checked={identityChecked} onChange={event => setIdentityChecked(event.target.checked)} disabled={busy} />这是发给我的邀请，我以本人身份参与。</label>
        <Button type="submit" variant="primary" busy={busy} disabled={!identityChecked || !code.trim() || !name.trim() || !literal(name)}>进入我的任务</Button>
      </form></section>}
      {auth && incoming && <Notice>你已有朋友会话：{auth.display_name}。新链接不会自动切换身份。<div className="portal-actions"><Button onClick={() => { setIncoming(false); setCode(''); bootstrapCode.current = ''; }}>继续当前任务</Button><Button onClick={() => void logout()} disabled={busy}>退出后使用这份邀请</Button></div></Notice>}
      {auth && frozen && !incoming && <Notice>正在核对权限，媒体和操作已暂停。未保存输入仍保留。{!checking && <Button disabled={busy} onClick={() => void refresh(true)}>重新连接</Button>}</Notice>}
      <MediaAccessContext.Provider value={{ allowed, revision: mediaRevision, revalidate: async () => { if (!await refresh(true)) throw new Error('尚未完成任务权限核对。'); } }}>
        {auth && data && <div key={`${auth.participant_id}:${auth.invite_id}`} hidden={!allowed} inert={!allowed}><ParticipantWorkspace data={data} name={auth.display_name} busy={busy} command={command} ask={ask} onDirty={setDirty} refresh={() => refresh(true)} /></div>}
        <Dialog open={!!confirmation && !frozen} title={confirmation?.title || '确认操作'} onClose={() => closeConfirm()}><p>{confirmation?.message}</p><div className="portal-actions"><Button onClick={() => closeConfirm()}>取消</Button><Button variant="primary" onClick={() => closeConfirm(true)}>{confirmation?.accept || '确认'}</Button></div></Dialog>
      </MediaAccessContext.Provider>
    </main>
  </div>;
}
