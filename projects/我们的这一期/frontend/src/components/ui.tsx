'use client';
import { createContext, useContext, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { AlertCircle, CheckCircle2, Clapperboard, Info, LoaderCircle, Play, X } from 'lucide-react';
import type { Status } from '@/lib/types';

export const MediaAccessContext = createContext<{ allowed: boolean; revision: number; revalidate?: () => Promise<void> }>({ allowed: true, revision: 0 });
export const useMediaAccess = () => useContext(MediaAccessContext);

export function Button({ variant = 'secondary', busy, className = '', children, disabled, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'danger' | 'ghost'; busy?: boolean }) {
  return <button type="button" {...props} disabled={disabled || busy} aria-busy={busy || undefined} className={`button ${variant} ${className}`}>
    {busy && <LoaderCircle size={17} className="spin" aria-hidden="true" />}{children}
  </button>;
}

export function Notice({ children, tone = 'info', className = '' }: { children: ReactNode; tone?: 'info' | 'error' | 'success'; className?: string }) {
  const Icon = tone === 'error' ? AlertCircle : tone === 'success' ? CheckCircle2 : Info;
  return <div className={`notice ${tone} ${className}`} role={tone === 'error' ? 'alert' : 'status'}><Icon size={18} aria-hidden="true" /><div>{children}</div></div>;
}

export function Dialog({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const { allowed } = useMediaAccess();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (open && allowed && !dialog.open) dialog.showModal();
    if ((!open || !allowed) && dialog.open) dialog.close();
    return () => { if (dialog.open) dialog.close(); if (open && allowed && trigger?.isConnected) trigger.focus(); };
  }, [open, allowed]);
  return <dialog ref={ref} className="dialog" aria-labelledby={titleId} onCancel={event => { event.preventDefault(); onClose(); }}>
    <div className="dialog-heading"><h2 id={titleId}>{title}</h2><Button variant="ghost" aria-label="关闭弹窗" onClick={onClose}><X size={20} /></Button></div>
    <div className="dialog-content">{children}</div>
  </dialog>;
}

export function MediaPlayer({ src, title, start, end, className = '', onEnded, onReady, onProgress }: { src?: string | null; title: string; start?: number; end?: number; className?: string; onEnded?: () => void; onReady?: () => void; onProgress?: (currentTime: number, duration: number) => void }) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const [visible, setVisible] = useState(true);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState('');
  const access = useMediaAccess();
  const videoRef = useRef<HTMLVideoElement>(null);
  const mounted = useRef(false);
  const delivered = useRef(false);
  const finish = () => { if (!delivered.current) { delivered.current = true; onEnded?.(); } };
  // Only relative, same-origin authorized media URLs are accepted by this component.
  const safe = src && src.startsWith('/api/v1/') && !src.includes('\\') && !/[\r\n]/.test(src) ? src : null;
  const mediaKey = `${safe}:${access.revision}`;
  const allowed = access.allowed && visible;
  useEffect(() => {
    mounted.current = true;
    const visibility = () => { if (document.hidden) videoRef.current?.pause(); setVisible(!document.hidden); };
    visibility();
    document.addEventListener('visibilitychange', visibility);
    return () => { mounted.current = false; document.removeEventListener('visibilitychange', visibility); };
  }, []);
  useEffect(() => {
    const video = videoRef.current;
    // StrictMode rehearses cleanup/setup on the same DOM node. Restore only the
    // currently authorized source removed by cleanup; hidden media stays clear.
    if (video && allowed && safe && !document.hidden && !video.hasAttribute('src')) video.setAttribute('src', safe);
    return () => { if (video) { video.pause(); video.removeAttribute('src'); video.load(); } };
  }, [allowed, safe, start, end, access.revision, failedSrc]);
  async function retry() {
    setRetrying(true); setRetryError('');
    try { await access.revalidate?.(); if (mounted.current) setFailedSrc(null); }
    catch (cause) { if (mounted.current) setRetryError(cause instanceof Error ? cause.message : '权限尚未确认，请重新读取。'); }
    finally { if (mounted.current) setRetrying(false); }
  }
  return <figure className={`media-player ${className}`}>
    <div className="media-screen">
      {safe && allowed && failedSrc !== mediaKey ? <video ref={videoRef} key={`${mediaKey}:${start}:${end}`} aria-label={title} src={safe} controls playsInline preload="metadata"
        onLoadedMetadata={event => { delivered.current = false; const video = event.currentTarget; if (start !== undefined) video.currentTime = Math.max(0, Math.min(start, video.duration)); onReady?.(); }}
        onPlay={() => { delivered.current = false; }}
        onTimeUpdate={event => { const video = event.currentTarget; onProgress?.(video.currentTime, video.duration); if (end !== undefined && video.currentTime >= end) { video.pause(); finish(); } }}
        onEnded={finish} onError={() => setFailedSrc(mediaKey)} /> : <div className="media-empty"><Play size={40} strokeWidth={1.3} aria-hidden="true" /><strong>{safe ? allowed ? '暂时无法播放这一版' : '媒体已暂停' : '这里，是这一期的主场'}</strong><span>{safe ? allowed ? '访问权限或文件状态可能已变化，请重新核对后播放。' : '回到页面后，先重新核对权限与素材状态。' : '选择素材或生成成片后，在这里预览。'}</span>{safe && allowed && <Button onClick={() => void retry()} busy={retrying}>重新核对并加载视频</Button>}</div>}
    </div><figcaption><span className="frame-mark" aria-hidden="true">▶</span>{title}</figcaption>
    {retryError && <Notice tone="error">{retryError}</Notice>}
  </figure>;
}

export function ModeBanner({ status }: { status: Status | null }) {
  if (!status) return null;
  if (status.model_mode === 'demo') return <div className="mode-banner"><Clapperboard size={16} aria-hidden="true" /><span><strong>本机流程演示</strong> · 固定结果，非真实 AI；提示音非人声旁白</span></div>;
  if (!status.workflow_enabled) return <div className="mode-banner"><Info size={16} aria-hidden="true" /><span><strong>本机制作</strong> · 可手动剪接，AI 暂未接入</span></div>;
  return null;
}
