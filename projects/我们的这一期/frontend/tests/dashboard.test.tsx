import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Dashboard } from '@/components/dashboard';
import { MediaAccessContext } from '@/components/ui';
import { api, AppError, uploadAsset } from '@/lib/api';
import type { Asset, Episode, Version } from '@/lib/types';

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => router }));
vi.mock('@/lib/api', async importActual => {
  const real = await importActual<typeof import('@/lib/api')>();
  return { ...real, api: vi.fn(), uploadAsset: vi.fn() };
});
const episode = (id = 'a'.repeat(32), title = '这一次聚会'): Episode => ({
  id, title, background: '', created_at: '2026-11-08T10:00:00Z', assets: [], versions: [], jobs: [], directions: [],
  budget: { spent: 0, reserved: 0, limit: 3, total_spent: 0, total_reserved: 0, total_limit: 30 },
  evidence: [], evidence_revision: 0, evidence_confirmed_revision: null, analysis_state: 'not_started', analysis_notes: [], analysis_coverage: [], directions_revision: null, selected_direction: null,
});
const asset = (filename = '第一段.mp4'): Asset => ({ id: 'b'.repeat(32), filename, duration: 1, width: 1280, height: 720, has_audio: true, size: 8, media_url: `/api/v1/assets/${'b'.repeat(32)}/media`, source_kind: 'original', contributor: null, interview_text: null, fixed_segment: null });
const file = (name = '第一段.mp4') => new File(['video-fixture'], name, { type: name.endsWith('.mov') ? 'video/quicktime' : 'video/mp4', lastModified: 100 });
const select = (files: File[]) => {
  const summary = screen.getByText('添加素材与更多灵感');
  if (!summary.closest('details')!.open) fireEvent.click(summary);
  fireEvent.change(screen.getByLabelText('选择本期视频'), { target: { files } });
};
const posts = () => vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST');
function deferred<T>() { let resolve!: (result: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

beforeEach(() => {
  vi.mocked(api).mockReset(); vi.mocked(uploadAsset).mockReset(); router.push.mockReset();
  vi.mocked(api).mockImplementation(async (_path, options) => options?.method === 'POST' ? episode() : { items: [] });
  vi.mocked(uploadAsset).mockResolvedValue(asset());
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('creates an idea-only episode with a bounded title and full prompt on Enter, without model or upload requests', async () => {
  render(<Dashboard />);
  await screen.findByText('第一期，从你的素材开始');
  const prompt = '周末做饭，'.repeat(30) + '保留大家聊天的原声。';
  const field = screen.getByRole('textbox', { name: '这一期的想法' });
  expect(field.tagName).toBe('INPUT');
  expect(screen.getByRole('button', { name: '开拍' })).toBeEnabled();
  fireEvent.change(field, { target: { value: `  ${prompt}  ` } });
  field.focus();
  await userEvent.keyboard('{Enter}');
  expect(posts()).toHaveLength(1);
  expect(posts()[0]).toEqual(['/episodes', expect.objectContaining({ body: { title: prompt.slice(0, 120), background: prompt }, signal: expect.any(AbortSignal) })]);
  expect(router.push).toHaveBeenCalledWith(`/episodes/${episode().id}?step=materials`);
  expect(uploadAsset).not.toHaveBeenCalled();
  expect(vi.mocked(api).mock.calls.every(([path]) => path === '/episodes')).toBe(true);
});

it('lets editable examples and file selection stay local until explicit submission', async () => {
  render(<Dashboard />);
  const summary = screen.getByText('添加素材与更多灵感');
  expect(summary.closest('details')).not.toHaveAttribute('open');
  await userEvent.click(summary);
  expect(summary.closest('details')).toHaveAttribute('open');
  await userEvent.click(screen.getByRole('button', { name: '朋友聚会' }));
  expect((screen.getByRole('textbox', { name: '这一期的想法' }) as HTMLInputElement).value).toContain('朋友聚会');
  fireEvent.change(screen.getByRole('textbox', { name: '这一期的想法' }), { target: { value: '我自己改的想法' } });
  select([file(), file('第二段.mov')]);
  expect(screen.getByLabelText('所选视频').children).toHaveLength(2);
  expect(posts()).toHaveLength(0); expect(uploadAsset).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '移除第一段.mp4' }));
  expect(screen.queryByText('第一段.mp4')).not.toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '这一期的想法' })).toHaveValue('我自己改的想法');
});

it('rejects client-side count, size, empty and extension violations without losing a valid selection', async () => {
  render(<Dashboard />); select([file()]);
  select(Array.from({ length: 8 }, (_, index) => file(`extra-${index}.mp4`)));
  expect(screen.getByRole('alert')).toHaveTextContent('最多选择 8 段');
  const huge = file('huge.mp4'); Object.defineProperty(huge, 'size', { value: 1024 ** 3 });
  select([huge]); expect(screen.getByLabelText('所选视频').children).toHaveLength(1);
  select([file('图片.jpg')]); expect(screen.getByRole('alert')).toHaveTextContent('MP4 或 MOV');
  select([new File([], 'empty.mp4')]);
  expect(screen.getByLabelText('所选视频').children).toHaveLength(1);
  expect(posts()).toHaveLength(0); expect(uploadAsset).not.toHaveBeenCalled();
});

it('reports actual sequential upload progress and waits for server verification after all bytes are sent', async () => {
  const first = deferred<Asset>();
  vi.mocked(uploadAsset).mockImplementationOnce((_id, _file, progress) => { progress?.(4, 8); return first.promise; }).mockResolvedValueOnce(asset('第二段.mov'));
  render(<Dashboard />); select([file(), file('第二段.mov')]);
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  expect(posts()[0][1]?.body).toEqual({ title: '新的一期', background: '' });
  expect(uploadAsset).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('progressbar', { name: '第一段.mp4传输进度' })).toHaveAttribute('value', '4');
  act(() => vi.mocked(uploadAsset).mock.calls[0][2]?.(8, 8));
  expect(screen.getByText(/传输完成，正在核对视频/)).toBeInTheDocument();
  expect(router.push).not.toHaveBeenCalled();
  await act(async () => first.resolve(asset()));
  expect(uploadAsset).toHaveBeenCalledTimes(2);
  expect(router.push).toHaveBeenCalledWith(`/episodes/${episode().id}?step=materials`);
});

it('keeps successful uploads and reuses the known episode when retrying only a rejected video', async () => {
  vi.mocked(uploadAsset).mockResolvedValueOnce(asset()).mockRejectedValueOnce(new AppError('INVALID_MEDIA', '第二段无法解码', 422)).mockResolvedValueOnce(asset('第二段.mov'));
  render(<Dashboard />); select([file(), file('第二段.mov')]);
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  expect(await screen.findByText('第二段无法解码')).toBeVisible();
  expect(screen.getByRole('link', { name: '进入已创建的这一期' })).toHaveAttribute('href', `/episodes/${episode().id}?step=materials`);
  expect(screen.getByText(/已接收$/)).toBeInTheDocument();
  expect(router.push).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '仅接收未完成的视频' }));
  expect(posts()).toHaveLength(1);
  expect(vi.mocked(uploadAsset).mock.calls.map(([id, video]) => [id, video.name])).toEqual([[episode().id, '第一段.mp4'], [episode().id, '第二段.mov'], [episode().id, '第二段.mov']]);
  expect(router.push).toHaveBeenCalledWith(`/episodes/${episode().id}?step=materials`);
});

it('never recreates or automatically claims a list entry after an uncertain create response', async () => {
  const candidate = episode('c'.repeat(32), '可能已经建立的一期');
  vi.mocked(api).mockImplementation(async (_path, options) => {
    if (options?.method === 'POST') throw new AppError('NETWORK_ERROR', '创建响应丢失', 0, true);
    return { items: [candidate] };
  });
  render(<Dashboard />); select([file()]);
  fireEvent.change(screen.getByRole('textbox', { name: '这一期的想法' }), { target: { value: '保留这个想法' } });
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('创建响应丢失');
  expect(screen.getByRole('textbox', { name: '这一期的想法' })).toHaveValue('保留这个想法');
  expect(screen.getByText('第一段.mp4')).toBeVisible();
  expect(screen.getByRole('button', { name: '开拍' })).toBeDisabled();
  fireEvent.submit(screen.getByRole('textbox', { name: '这一期的想法' }).closest('form')!);
  await userEvent.click(screen.getByRole('button', { name: '刷新节目列表' }));
  expect(posts()).toHaveLength(1); expect(uploadAsset).not.toHaveBeenCalled(); expect(router.push).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: `继续制作：${candidate.title}` })).toHaveAttribute('href', `/episodes/${candidate.id}?step=materials`);
});

it('does not retry an uncertain upload, including when another explicitly rejected file is retried', async () => {
  vi.mocked(uploadAsset).mockRejectedValueOnce(new AppError('NETWORK_ERROR', '第一段结果未知', 0, true)).mockRejectedValueOnce(new AppError('INVALID_MEDIA', '第二段未接收', 422)).mockResolvedValueOnce(asset('第二段.mov'));
  render(<Dashboard />); select([file(), file('第二段.mov')]);
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  expect(await screen.findByText('第一段结果未知')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '仅接收未完成的视频' }));
  expect(vi.mocked(uploadAsset).mock.calls.map(([, video]) => video.name)).toEqual(['第一段.mp4', '第二段.mov', '第二段.mov']);
  expect(posts()).toHaveLength(1); expect(router.push).not.toHaveBeenCalled();
  expect(screen.getByText(/这段视频不会自动重传/)).toBeVisible();
  expect(screen.queryByRole('button', { name: '仅接收未完成的视频' })).not.toBeInTheDocument();
});

it('requires explicit confirmation to unlock an uncertain creation and still waits for a new submit', async () => {
  vi.mocked(api).mockImplementation(async (_path, options) => {
    if (options?.method === 'POST') throw new AppError('NETWORK_ERROR', '创建响应丢失', 0, true);
    return { items: [] };
  });
  render(<Dashboard />); select([file()]);
  fireEvent.change(screen.getByRole('textbox', { name: '这一期的想法' }), { target: { value: '核对后继续' } });
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  vi.mocked(window.confirm).mockReturnValueOnce(false);
  await userEvent.click(screen.getByRole('button', { name: '已核对，允许重新提交' }));
  expect(screen.getByRole('button', { name: '开拍' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '已核对，允许重新提交' }));
  expect(screen.getByRole('button', { name: '开拍' })).toBeEnabled();
  expect(screen.getByRole('textbox', { name: '这一期的想法' })).toHaveValue('核对后继续');
  expect(screen.getByText('第一段.mp4')).toBeVisible();
  expect(posts()).toHaveLength(1); expect(uploadAsset).not.toHaveBeenCalled();
  vi.mocked(api).mockImplementation(async (_path, options) => options?.method === 'POST' ? episode() : { items: [] });
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  expect(posts()).toHaveLength(2); expect(uploadAsset).toHaveBeenCalledTimes(1);
});

it('aborts a creation request on unmount and ignores its late response instead of starting uploads', async () => {
  const pending = deferred<Episode>();
  vi.mocked(api).mockImplementation((_path, options) => options?.method === 'POST' ? pending.promise : Promise.resolve({ items: [] }));
  const view = render(<Dashboard />); select([file()]);
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  const signal = posts()[0][1]?.signal;
  view.unmount(); expect(signal?.aborted).toBe(true);
  await act(async () => pending.resolve(episode()));
  expect(uploadAsset).not.toHaveBeenCalled(); expect(router.push).not.toHaveBeenCalled();
});

it('aborts the current upload and ignores a late success on logout or expired authentication', async () => {
  const pending = deferred<Asset>(); vi.mocked(uploadAsset).mockReturnValue(pending.promise);
  render(<Dashboard />); select([file(), file('第二段.mov')]);
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  const signal = vi.mocked(uploadAsset).mock.calls[0][3];
  act(() => window.dispatchEvent(new Event('episode:auth-expired')));
  expect(signal?.aborted).toBe(true);
  await act(async () => pending.resolve(asset()));
  expect(uploadAsset).toHaveBeenCalledTimes(1); expect(router.push).not.toHaveBeenCalled();
});

it('stops the remaining upload queue after a 401 rather than sending the next video', async () => {
  vi.mocked(uploadAsset).mockRejectedValue(new AppError('AUTH_REQUIRED', '请重新登录', 401));
  render(<Dashboard />); select([file(), file('第二段.mov')]);
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  expect(await screen.findByText('请重新登录')).toBeVisible();
  expect(uploadAsset).toHaveBeenCalledTimes(1); expect(posts()).toHaveLength(1); expect(router.push).not.toHaveBeenCalled();
});

it('pauses unsent uploads in the background and continues on explicit action after access is restored', async () => {
  const pending = deferred<Asset>(); vi.mocked(uploadAsset).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(asset('第二段.mov'));
  const view = render(<MediaAccessContext.Provider value={{ allowed: true, revision: 0 }}><Dashboard /></MediaAccessContext.Provider>);
  select([file(), file('第二段.mov')]);
  await userEvent.click(screen.getByRole('button', { name: '开拍' }));
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  view.rerender(<MediaAccessContext.Provider value={{ allowed: false, revision: 0 }}><Dashboard /></MediaAccessContext.Provider>);
  await act(async () => pending.resolve(asset()));
  expect(uploadAsset).toHaveBeenCalledTimes(1); expect(router.push).not.toHaveBeenCalled();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(screen.getByRole('button', { name: '仅接收未完成的视频' })).toBeDisabled();
  view.rerender(<MediaAccessContext.Provider value={{ allowed: true, revision: 1 }}><Dashboard /></MediaAccessContext.Provider>);
  await userEvent.click(screen.getByRole('button', { name: '仅接收未完成的视频' }));
  expect(uploadAsset).toHaveBeenCalledTimes(2); expect(posts()).toHaveLength(1);
  expect(router.push).toHaveBeenCalledWith(`/episodes/${episode().id}?step=materials`);
});

it('keeps the latest three episodes visible and restores older episode links when expanded', async () => {
  const items = [1, 2, 3, 4].map(index => episode(String(index).repeat(32), `节目${index}`));
  vi.mocked(api).mockResolvedValue({ items });
  render(<Dashboard />);
  const recent = screen.getByRole('region', { name: '最近制作' });
  await screen.findByRole('link', { name: '继续制作：节目4' });
  expect(within(recent).getAllByRole('link')).toHaveLength(3);
  expect(screen.queryByRole('link', { name: '继续制作：节目1' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '展开全部 4 期' }));
  expect(screen.getByRole('link', { name: '继续制作：节目1' })).toHaveAttribute('href', `/episodes/${items[0].id}?step=materials`);
  expect(within(recent).getAllByRole('link')).toHaveLength(4);
  await userEvent.click(screen.getByRole('button', { name: '收起，查看最近 3 期' }));
  await waitFor(() => expect(within(recent).getAllByRole('link')).toHaveLength(3));
});

it('brings a newly edited older episode forward and labels the actual preview version, including after refresh', async () => {
  const older = { ...episode('a'.repeat(32), '旧节目新成片'), created_at: '2026-11-01T00:00:00Z' };
  const version = (id: string, state: string, created_at: string): Version => ({
    id: id.repeat(32), title: '成片', state, created_at, origin: 'manual', segments: [],
    media_url: `/api/v1/versions/${id.repeat(32)}/media`, job_id: null, error: null,
    base_version_id: null, narration_text: '', narration_approved: false, narration_ready: false,
    packaging: 'variety_v2', evidence_revision: null,
  });
  const latest = version('b', 'ready', '2026-11-22T00:00:00Z');
  // Deliberately unsorted: API order must not make an older confirmed film look current.
  older.versions = [latest, version('c', 'confirmed', '2026-11-02T00:00:00Z')];
  const items = [older, ...[2, 3, 4].map(n => episode(String(n).repeat(32), `节目${n}`))];
  vi.mocked(api).mockResolvedValue({ items });
  render(<Dashboard />);
  const link = await screen.findByRole('link', { name: '继续制作：旧节目新成片' });
  const region = screen.getByRole('region', { name: '最近制作' });
  expect(within(region).getAllByRole('link')[0]).toBe(link);
  expect(link).toHaveAttribute('href', `/episodes/${older.id}?step=preview&version=${latest.id}`);
  expect(link).toHaveTextContent('新版待预览');
  expect(link).not.toHaveTextContent('这版已确认');
  latest.state = 'confirmed';
  await userEvent.click(screen.getByRole('button', { name: '刷新节目列表' }));
  await waitFor(() => expect(link).toHaveTextContent('这版已确认'));
  expect(within(region).getAllByRole('link')[0]).toBe(link);
  expect(items[0]).toBe(older);
  expect(older.versions[0]).toBe(latest);
  expect(posts()).toHaveLength(0);
});


it('opens a new materials flow on an empty shoot click without spending or sending media', async () => {
  render(<Dashboard />);
  await screen.findByText('第一期，从你的素材开始');
  await userEvent.click(screen.getByRole('button', {name:'开拍'}));
  expect(posts()).toHaveLength(1);
  expect(posts()[0]).toEqual(['/episodes', expect.objectContaining({body:{title:'新的一期',background:''}})]);
  expect(router.push).toHaveBeenCalledWith(`/episodes/${episode().id}?step=materials`);
  expect(uploadAsset).not.toHaveBeenCalled();
  expect(vi.mocked(api).mock.calls.every(([path])=>path==='/episodes')).toBe(true);
});
