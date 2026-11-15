'use client';
import { useEffect, useState, type FormEvent } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { ArrowRight } from 'lucide-react';
import { api, setCsrf } from '@/lib/api';
import type { Auth, Status } from '@/lib/types';
import { Brand } from './session-boundary';
import { Button, ModeBanner, Notice } from './ui';
import { ProductionChrome } from './production-chrome';

export function safeNext(value: string | null) {
  return value && /^\/(?:episodes\/[a-f0-9]{32}(?:\/(?:collaboration|sharing))?(?:\?[^#\\\r\n]*)?)?$/.test(value) ? value : '/';
}
export function Login() {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState<Status | null>(null);
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get('next'));
  useEffect(() => {
    const controller = new AbortController();
    api<Status>('/status', { signal: controller.signal }).then(setStatus).catch(() => {});
    return () => controller.abort();
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy || !code.trim()) return;
    setBusy(true); setError('');
    try { const auth = await api<Auth>('/auth/login', { method: 'POST', body: { invite_code: code.trim() } }); setCsrf(auth.csrf_token); setCode(''); router.replace(next); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '暂时没能进入，请重试。'); setBusy(false); }
  }
  return <div className="production-shell login-shell">
    <ProductionChrome label="欢迎在场" />
    <header className="app-header"><Brand /><span className="header-link">日常生活 · 特别放送</span></header><ModeBanner status={status} />
    <main id="main" className="login-entry">
      <div className="hero-copy"><span className="eyebrow login-ticket">日常生活 · 特别放送</span><h1>欢迎来到，<span>你的主场。</span></h1><p>主角不用选，你们本来就是。</p></div>
      <section className="login-card"><h2>用邀请码进入创作</h2>
        <form onSubmit={submit}><label className="field">邀请码<input type="password" autoComplete="current-password" value={code} onChange={event => setCode(event.target.value)} maxLength={200} required disabled={busy} placeholder="输入你的邀请码" aria-describedby={error ? 'login-error' : 'login-help'} /></label>
          {error && <div id="login-error"><Notice tone="error">{error}</Notice></div>}
          <Button type="submit" variant="primary" busy={busy} disabled={!code.trim()} className="full-width">进入，开始这一期<ArrowRight size={18} /></Button>
        </form><p id="login-help" className="login-help">{status?.model_mode === 'demo' ? '当前为独立本机演示，使用公开测试码 demo。' : '邀请码由发起人保管。只在本机保存制作内容。'}</p>
      </section>
    </main>
  </div>;
}
