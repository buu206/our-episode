import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ShareManager } from '@/components/share-manager';
import { api, AppError } from '@/lib/api';
import type { ApiOptions } from '@/lib/api';
import type { Share } from '@/lib/collaboration-types';
import { eid, sid, vid, nextVid, episode, share, version } from './sharing-fixtures';

vi.mock('@/lib/api', async importActual => ({ ...await importActual<typeof import('@/lib/api')>(), api: vi.fn() }));
let current = episode();
let shares: Share[] = [];
let owner = '';
let count = 0;
const writes = () => vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST');
async function service(path: string, options: ApiOptions = {}): Promise<unknown> {
  if (path === '/auth/me') return { user_id: owner, csrf_token: 'fixture-owner-csrf' };
  if (path === `/episodes/${eid}`) return structuredClone(current);
  if (path === `/episodes/${eid}/shares`) return { items: structuredClone(shares) };
  if (path === `/versions/${vid}/shares` && options.method === 'POST') {
    const result = share(); shares = [result];
    return { ...result, share_code: 'fixture-watch-code', watch_url: `/watch/${sid}#code=fixture-watch-code` };
  }
  throw new Error(`Unexpected fixture route: ${path}`);
}
beforeEach(() => {
  count++; owner = `fixture-owner-${count}`;
  current = episode(); shares = [];
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.mocked(api).mockReset().mockImplementation(service as typeof api);
  window.history.replaceState({}, '', `/episodes/${eid}/sharing`);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
async function prepareCreate() {
  await screen.findByText('朋友的一期');
  fireEvent.change(screen.getByLabelText('给谁看'), { target: { value: '小林' } });
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: '确认版本并创建邀请' }));
  return screen.getByRole('dialog', { name: '确认创建这条观看邀请？' });
}

it('offers only confirmed versions and requires a concrete confirmation before creating a one-time code', async () => {
  render(<ShareManager episodeId={eid} />);
  await screen.findByText('朋友的一期');
  expect(screen.queryByRole('option', { name: '尚未确认的草案' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认版本并创建邀请' })).toBeDisabled();
  const dialog = await prepareCreate();
  expect(within(dialog).getByText('观看对象：小林')).toBeInTheDocument();
  expect(within(dialog).getByText(/有效期7天/)).toBeInTheDocument();
  expect(writes()).toHaveLength(0);
  fireEvent.click(within(dialog).getByRole('button', { name: '确认创建' }));
  expect(await screen.findByLabelText('本次观看码')).toHaveValue('fixture-watch-code');
  expect(writes()).toHaveLength(1);
  expect(writes()[0][1]?.body).toMatchObject({ label: '小林', expires_days: 7, confirmed: true, client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/) });
  expect(screen.getByRole('link', { name: '在本机打开观看' })).toHaveAttribute('href', `/watch/${sid}#code=fixture-watch-code`);
  fireEvent.click(screen.getByRole('button', { name: '收起观看码' }));
  expect(screen.queryByLabelText('本次观看码')).not.toBeInTheDocument();
});

it('does not blindly repeat an uncertain create and resumes only the identical UUID and payload after reading', async () => {
  let attempts = 0;
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === `/versions/${vid}/shares` && options?.method === 'POST') {
      attempts++; shares = [share()];
      if (attempts === 1) throw new AppError('NETWORK_ERROR', '创建结果未知', 0, true);
      return { ...shares[0], share_code: null, watch_url: null };
    }
    return service(path, options);
  });
  render(<ShareManager episodeId={eid} />);
  const dialog = await prepareCreate(); fireEvent.click(within(dialog).getByRole('button', { name: '确认创建' }));
  await screen.findByText('创建结果未知');
  expect(screen.getByLabelText('给谁看')).toBeDisabled();
  expect(screen.getByRole('button', { name: '用原请求继续核对' })).toBeDisabled();
  expect(writes()).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '先核对已有邀请' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '用原请求继续核对' })).toBeEnabled());
  expect(writes()).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '用原请求继续核对' }));
  await screen.findByText(/一次性观看码不能再次读取/);
  expect(writes()).toHaveLength(2);
  expect(writes()[1][1]?.body).toEqual(writes()[0][1]?.body);
  expect(screen.queryByLabelText('本次观看码')).not.toBeInTheDocument();
});

it('retains an unresolved request across route remounts without sending it automatically', async () => {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') throw new AppError('NETWORK_ERROR', '尚待核对', 0, true);
    return service(path, options);
  });
  const first = render(<ShareManager episodeId={eid} />);
  const dialog = await prepareCreate(); fireEvent.click(within(dialog).getByRole('button', { name: '确认创建' }));
  await screen.findByText('尚待核对');
  const body = writes()[0][1]?.body;
  first.unmount();
  render(<ShareManager episodeId={eid} />);
  await screen.findByRole('region', { name: '待核对的分享请求' });
  expect(writes()).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '用原请求继续核对' }));
  await waitFor(() => expect(writes()).toHaveLength(2));
  expect(writes()[1][1]?.body).toEqual(body);
});

it('never exposes another owner’s in-memory recovery request', async () => {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') throw new AppError('NETWORK_ERROR', '尚待核对', 0, true);
    return service(path, options);
  });
  const first = render(<ShareManager episodeId={eid} />);
  const dialog = await prepareCreate(); fireEvent.click(within(dialog).getByRole('button', { name: '确认创建' }));
  await screen.findByText('尚待核对'); first.unmount(); owner = 'different-owner';
  render(<ShareManager episodeId={eid} />);
  await screen.findByText('朋友的一期');
  expect(screen.queryByRole('region', { name: '待核对的分享请求' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('给谁看')).toHaveValue('');
  expect(writes()).toHaveLength(1);
});

it('confirms old/new versions and reuses an uncertain update request without changing its expected revision', async () => {
  current.versions.push(version({ id: nextVid, title: '已确认新版' }));
  current.versions = current.versions.filter(item => item.state === 'confirmed');
  shares = [share()]; const expires = shares[0].expires_at;
  let attempts = 0;
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === `/shares/${sid}/update`) {
      attempts++; shares = [share({ version_id: nextVid, title: '已确认新版', revision: 2, expires_at: expires })];
      if (attempts === 1) throw new AppError('NETWORK_ERROR', '更新结果未知', 0, true);
      return shares[0];
    }
    return service(path, options);
  });
  render(<ShareManager episodeId={eid} />);
  const select = await screen.findByLabelText('给小林的新版本');
  fireEvent.change(select, { target: { value: nextVid } });
  fireEvent.click(screen.getByRole('button', { name: '核对更新影响' }));
  const dialog = screen.getByRole('dialog', { name: '确认更新这条分享？' });
  expect(within(dialog).getByText('原版本：已经确认的一版')).toBeInTheDocument();
  expect(within(dialog).getByText('新版本：已确认新版')).toBeInTheDocument();
  expect(writes()).toHaveLength(0);
  fireEvent.click(within(dialog).getByRole('button', { name: '确认更新' }));
  await screen.findByText('更新结果未知');
  fireEvent.click(screen.getByRole('button', { name: '先核对已有邀请' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '用原请求继续核对' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '用原请求继续核对' }));
  await screen.findByText(/当前邀请可观看/);
  expect(writes()[0][1]?.body).toMatchObject({ version_id: nextVid, expected_revision: 1, confirmed: true });
  expect(writes()[1][1]?.body).toEqual(writes()[0][1]?.body);
  expect(shares[0].expires_at).toBe(expires);
});

it('reconciles a lost close response by reading the closed state without submitting again', async () => {
  shares = [share()];
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === `/shares/${sid}/close`) { shares = [share({ state: 'closed' })]; throw new AppError('NETWORK_ERROR', '关闭结果未知', 0, true); }
    return service(path, options);
  });
  render(<ShareManager episodeId={eid} />);
  fireEvent.click(await screen.findByRole('button', { name: '关闭给小林的邀请' }));
  expect(writes()).toHaveLength(0);
  const dialog = screen.getByRole('dialog', { name: '确认关闭这条分享？' });
  fireEvent.click(within(dialog).getByRole('button', { name: '确认关闭' }));
  await screen.findByText('关闭结果未知');
  fireEvent.click(screen.getByRole('button', { name: '先核对已有邀请' }));
  await screen.findByText('已核对服务端：这条邀请已经关闭，未重复提交。');
  expect(writes()).toHaveLength(1);
  expect(writes()[0][1]?.body).toEqual({ expected_revision: 1, confirmed: true });
  expect(screen.queryByRole('button', { name: '关闭给小林的邀请' })).not.toBeInTheDocument();
});

it('does not enable operations for unknown future share states or unconfirmed-only versions', async () => {
  shares = [share({ state: 'future-review-state' })];
  current.versions = [version({ state: 'ready' })];
  render(<ShareManager episodeId={eid} />);
  await screen.findByText('状态待核对');
  expect(screen.getByRole('button', { name: '确认版本并创建邀请' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: '核对更新影响' })).not.toBeInTheDocument();
  expect(writes()).toHaveLength(0);
});

it('removes the one-time secret and private share list on owner session expiry', async () => {
  render(<ShareManager episodeId={eid} />);
  const dialog = await prepareCreate(); fireEvent.click(within(dialog).getByRole('button', { name: '确认创建' }));
  await screen.findByLabelText('本次观看码');
  act(() => window.dispatchEvent(new Event('episode:auth-expired')));
  expect(screen.queryByLabelText('本次观看码')).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: '已创建的邀请' })).not.toBeInTheDocument();
});

it('does not restore an expired owner’s secret when a create response arrives late', async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api).mockImplementation((path, options) => options?.method === 'POST'
    ? new Promise(resolve => { finish = resolve; }) as ReturnType<typeof api>
    : service(path, options) as ReturnType<typeof api>);
  render(<ShareManager episodeId={eid} />);
  const dialog = await prepareCreate(); fireEvent.click(within(dialog).getByRole('button', { name: '确认创建' }));
  act(() => window.dispatchEvent(new Event('episode:auth-expired')));
  await act(async () => { finish({ ...share(), share_code: 'late-fixture-code', watch_url: `/watch/${sid}#code=late-fixture-code` }); });
  expect(screen.queryByLabelText('本次观看码')).not.toBeInTheDocument();
  expect(screen.queryByRole('region', { name: '待核对的分享请求' })).not.toBeInTheDocument();
});

it('cancels returning to production without losing unsaved fields or the first-display code', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<ShareManager episodeId={eid} />);
  await screen.findByText('朋友的一期');
  fireEvent.change(screen.getByLabelText('给谁看'), { target: { value: '小林' } });
  fireEvent.click(screen.getByRole('checkbox'));
  expect(fireEvent.click(screen.getByRole('link', { name: '返回本期制作' }))).toBe(false);
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText('给谁看')).toHaveValue('小林');
  expect(screen.getByRole('checkbox')).toBeChecked();
  expect(writes()).toHaveLength(0);

  fireEvent.click(screen.getByRole('button', { name: '确认版本并创建邀请' }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认创建' }));
  await screen.findByLabelText('本次观看码');
  await waitFor(() => expect(screen.getByLabelText('给谁看')).toBeEnabled());
  expect(screen.getByLabelText('给谁看')).toHaveValue('');
  expect(fireEvent.click(screen.getByRole('link', { name: '返回本期制作' }))).toBe(false);
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText('本次观看码')).toHaveValue('fixture-watch-code');
  expect(writes()).toHaveLength(1);
});

it('allows confirmed departure without resending or losing the unresolved request journal', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') throw new AppError('NETWORK_ERROR', '尚待核对', 0, true);
    return service(path, options);
  });
  const first = render(<ShareManager episodeId={eid} />);
  const dialog = await prepareCreate();
  fireEvent.click(within(dialog).getByRole('button', { name: '确认创建' }));
  await screen.findByText('尚待核对');
  const body = writes()[0][1]?.body;
  let preventedByPage: boolean | undefined;
  document.addEventListener('click', event => {
    preventedByPage = event.defaultPrevented;
    event.preventDefault(); // Avoid jsdom navigation after observing the page's decision.
  }, { once: true });
  fireEvent.click(screen.getByRole('link', { name: '返回本期制作' }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(preventedByPage).toBe(false);
  expect(writes()).toHaveLength(1);
  first.unmount();
  render(<ShareManager episodeId={eid} />);
  await screen.findByRole('region', { name: '待核对的分享请求' });
  expect(writes()).toHaveLength(1);
  expect(writes()[0][1]?.body).toEqual(body);
  expect(screen.getByRole('button', { name: '用原请求继续核对' })).toBeEnabled();
});
