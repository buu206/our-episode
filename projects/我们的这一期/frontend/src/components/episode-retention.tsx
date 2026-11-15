'use client';
import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api';
import type { Retention } from '@/lib/types';
import { Button, Dialog, Notice } from './ui';

export function EpisodeRetention({ episodeId, initial, retired = false, onRetired }: {
  episodeId: string; initial?: Retention; retired?: boolean; onRetired: () => void;
}) {
  const [info, setInfo] = useState(initial);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const reload = useCallback(async (signal?: AbortSignal) => {
    const value = await api<Retention>(`/episodes/${episodeId}/retention`, { signal });
    if (signal?.aborted) return;
    setInfo(value); setError('');
    if (!retired && (value.deletion_requested || (value.expires_at !== null && value.expires_at * 1000 <= Date.now()))) onRetired();
  }, [episodeId, onRetired, retired]);
  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => { void reload(controller.signal).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '未能取得清理状态。');
    }); };
    // A visible tab checks the deadline even when no model/render job is running.
    const timer = window.setInterval(refresh, retired ? 3000 : 30000);
    if (retired) refresh();
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [reload, retired]);

  async function remove() {
    if (busy) return;
    setBusy(true); setError('');
    try {
      await api(`/episodes/${episodeId}?confirm=true`, { method: 'DELETE' });
      setConfirm(false);
      if (retired) await reload();
      else onRetired();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '请求未完成，请刷新核对。');
    } finally { setBusy(false); }
  }
  return <section className={`panel retention-panel${retired ? " retired" : ""}`} aria-label="本期保存与删除">
    {retired ? <><h2>本期已停止访问</h2><p role="status">{!info || error ? '正在核对清理状态。' : info.deletion_requested && !info.cleanup_pending ? '应用内节目内容已清理。' : '内容已停止访问，正在等待任务结束或重试清理。'}</p><Link className="button secondary" href="/#recent">返回往期</Link></> :
      <div className="toolbar"><p className="muted">{info?.expires_at ? `应用副本保存至 ${new Date(info.expires_at * 1000).toLocaleString('zh-CN')}，到期自动删除；修改不续期。` : '正在核对本期保存期限。'}</p><Button disabled={busy || !info} onClick={() => { void reload().then(() => setConfirm(true)).catch(cause => setError(cause instanceof Error ? cause.message : '未取得删除范围。')); }}>删除这一期</Button></div>}
    {error && <Notice tone="error">{error}</Notice>}
    {retired && info?.cleanup_pending && <Button disabled={busy} onClick={() => { void remove(); }}>重试清理</Button>}
    {retired && <p className="muted">外部源视频不受影响。已下载副本和供应商留存不由此处删除；费用记录继续保留用于对账。</p>}
    <Dialog open={confirm} title="删除这一期？" onClose={() => { if (!busy) setConfirm(false); }}>
      <p>将清理本期 {info?.counts.assets ?? 0} 份素材、{info?.counts.versions ?? 0} 个版本及 {info?.counts.submissions ?? 0} 份投稿/采访，同时关闭全部朋友入口与 {info?.counts.shares ?? 0} 个分享。</p>
      <p>分析结果、旁白、未保存草案和应用内副本会一并移除，无法恢复。外部源视频不动；已下载副本和模型供应商副本不在此删除范围。费用记录继续保留。</p>
      <div className="toolbar"><Button disabled={busy} onClick={() => setConfirm(false)}>取消</Button><Button variant="primary" disabled={busy} busy={busy} onClick={() => { void remove(); }}>确认删除整期</Button></div>
    </Dialog>
  </section>;
}
