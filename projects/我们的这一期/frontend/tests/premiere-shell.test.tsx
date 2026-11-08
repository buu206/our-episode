import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SessionBoundary } from '@/components/session-boundary';
import { api, AppError, setCsrf } from '@/lib/api';
import { clearWorkspaceDrafts, readWorkspaceDraft, writeWorkspaceDraft } from '@/components/studio/draft-cache';

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => router, usePathname: () => '/' }));
vi.mock('@/lib/api', async importActual => {
  const real = await importActual<typeof import('@/lib/api')>();
  return { ...real, api: vi.fn(), setCsrf: vi.fn() };
});
const status = { cloud_enabled: false, cloud_reason: '固定演示', media_ready: true, stage: '阶段3-1', workflow_enabled: true, model_mode: 'demo' };
const episodeId = 'a'.repeat(32);
beforeEach(() => {
  clearWorkspaceDrafts(); vi.mocked(api).mockReset(); vi.mocked(setCsrf).mockClear(); router.push.mockReset(); router.replace.mockReset();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  window.history.replaceState({}, '', '/');
  vi.mocked(api).mockImplementation(async path => path === '/status' ? status : { user_id: 'alice', csrf_token: 'fixture' });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('opens and closes the home account without clearing a draft and returns focus to its trigger', async () => {
  render(<SessionBoundary><label>临时想法<input defaultValue="先写下来" /></label></SessionBoundary>);
  const draft = await screen.findByLabelText('临时想法');
  fireEvent.change(draft, { target: { value: '还没有提交的聚会主题' } });
  writeWorkspaceDraft(episodeId, { draft: { title: '仍保留的片段安排', segments: [], packaging: 'light', narration_text: '' } });
  act(() => window.dispatchEvent(new CustomEvent('episode:dirty', { detail: true })));
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const trigger = screen.getByRole('button', { name: '我的账号' });
  expect(screen.getByText('本机演示 · 非真实 AI')).toBeVisible();
  await userEvent.click(trigger);
  const dialog = screen.getByRole('dialog', { name: '我的账号' });
  expect(within(dialog).getByText(/固定结果，非真实 AI/)).toBeVisible();
  await userEvent.click(within(dialog).getByRole('button', { name: '继续开场' }));
  expect(dialog).not.toHaveAttribute('open');
  expect(trigger).toHaveFocus();
  expect(draft).toHaveValue('还没有提交的聚会主题');
  expect(readWorkspaceDraft(episodeId)?.draft?.title).toBe('仍保留的片段安排');
  expect(confirm).not.toHaveBeenCalled();
  expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
});

it('confirms home logout and keeps the draft and readable dialog error until logout actually succeeds', async () => {
  let fail = true;
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/auth/logout') { if (fail) throw new AppError('NETWORK_ERROR', '退出未完成，请重试'); return { ok: true }; }
    return path === '/status' ? status : { user_id: 'alice', csrf_token: 'fixture' };
  });
  render(<SessionBoundary><label>临时想法<input defaultValue="不要丢掉" /></label></SessionBoundary>);
  const draft = await screen.findByLabelText('临时想法');
  writeWorkspaceDraft(episodeId, { draft: { title: '退出失败仍保留', segments: [], packaging: 'light', narration_text: '' } });
  act(() => window.dispatchEvent(new CustomEvent('episode:dirty', { detail: true })));
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(screen.getByRole('button', { name: '我的账号' }));
  const dialog = screen.getByRole('dialog', { name: '我的账号' });
  const logout = within(dialog).getByRole('button', { name: '退出发起人身份' });
  await userEvent.click(logout);
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(vi.mocked(api).mock.calls.some(([path]) => path === '/auth/logout')).toBe(false);
  confirm.mockReturnValue(true);
  await userEvent.click(logout);
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('退出未完成，请重试');
  expect(dialog).toHaveAttribute('open');
  expect(draft).toHaveValue('不要丢掉');
  expect(readWorkspaceDraft(episodeId)?.draft?.title).toBe('退出失败仍保留');
  expect(router.replace).not.toHaveBeenCalled();
  expect(setCsrf).not.toHaveBeenCalledWith(null);
  fail = false;
  await userEvent.click(logout);
  await waitFor(() => expect(router.replace).toHaveBeenCalledWith('/login'));
  expect(readWorkspaceDraft(episodeId)).toBeNull();
  expect(setCsrf).toHaveBeenLastCalledWith(null);
});

it('closes the home modal during permission revalidation and keeps it closed after an expired session', async () => {
  render(<SessionBoundary><p>私有首页内容</p></SessionBoundary>);
  await screen.findByText('私有首页内容');
  await userEvent.click(screen.getByRole('button', { name: '我的账号' }));
  const dialog = screen.getByRole('dialog', { name: '我的账号' });
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(dialog).not.toHaveAttribute('open');
  expect(screen.getByText('私有首页内容')).not.toBeVisible();
  vi.mocked(api).mockRejectedValue(new AppError('AUTH_REQUIRED', '会话已失效', 401));
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  expect(dialog).not.toHaveAttribute('open');
  expect(screen.queryByText('私有首页内容')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '我的账号' })).toBeDisabled();
  expect(router.replace).toHaveBeenCalledWith('/login?next=%2F');
});

it.each([
  [{ ...status, model_mode: 'cloud', workflow_enabled: false }, '本机制作 · AI 暂未接入'],
  [{ ...status, model_mode: 'cloud', cloud_enabled: true, workflow_enabled: true }, '真实 AI 已启用'],
])('shows the server capability state for a non-demo home session', async (mode, label) => {
  vi.mocked(api).mockImplementation(async path => path === '/status' ? mode : { user_id: 'alice', csrf_token: 'fixture' });
  render(<SessionBoundary><p>首页内容</p></SessionBoundary>);
  expect(await screen.findByText(label)).toBeVisible();
  expect(screen.queryByText('本机演示 · 非真实 AI')).not.toBeInTheDocument();
});
