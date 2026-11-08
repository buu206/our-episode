'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { LockKeyhole, LogOut, RefreshCw } from 'lucide-react';
import { api, AppError, mediaUrl, setCsrf } from '@/lib/api';
import type { ViewerAuth, ViewerShare } from '@/lib/collaboration-types';
import { Button, Notice } from './ui';
import styles from './sharing.module.css';

const idPattern = /^[a-f0-9]{32}$/;
const formatDate = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });
function stopVideo(video: HTMLVideoElement | null) {
  if (!video) return;
  video.pause(); video.removeAttribute('src'); video.load();
}

export function ViewerPortal({ shareId }: { shareId: string }) {
  const router = useRouter();
  const [inputId, setInputId] = useState(shareId);
  const [code, setCode] = useState('');
  const [consent, setConsent] = useState(false);
  const [identity, setIdentity] = useState<ViewerAuth | null>(null);
  const [share, setShare] = useState<ViewerShare | null>(null);
  const [phase, setPhase] = useState<'loading' | 'login' | 'ready' | 'unavailable'>('loading');
  const [issue, setIssue] = useState('');
  const [announcement, setAnnouncement] = useState('');
  const [canPlay, setCanPlay] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const mounted = useRef(false);
  const lifecycle = useRef(0);
  const generation = useRef(0);
  const reader = useRef<AbortController | null>(null);
  const writer = useRef<AbortController | null>(null);
  const working = useRef(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const poll = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const expiry = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const failedSource = useRef('');
  const lastRevision = useRef<number | null>(null);
  const failures = useRef(0);
  const fragmentRead = useRef(false);
  const fragmentCode = useRef('');
  const source = canPlay && share ? mediaUrl(share.media_url, 'viewer') : null;

  const freeze = useCallback(() => { stopVideo(videoRef.current); setCanPlay(false); }, []);
  const clearTimers = useCallback(() => { clearTimeout(poll.current); clearTimeout(expiry.current); }, []);
  const check = useCallback(async function checkLatest(options: { announce?: boolean } = {}) {
    if (!mounted.current || document.hidden || !idPattern.test(shareId) || working.current) return;
    const current = ++generation.current;
    reader.current?.abort(); clearTimeout(poll.current);
    const controller = new AbortController(); reader.current = controller;
    if (options.announce) setChecking(true);
    try {
      const auth = await api<ViewerAuth>('/viewer/auth/me', { signal: controller.signal });
      if (!mounted.current || current !== generation.current || controller.signal.aborted || document.hidden) return;
      if (auth.share_id !== shareId) {
        freeze(); clearTimers(); setCsrf(null, 'viewer'); setIdentity(null); setShare(null); setPhase('login');
        setIssue('当前观看身份属于另一条邀请。请使用这条邀请的观看码重新进入。'); return;
      }
      setCsrf(auth.csrf_token, 'viewer'); setIdentity(auth);
      const result = await api<ViewerShare>(`/viewer/shares/${shareId}`, { signal: controller.signal });
      if (!mounted.current || current !== generation.current || controller.signal.aborted || document.hidden) return;
      const expected = `/api/v1/viewer/shares/${shareId}/media?revision=${result.revision}`;
      if (result.id !== shareId || mediaUrl(result.media_url, 'viewer') !== expected) throw new AppError('INVALID_MEDIA_SCOPE', '服务返回的媒体不属于本次观看邀请，已停止播放。', 502);
      if (result.expires_at * 1000 <= Date.now()) throw new AppError('SHARE_EXPIRED', '该分享已过期，需要发起人另建邀请。', 410);
      if (lastRevision.current !== null && lastRevision.current !== result.revision) {
        freeze(); failedSource.current = '';
        setAnnouncement('发起人已明确更新分享。旧播放已停止，请核对新标题后再播放。');
      } else if (options.announce) setAnnouncement('已重新核对当前观看范围。');
      lastRevision.current = result.revision; failures.current = 0;
      setShare(result); setPhase('ready');
      const failed = failedSource.current === expected;
      setCanPlay(!failed);
      setIssue(failed ? '这份媒体暂时无法播放。当前权限已核对，可点击“重新核对并播放”再试。' : '');
      clearTimeout(expiry.current);
      expiry.current = setTimeout(() => {
        if (!mounted.current) return;
        generation.current++; reader.current?.abort(); clearTimers(); freeze();
        setPhase('unavailable'); setIssue('该分享已到期，已停止播放。需要发起人另建邀请。');
      }, Math.max(0, result.expires_at * 1000 - Date.now()));
      poll.current = setTimeout(() => void checkLatest(), 5000);
    } catch (cause) {
      if (!mounted.current || current !== generation.current || controller.signal.aborted) return;
      freeze(); clearTimeout(expiry.current);
      const error = cause instanceof AppError ? cause : new AppError('READ_FAILED', '暂时无法核对观看权限，已停止播放。');
      setIssue(error.message);
      if (error.status === 401) {
        setCsrf(null, 'viewer'); setIdentity(null); setShare(null); setPhase('login');
      } else {
        setPhase('unavailable');
        if (error.status !== 410 && error.status !== 404 && error.code !== 'INVALID_MEDIA_SCOPE') {
          failures.current = Math.min(failures.current + 1, 4);
          const delay = error.status === 409 ? 5000 : Math.min(5000 * 2 ** (failures.current - 1), 30000);
          poll.current = setTimeout(() => void checkLatest(), delay);
        }
      }
    } finally {
      if (mounted.current && current === generation.current) setChecking(false);
    }
  }, [shareId, clearTimers, freeze]);

  useEffect(() => {
    mounted.current = true;
    const life = ++lifecycle.current;
    queueMicrotask(() => {
      if (!mounted.current || life !== lifecycle.current) return;
      if (!fragmentRead.current) {
        fragmentRead.current = true;
        const values = new URLSearchParams(window.location.hash.slice(1));
        const supplied = values.get('code');
        if (supplied !== null) {
          fragmentCode.current = supplied.length <= 200 ? supplied : '';
          window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
        }
      }
      if (fragmentCode.current) {
        setCode(fragmentCode.current); fragmentCode.current = ''; setPhase('login');
        setAnnouncement('已从本次链接读取观看码，请核对分享范围后进入。');
      } else if (!idPattern.test(shareId)) {
        setPhase('login'); setIssue('分享编号不完整，请核对发起人提供的编号与观看码。');
      } else void check();
    });
    const visibility = () => {
      generation.current++; reader.current?.abort(); clearTimers(); freeze();
      if (!document.hidden) void check({ announce: true });
    };
    const expired = () => {
      generation.current++; reader.current?.abort(); clearTimers(); freeze();
      setIdentity(null); setShare(null); setPhase('login'); setChecking(false);
      setIssue('观看会话已失效，请使用这条邀请的观看码重新进入。');
    };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('episode:viewer-auth-expired', expired);
    return () => {
      mounted.current = false; lifecycle.current = life + 1;
      reader.current?.abort(); writer.current?.abort(); clearTimers();
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('episode:viewer-auth-expired', expired);
    };
  }, [shareId, check, clearTimers, freeze]);
  useEffect(() => {
    const video = videoRef.current;
    if (video && source && !document.hidden && !video.hasAttribute('src')) video.setAttribute('src', source);
    return () => stopVideo(video);
  }, [source]);

  async function login(event: FormEvent) {
    event.preventDefault();
    if (working.current || !consent) return;
    const requested = inputId.trim();
    if (!idPattern.test(requested) || !code.trim()) { setIssue('请填写完整分享编号和观看码。'); return; }
    working.current = true; setBusy(true); setIssue(''); setAnnouncement('');
    generation.current++; reader.current?.abort(); clearTimers(); freeze();
    const controller = new AbortController(); writer.current = controller;
    try {
      const auth = await api<ViewerAuth>('/viewer/auth/login', { method: 'POST', body: { share_id: requested, share_code: code.trim() }, signal: controller.signal });
      if (!mounted.current) return;
      if (auth.share_id !== requested) throw new AppError('INVALID_VIEWER_SCOPE', '观看身份与请求邀请不一致，请重新核对。', 502);
      setCsrf(auth.csrf_token, 'viewer'); setIdentity(auth); setCode(''); fragmentCode.current = '';
      setConsent(false); lastRevision.current = null; failedSource.current = '';
      if (requested !== shareId) { router.replace(`/watch/${requested}`); return; }
      working.current = false;
      await check({ announce: true });
    } catch (cause) {
      if (mounted.current) { setPhase('login'); setIssue(cause instanceof Error ? cause.message : '未能进入这条观看邀请，请重试。'); }
    } finally { working.current = false; if (mounted.current) setBusy(false); }
  }

  async function logout() {
    if (working.current) return;
    working.current = true; setBusy(true); generation.current++; reader.current?.abort(); clearTimers(); freeze();
    const controller = new AbortController(); writer.current = controller;
    try {
      await api('/viewer/auth/logout', { method: 'POST', body: {}, signal: controller.signal });
      if (!mounted.current) return;
      setCsrf(null, 'viewer'); setIdentity(null); setShare(null); setCode(''); setConsent(false); setPhase('login'); setIssue('');
      setAnnouncement('已退出本次观看身份。'); lastRevision.current = null;
    } catch (cause) { if (mounted.current) setIssue(cause instanceof Error ? cause.message : '退出结果未确定，媒体保持停止，请重试。'); }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }

  function retryMedia() { failedSource.current = ''; void check({ announce: true }); }
  function mediaFailed() { failedSource.current = source || ''; freeze(); void check({ announce: true }); }

  return <div className={`portal-page ${styles.watch}`}>
    <header className={`portal-header ${styles.watchHeader}`}><Link href={`/watch/${shareId}`} className={styles.brand}>我们的这一期</Link><span className="chip">受邀观看 · 独立身份</span>
      {identity && <Button variant="ghost" busy={busy} onClick={() => void logout()}><LogOut size={16} />退出观看身份</Button>}</header>
    <main id="main"><header className="portal-hero"><div><span className="eyebrow">把确认过的片段，一起看</span><h1>{share?.title && phase !== 'login' ? share.title : '一起看这一版'}</h1><p>只属于这条邀请的观看范围。</p></div></header>
      {announcement && <Notice>{announcement}</Notice>}
      {issue && <Notice tone="error">{issue}</Notice>}
      {phase === 'loading' && <p role="status">正在核对观看身份与版本…</p>}
      {(phase === 'login' || (!identity && phase === 'unavailable')) && <section className={`portal-card ${styles.login}`}><h2>进入受邀观看</h2>
        <p>观看码只授权对应成片，不授予原片、采访稿件或编辑权限。持有同一码的人拥有同一观看范围。</p>
        <form onSubmit={login}><label className="field">分享编号<input value={inputId} maxLength={80} required autoComplete="off" disabled={busy} onChange={event => setInputId(event.target.value)} /></label>
          <label className="field">观看码<input type="password" value={code} maxLength={200} required autoComplete="off" disabled={busy} onChange={event => setCode(event.target.value)} /></label>
          <label className={styles.check}><input type="checkbox" checked={consent} disabled={busy} required onChange={event => setConsent(event.target.checked)} /><span>这是发给我的观看邀请，我会尊重参与者的分享范围。</span></label>
          <Button variant="primary" type="submit" busy={busy} disabled={!consent || !inputId.trim() || !code.trim()}>进入观看</Button></form>
        <p className="muted">同一浏览器的标签页共享观看身份。进入另一条邀请后，旧标签需重新核对；发起人与朋友供材身份不会代替观看码。</p>
      </section>}
      {phase !== 'login' && phase !== 'loading' && <section className="portal-card">
        <div className="section-title"><h2>当前观看范围</h2><Button variant="ghost" busy={checking} disabled={busy} onClick={retryMedia}><RefreshCw size={16} />重新核对并播放</Button></div>
        {share && <p className="muted">给{share.label}的邀请 · 发布修订{share.revision} · 到期{formatDate(share.expires_at)}</p>}
        {source ? <video ref={videoRef} key={source} className={styles.watchVideo} aria-label={share?.title || '受邀成片'} src={source} controls controlsList="nodownload" playsInline preload="metadata" onError={mediaFailed} />
          : <div className={styles.unavailable}><LockKeyhole size={28} /><p>媒体已停止。只有重新通过服务端权限核对，才会加载当前授权版本。</p></div>}
      </section>}
      <section className={`portal-card ${styles.scope}`}><p>只提供这条邀请的成片观看入口，不开放原片、采访稿件或编辑。邀请关闭、到期、内容有异议或被撤回时，会停止后续访问。</p>
        <p>分享更新由发起人明确确认，不会自动跟随新版本。页面在前台定期核对，离开前台会停播；网络中断或核对失败时也保持停止。</p>
        <p>请勿擅自扩大传播范围。可播放的内容仍可能被保存或录屏，产品不能收回已接收的副本。当前为本机验收地址，不会代你发送邀请。</p></section>
    </main>
  </div>;
}
