import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ViewerPortal } from '@/components/viewer-portal';
import { api, AppError, setCsrf } from '@/lib/api';
import type { ApiOptions } from '@/lib/api';
import { sid, vid, otherSid, viewerAuth, viewerShare } from './sharing-fixtures';

const router = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => router }));
vi.mock('@/lib/api', async importActual => ({ ...await importActual<typeof import('@/lib/api')>(), api: vi.fn(), setCsrf: vi.fn() }));
let auth = viewerAuth();
let result = viewerShare();
async function service(path: string, options: ApiOptions = {}): Promise<unknown> {
  if (path === '/viewer/auth/me') return { ...auth };
  if (path === `/viewer/shares/${sid}`) return { ...result };
  if (path === '/viewer/auth/login') return { ...auth, share_id: (options.body as { share_id: string }).share_id };
  if (path === '/viewer/auth/logout') return { ok: true };
  throw new Error(`Unexpected fixture route: ${path}`);
}
const writes = () => vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST');
beforeEach(() => {
  auth = viewerAuth(); result = viewerShare();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  vi.mocked(api).mockReset().mockImplementation(service as typeof api);
  vi.mocked(setCsrf).mockClear(); router.replace.mockReset();
  window.history.replaceState({}, '', `/watch/${sid}`);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

it('strips a fragment code without auto-login or storing it and waits for viewer consent', async () => {
  window.history.replaceState({}, '', `/watch/${sid}#code=fixture-watch-code`);
  const storage = vi.spyOn(Storage.prototype, 'setItem');
  render(<StrictMode><ViewerPortal shareId={sid} /></StrictMode>);
  const code = await screen.findByLabelText('观看码');
  expect(code).toHaveValue('fixture-watch-code');
  expect(window.location.hash).toBe('');
  expect(vi.mocked(api)).not.toHaveBeenCalled();
  expect(storage).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: '进入观看' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: '进入观看' }));
  const video = await screen.findByLabelText('已经确认的一版');
  expect(video).toHaveAttribute('src', result.media_url);
  expect(writes()[0][1]?.body).toEqual({ share_id: sid, share_code: 'fixture-watch-code' });
  expect(setCsrf).toHaveBeenCalledWith('fixture-viewer-csrf', 'viewer');
});

it('loads only the viewer capability and survives StrictMode effect rehearsal', async () => {
  render(<StrictMode><ViewerPortal shareId={sid} /></StrictMode>);
  const video = await screen.findByLabelText('已经确认的一版');
  expect(video).toHaveAttribute('src', `/api/v1/viewer/shares/${sid}/media?revision=1`);
  expect(vi.mocked(api).mock.calls.every(([path]) => path.startsWith('/viewer/'))).toBe(true);
  expect(screen.queryByRole('button', { name: /导出/ })).not.toBeInTheDocument();
});

it('refuses a viewer cookie for a different invitation without requesting this media', async () => {
  auth = viewerAuth({ share_id: otherSid });
  render(<ViewerPortal shareId={sid} />);
  await screen.findByText(/当前观看身份属于另一条邀请/);
  expect(document.querySelector('video')).toBeNull();
  expect(vi.mocked(api).mock.calls.some(([path]) => path === `/viewer/shares/${sid}`)).toBe(false);
  expect(setCsrf).toHaveBeenCalledWith(null, 'viewer');
  expect(setCsrf).not.toHaveBeenCalledWith(null, 'owner');
});

it.each([
  `/api/v1/versions/${vid}/media`,
  `/api/v1/participant/submissions/${vid}/media`,
  `/api/v1/viewer/shares/${otherSid}/media?revision=1`,
  `/api/v1/viewer/shares/${sid}/media?revision=2`,
])('does not attach a media address outside this invitation and revision: %s', async media => {
  result = viewerShare({ media_url: media });
  render(<ViewerPortal shareId={sid} />);
  await screen.findByText('服务返回的媒体不属于本次观看邀请，已停止播放。');
  expect(document.querySelector('video')).toBeNull();
});

it('removes the existing source when polling sees closure and stops polling a terminal invitation', async () => {
  vi.useFakeTimers();
  let closed = false;
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (closed && path === '/viewer/auth/me') throw new AppError('SHARE_CLOSED', '邀请已关闭', 410);
    return service(path, options);
  });
  await act(async () => { render(<ViewerPortal shareId={sid} />); });
  const video = screen.getByLabelText('已经确认的一版');
  closed = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(video).not.toHaveAttribute('src');
  expect(document.querySelector('video')).toBeNull();
  expect(screen.getByText('邀请已关闭')).toBeInTheDocument();
  const reads = vi.mocked(api).mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
  expect(vi.mocked(api)).toHaveBeenCalledTimes(reads);
  expect(setCsrf).not.toHaveBeenCalledWith(null, 'viewer');
  fireEvent.click(screen.getByRole('button', { name: '退出观看身份' }));
  await act(async () => {});
  expect(writes()[0][0]).toBe('/viewer/auth/logout');
  expect(setCsrf).toHaveBeenCalledWith(null, 'viewer');
});

it('keeps restricted media stopped until the server reports an explicit new publication', async () => {
  vi.useFakeTimers();
  let restricted = false;
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (restricted && path === '/viewer/auth/me') throw new AppError('SHARE_RESTRICTED', '内容受限', 409);
    return service(path, options);
  });
  await act(async () => { render(<ViewerPortal shareId={sid} />); });
  const previous = screen.getByLabelText('已经确认的一版');
  restricted = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(previous).not.toHaveAttribute('src');
  expect(document.querySelector('video')).toBeNull();
  expect(writes()).toHaveLength(0);
  restricted = false;
  result = viewerShare({ revision: 2, title: '明确更新后的版本', media_url: `/api/v1/viewer/shares/${sid}/media?revision=2` });
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.getByLabelText('明确更新后的版本')).toHaveAttribute('src', result.media_url);
  expect(screen.getByText(/发起人已明确更新分享/)).toBeInTheDocument();
});

it('clears hidden media immediately and does not remount before foreground authorization finishes', async () => {
  render(<ViewerPortal shareId={sid} />);
  const previous = await screen.findByLabelText('已经确认的一版');
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  fireEvent(document, new Event('visibilitychange'));
  expect(previous).not.toHaveAttribute('src');
  expect(document.querySelector('video')).toBeNull();
  let finish!: (value: ReturnType<typeof viewerAuth>) => void;
  vi.mocked(api).mockImplementation((path, options) => path === '/viewer/auth/me'
    ? new Promise(resolve => { finish = resolve; }) as ReturnType<typeof api>
    : service(path, options) as ReturnType<typeof api>);
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  fireEvent(document, new Event('visibilitychange'));
  expect(document.querySelector('video')).toBeNull();
  await act(async () => { finish(auth); });
  expect(screen.getByLabelText('已经确认的一版')).toHaveAttribute('src', result.media_url);
});

it('rejects a late successful permission read after the page becomes hidden', async () => {
  let finish!: (value: ReturnType<typeof viewerShare>) => void;
  vi.mocked(api).mockImplementation((path, options) => path === `/viewer/shares/${sid}`
    ? new Promise(resolve => { finish = resolve; }) as ReturnType<typeof api>
    : service(path, options) as ReturnType<typeof api>);
  render(<ViewerPortal shareId={sid} />);
  await waitFor(() => expect(finish).toBeTypeOf('function'));
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  fireEvent(document, new Event('visibilitychange'));
  await act(async () => { finish(result); });
  expect(document.querySelector('video')).toBeNull();
});

it('stops media at the known expiry without waiting for the next poll', async () => {
  vi.useFakeTimers();
  result = viewerShare({ expires_at: Date.now() / 1000 + 1.5 });
  await act(async () => { render(<ViewerPortal shareId={sid} />); });
  const video = screen.getByLabelText('已经确认的一版');
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(video).not.toHaveAttribute('src');
  expect(screen.getByText(/该分享已到期，已停止播放/)).toBeInTheDocument();
});

it('keeps a failed media URL stopped during passive polls until an explicit playback retry', async () => {
  vi.useFakeTimers();
  await act(async () => { render(<ViewerPortal shareId={sid} />); });
  const previous = screen.getByLabelText('已经确认的一版');
  await act(async () => { fireEvent.error(previous); });
  expect(previous).not.toHaveAttribute('src');
  expect(document.querySelector('video')).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(document.querySelector('video')).toBeNull();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新核对并播放' })); });
  expect(screen.getByLabelText('已经确认的一版')).toHaveAttribute('src', result.media_url);
});

it('clears media only for its own role-expiry event and never submits a replacement login automatically', async () => {
  render(<ViewerPortal shareId={sid} />);
  const video = await screen.findByLabelText('已经确认的一版');
  act(() => window.dispatchEvent(new Event('episode:auth-expired')));
  expect(video).toHaveAttribute('src', result.media_url);
  act(() => window.dispatchEvent(new Event('episode:viewer-auth-expired')));
  expect(video).not.toHaveAttribute('src');
  expect(screen.getByLabelText('观看码')).toHaveValue('');
  expect(writes()).toHaveLength(0);
});

it('accepts a manually corrected share id and moves only to that local watch route after login', async () => {
  window.history.replaceState({}, '', `/watch/${sid}#code=fixture-watch-code`);
  render(<ViewerPortal shareId={sid} />);
  fireEvent.change(await screen.findByLabelText('分享编号'), { target: { value: otherSid } });
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: '进入观看' }));
  await waitFor(() => expect(router.replace).toHaveBeenCalledWith(`/watch/${otherSid}`));
  expect(writes()[0][1]?.body).toEqual({ share_id: otherSid, share_code: 'fixture-watch-code' });
  expect(document.querySelector('video')).toBeNull();
});

it('stops authorized playback on a network check failure without replacing the session or posting a retry', async () => {
  vi.useFakeTimers();
  let unavailable = false;
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (unavailable && path === '/viewer/auth/me') throw new AppError('NETWORK_ERROR', '无法核对观看权限');
    return service(path, options);
  });
  await act(async () => { render(<ViewerPortal shareId={sid} />); });
  const video = screen.getByLabelText('已经确认的一版');
  unavailable = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(video).not.toHaveAttribute('src');
  expect(screen.getByText('无法核对观看权限')).toBeInTheDocument();
  expect(document.querySelector('video')).toBeNull();
  expect(writes()).toHaveLength(0);
  expect(setCsrf).not.toHaveBeenCalledWith(null, 'viewer');
  unavailable = false;
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新核对并播放' })); });
  expect(screen.getByLabelText('已经确认的一版')).toHaveAttribute('src', result.media_url);
});
