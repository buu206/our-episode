'use client';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { BrandMark } from './brand-mark';
import { ProductionChrome } from './production-chrome';
import { FloatingDirector, clearDirectorChatMemory } from './floating-director';
import { usePathname, useRouter } from 'next/navigation';
import { LockKeyhole, LogOut, MoreHorizontal } from 'lucide-react';
import { api, AppError, setCsrf } from '@/lib/api';
import type { Auth, Status } from '@/lib/types';
import { Button, Dialog, MediaAccessContext, ModeBanner, Notice } from './ui';
import { clearWorkspaceDrafts, discardWorkspaceDraft, setDraftOwner } from './studio/draft-cache';

export function Brand() {
  return <span className="brand"><BrandMark /><span>在场<small>ZAICHANG</small></span></span>;
}

function PremiereChrome({ status }: { status: Status | null }) {
  return <div className="premiere-chrome">
    <div className="chrome-dots" aria-hidden="true"><i /><i /><i /></div>
    <div className="chrome-address" aria-label="装饰浏览器地址栏，非实际访问地址"><LockKeyhole size={12} aria-hidden="true" /><span>在场 / 开场</span></div>
    <span className="chrome-mode">{!status ? '正在核对运行状态' : status.model_mode === 'demo' ? '本机演示 · 非真实 AI' : status.cloud_enabled && status.workflow_enabled ? '真实 AI 已启用' : '本机制作 · AI 按本期授权启用'}</span>
    <MoreHorizontal size={22} className="chrome-more" aria-hidden="true" />
  </div>;
}

/** Lives in the root layout, so route restoration cannot remove the leave guard. */
export function NavigationGuard() {
  useLayoutEffect(() => {
    let dirty = false;
    let signalGeneration = 0;
    let ownerPath = window.location.pathname;
    const release = () => {
      const generation = signalGeneration;
      queueMicrotask(() => { if (signalGeneration === generation && window.location.pathname !== ownerPath) dirty = false; });
    };
    const edit = (event: Event) => {
      if (Boolean((event as CustomEvent<boolean>).detail)) { signalGeneration++; dirty = true; ownerPath = window.location.pathname; }
      else if (dirty && ownerPath !== window.location.pathname) release();
      else { signalGeneration++; dirty = false; ownerPath = window.location.pathname; }
    };
    const expired = () => { signalGeneration++; dirty = false; clearWorkspaceDrafts(); };
    const navigate = (event: NavigateEvent) => {
      const destination = new URL(event.destination.url);
      if (!dirty || event.navigationType !== 'traverse' || destination.origin !== window.location.origin || destination.pathname === window.location.pathname || !event.cancelable) return;
      if (!window.confirm('还有未保存的修改或等待结果的操作。确认离开将放弃未保存修改，已保存内容保留。继续离开？')) event.preventDefault();
      else { discardWorkspaceDraft(window.location.pathname); signalGeneration++; dirty = false; }
    };
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('episode:dirty', edit);
    window.addEventListener('episode:dirty-release', release);
    window.addEventListener('episode:auth-expired', expired);
    window.navigation?.addEventListener('navigate', navigate);
    window.addEventListener('beforeunload', beforeUnload);
    return () => {
      window.removeEventListener('episode:dirty', edit);
      window.removeEventListener('episode:dirty-release', release);
      window.removeEventListener('episode:auth-expired', expired);
      window.navigation?.removeEventListener('navigate', navigate);
      window.removeEventListener('beforeunload', beforeUnload);
    };
  }, []);
  return null;
}

export function SessionBoundary({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [userId, setUserId] = useState<string | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [frozen, setFrozen] = useState(true);
  const [mediaRevision, setMediaRevision] = useState(0);
  const router = useRouter();
  const pathname = usePathname();
  const isHome = pathname === '/';
  const dirty = useRef(false);
  const authGeneration = useRef(0);
  const authController = useRef<AbortController | null>(null);
  const invalidateAuth = useCallback(() => { authGeneration.current++; authController.current?.abort(); }, []);
  const redirect = useCallback(() => {
    clearDirectorChatMemory(); window.dispatchEvent(new Event('director:reset'));
    clearWorkspaceDrafts();
    setReady(false); setFrozen(true); setStatus(null);
    setCsrf(null);
    const next = pathname + (typeof window !== 'undefined' ? window.location.search : '');
    router.replace('/login?next=' + encodeURIComponent(next));
  }, [pathname, router]);
  const revalidate = useCallback(async () => {
    const generation = ++authGeneration.current;
    authController.current?.abort();
    const controller = new AbortController();
    authController.current = controller;
    setFrozen(true); setError('');
    try {
      const [auth, mode] = await Promise.all([api<Auth>('/auth/me', { signal: controller.signal }), api<Status>('/status', { signal: controller.signal })]);
      if (generation !== authGeneration.current || controller.signal.aborted) return;
      setCsrf(auth.csrf_token); setDraftOwner(auth.user_id); setUserId(auth.user_id); setStatus(mode); setReady(true); setFrozen(document.hidden); setMediaRevision(value => value + 1);
    } catch (cause) {
      if (generation !== authGeneration.current || controller.signal.aborted) return;
      if (cause instanceof AppError && cause.status === 401) redirect();
      else setError(cause instanceof Error ? cause.message : '暂时无法连接导演台。');
      throw cause;
    }
  }, [redirect]);
  useEffect(() => {
    let current = true;
    queueMicrotask(() => { if (current) void revalidate().catch(() => {}); });
    const visibility = () => {
      if (document.hidden) { setFrozen(true); authGeneration.current++; authController.current?.abort(); }
      else void revalidate().catch(() => {});
    };
    document.addEventListener('visibilitychange', visibility);
    return () => { current = false; invalidateAuth(); document.removeEventListener('visibilitychange', visibility); };
  }, [revalidate, invalidateAuth]);
  useEffect(() => {
    const handler = (event: Event) => { dirty.current = Boolean((event as CustomEvent<boolean>).detail); };
    const expired = () => { authGeneration.current++; authController.current?.abort(); dirty.current = false; redirect(); };
    window.addEventListener('episode:dirty', handler);
    window.addEventListener('episode:auth-expired', expired);
    return () => { window.removeEventListener('episode:dirty', handler); window.removeEventListener('episode:auth-expired', expired); };
  }, [redirect]);
  function allowLeave(discard = true) {
    if (dirty.current && !window.confirm('还有未保存的修改或等待结果的操作。确认离开将放弃未保存修改，已保存内容保留。继续离开？')) return false;
    if (discard) discardWorkspaceDraft(pathname);
    return true;
  }
  async function logout() {
    if (busy || !allowLeave(false)) return;
    setBusy(true); setError('');
    try { await api('/auth/logout', { method: 'POST', body: {} }); clearWorkspaceDrafts(); clearDirectorChatMemory(); window.dispatchEvent(new Event('director:reset')); setCsrf(null); router.replace('/login'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '退出未完成，请重试。'); }
    finally { setBusy(false); }
  }
  return <div className={isHome ? 'premiere-shell' : 'production-shell'}>
    {isHome ? <PremiereChrome status={status} /> : <ProductionChrome label={pathname.endsWith("/collaboration") ? "朋友一起" : pathname.endsWith("/sharing") ? "私密放映" : "制作现场"} />}
    {isHome ? <header className="premiere-header">
      <Link href="/" className="premiere-brand" aria-label="在场，返回开场"><BrandMark />在场</Link>
      <nav aria-label="主导航"><Link href="/" aria-current="page">开场</Link><a href="#recent">往期</a><a href="#how-it-works">怎么玩</a></nav>
      <Button className="account-button" onClick={() => setAccountOpen(true)} disabled={!ready}>我的账号</Button>
    </header> : <header className="app-header"><Link href="/" aria-label="在场，返回节目列表" onClick={event => { if (!allowLeave()) event.preventDefault(); }}><Brand /></Link>
      <nav aria-label="主导航"><Link href="/" className={`header-link ${pathname === '/' ? 'current' : ''}`} onClick={event => { if (pathname !== '/' && !allowLeave()) event.preventDefault(); }}>创作</Link><Link href="/#recent" className="header-link" onClick={event => { if (pathname !== '/' && !allowLeave()) event.preventDefault(); }}>我的节目</Link>{ready && <Button variant="ghost" busy={busy} onClick={logout} aria-label="退出发起人身份"><LogOut size={16} /><span className="desktop-label">退出</span></Button>}</nav>
    </header>}
    {!isHome && <ModeBanner status={status} />}
    <main id="main" className={`app-main ${pathname === '/' ? 'home-main' : ''}`}>
      {error && <Notice tone="error">{error} {ready && frozen && '未保存编辑仍保留，媒体和操作暂时停用。'}<Button variant="ghost" onClick={() => void revalidate().catch(() => {})}>重新连接</Button></Notice>}
      {(!ready || frozen) && !error && <div className="loading-page" role="status"><span className="loading-reel" />正在核对会话与媒体权限…</div>}
      <MediaAccessContext.Provider value={{ allowed: ready && !frozen, revision: mediaRevision, revalidate }}>
        {ready && <div key={userId} hidden={frozen} inert={frozen}>{children}</div>}
        {ready && userId && <FloatingDirector ownerId={userId} status={status} allowed={!frozen} />}
      </MediaAccessContext.Provider>
    </main>
    <footer className="app-footer"><span>ZAICHANG <span aria-hidden="true">✳</span></span><span>生活没有剧本，但可以有片头。</span></footer>
    {isHome && <Dialog open={accountOpen && ready && !frozen} onClose={() => setAccountOpen(false)} title="我的账号">
      <p>当前制作会话已就绪。这台电脑上的节目和素材会保留，下次进入可以继续制作。</p>
      <ModeBanner status={status} />
      {error && <Notice tone="error">{error}</Notice>}
      <div className="dialog-actions"><Button variant="ghost" onClick={() => setAccountOpen(false)}>继续开场</Button><Button busy={busy} onClick={logout} aria-label="退出发起人身份"><LogOut size={16} />退出账号</Button></div>
    </Dialog>}
  </div>;
}
