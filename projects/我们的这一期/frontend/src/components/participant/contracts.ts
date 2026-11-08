import { useEffect } from 'react';
import type { AppError } from '@/lib/api';
import type { Invitation, Objection, Submission } from '@/lib/collaboration-types';

export type ParticipantData = { invitation: Invitation; submissions: Submission[]; objections: Objection[] };
export type Outcome<T> = { ok: true; value: T } | { ok: false; error: AppError };
export type Command = <T>(action: (signal: AbortSignal) => Promise<T>, success: string) => Promise<Outcome<T>>;
export type Ask = (title: string, message: string, accept?: string) => Promise<boolean>;
export type DirtyReport = (name: string, dirty: boolean) => void;
export function useParticipantDirty(name: string, dirty: boolean, report: DirtyReport) {
  useEffect(() => { report(name, dirty); return () => report(name, false); }, [name, dirty, report]);
}
export const labelState = (state: string) => ({ draft: '仅自己可见', submitted: '已本人确认提交', withdrawn: '已撤回', uploading: '正在接收', failed: '处理失败', queued: '排队中', running: '正在准备', pending: '等待处理', ready: '已就绪', succeeded: '已完成', proposed: '等待本人决定', confirmed: '本人已确认', rejected: '本人未接受', revoked: '使用受限', open: '尚未解决', resolved: '本人已确认解决' }[state] || `状态：${state}`);
export const literal = (value: string) => !/[\p{Cc}\p{Cf}]/u.test(value);
export const pending = (value: string) => ['queued', 'running', 'pending', 'uploading'].includes(value);
