import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import CollaborationManager from '@/components/collaboration-manager';
import { AppError } from '@/lib/api';
import type { Invitation, Submission } from '@/lib/collaboration-types';
import type { Episode } from '@/lib/types';

const mock = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, api: mock.api };
});
const eid = 'e'.repeat(32), aid = 'a'.repeat(32), iid = '1'.repeat(32), sid = '2'.repeat(32), rid = '3'.repeat(32);
const date = '2026-11-08T10:00:00+00:00';
const submission = (overrides: Partial<Submission> = {}): Submission => ({ id: sid, invite_id: iid, participant_id: '4'.repeat(32), display_name: '小满', kind: 'video', state: 'submitted', text: '我来补一句', question_id: null,
  filename: '本人片段.mp4', duration: 5, size: 1000, preview_state: 'ready', preview_job_id: null, preview_error: null,
  media_url: `/api/v1/collaboration/submissions/${sid}/media`, created_at: date,
  revisions: [{ id: rid, submission_id: sid, text: '本人确认的表达', start: 0, end: 5, state: 'confirmed', created_at: date }],
  adoptions: [], can_delete_draft: false, deleted: false, deletion_pending: false, ...overrides });
const episode = { id: eid, title: '周五夜场', assets: [{ id: aid, filename: '现场.mp4', duration: 50, media_url: `/api/v1/assets/${aid}/media` }] } as Episode;
let invites: Invitation[], submissions: Submission[], readFailure: boolean;
type Options = { method?: string; body?: unknown };
async function service(path: string, options: Options = {}) {
  if (options.method === 'POST') return {};
  if (readFailure) throw new AppError('NETWORK_ERROR', '读取暂时中断');
  if (path === `/episodes/${eid}`) return structuredClone(episode);
  if (path.endsWith('/invitations')) return { items: structuredClone(invites) };
  if (path.endsWith('/submissions')) return { items: structuredClone(submissions) };
  if (path.endsWith('/objections')) return { items: [] };
  throw new Error('Unexpected test endpoint');
}
beforeEach(() => {
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  mock.api.mockReset(); mock.api.mockImplementation(service);
  invites = []; submissions = []; readFailure = false;
});
const posts = () => mock.api.mock.calls.filter(([, options]) => options?.method === 'POST');
async function open() { render(<CollaborationManager episodeId={eid} />); await screen.findByRole('heading', { name: '朋友，一起入镜。' }); }

describe('Owner collaboration and consent boundaries', () => {
  it('never displays a private draft or offers to resolve another person’s objection', async () => {
    submissions = [submission({ state: 'draft', text: '私人草稿不应出现在这里' })];
    await open();
    expect(screen.queryByText('私人草稿不应出现在这里')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /解决/ })).not.toBeInTheDocument();
    expect(posts()).toHaveLength(0);
  });

  it('rejects an overbroad context before creating an invitation and keeps the draft', async () => {
    await open();
    fireEvent.change(screen.getByLabelText('邀请用途'), { target: { value: 'interview' } });
    fireEvent.change(screen.getByLabelText('邀请称呼'), { target: { value: '小满' } });
    fireEvent.change(screen.getByLabelText('授权上下文素材'), { target: { value: aid } });
    fireEvent.change(screen.getByLabelText('上下文结束（秒）'), { target: { value: '31' } });
    fireEvent.submit(screen.getByLabelText('邀请称呼').closest('form')!);
    expect(await screen.findByText(/请选择有效素材和不超过30秒的片段/)).toBeInTheDocument();
    expect(posts()).toHaveLength(0);
    expect(screen.getByLabelText('邀请称呼')).toHaveValue('小满');
  });

  it('locks an unknown non-idempotent create instead of automatically replaying it', async () => {
    mock.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (options.method === 'POST') throw new AppError('NETWORK_ERROR', '提交连接中断', 0, true);
      return service(path, options);
    });
    await open();
    fireEvent.change(screen.getByLabelText('邀请称呼'), { target: { value: '给小满' } });
    fireEvent.click(screen.getByRole('button', { name: '创建邀请' }));
    await screen.findByRole('button', { name: '核对记录后解除锁定' });
    expect(screen.getByRole('button', { name: '创建邀请' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '重新读取' })).toBeEnabled());
    expect(posts()).toHaveLength(1);
    expect(screen.getByLabelText('邀请称呼')).toHaveValue('给小满');
    fireEvent.click(screen.getByRole('button', { name: '核对记录后解除锁定' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '创建邀请' })).toBeEnabled());
    expect(posts()).toHaveLength(1);
  });

  it('adopts only a confirmed media revision after a separate explicit confirmation', async () => {
    submissions = [submission()]; await open();
    fireEvent.click(screen.getByRole('button', { name: '采用这段已确认表达' }));
    const dialog = screen.getByRole('dialog', { name: '采用小满的这段表达？' });
    expect(posts()).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole('button', { name: '确认采用这一修订' }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe(`/collaboration/revisions/${rid}/adopt`);
    await screen.findByText('已保存，当前显示服务端最新状态。');
  });

  it('does not adopt proposed or text-only expressions', async () => {
    submissions = [submission({ kind: 'text', media_url: null }), submission({ id: '5'.repeat(32), media_url: null, revisions: [{ ...submission().revisions[0], state: 'proposed' }] })];
    await open();
    expect(screen.queryByRole('button', { name: '采用这段已确认表达' })).not.toBeInTheDocument();
    expect(screen.getByText(/文字供本期参考，不自动转换/)).toBeInTheDocument();
  });

  it('clears original and open revision players after withdrawal, retaining unsent wording', async () => {
    submissions = [submission()]; await open();
    fireEvent.click(screen.getByRole('button', { name: '提出文字或选段修订' }));
    fireEvent.change(screen.getByLabelText('建议表达（最多100字）'), { target: { value: '尚未发送的修订' } });
    submissions = [submission({ state: 'withdrawn', text: '', media_url: null, revisions: [] })];
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await screen.findByText('这份内容已撤回或不可用，不能继续提交修订。');
    expect(document.querySelectorAll('video[src]')).toHaveLength(0);
    expect(screen.getByLabelText('建议表达（最多100字）')).toHaveValue('尚未发送的修订');
    expect(screen.getByRole('button', { name: '保存修订，等待本人确认' })).toBeDisabled();
  });

  it('removes media after a read failure and on backgrounding, without losing an invitation draft', async () => {
    submissions = [submission()]; await open();
    fireEvent.change(screen.getByLabelText('邀请称呼'), { target: { value: '还没发的邀请' } });
    expect(document.querySelectorAll('video[src]')).toHaveLength(1);
    readFailure = true;
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await screen.findByText(/读取暂时中断/);
    expect(document.querySelectorAll('video[src]')).toHaveLength(0);
    expect(screen.getByRole('button', { name: '创建邀请' })).toBeDisabled();
    expect(screen.getByLabelText('邀请称呼')).toHaveValue('还没发的邀请');
    readFailure = false;
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(document.querySelectorAll('video[src]')).toHaveLength(1));
    await act(async () => { Object.defineProperty(document, 'hidden', { configurable: true, value: true }); document.dispatchEvent(new Event('visibilitychange')); });
    expect(document.querySelectorAll('video[src]')).toHaveLength(0);
    expect(screen.getByLabelText('邀请称呼')).toHaveValue('还没发的邀请');
  });
});
