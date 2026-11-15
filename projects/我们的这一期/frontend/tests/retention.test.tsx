import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { EpisodeRetention } from '@/components/episode-retention';
import { routeContract } from '@/lib/routes';
import type { Retention } from '@/lib/types';

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('@/lib/api', () => ({ api: mocks.api }));
const eid = 'a'.repeat(32);
const active: Retention = { expires_at: Date.now() / 1000 + 86400,
  deletion_requested: false, cleanup_pending: false,
  counts: { assets: 2, versions: 3, submissions: 1, shares: 2 } };
beforeEach(() => mocks.api.mockReset());

it('requires an impact review and explicit confirmation before deletion', async () => {
  const retired = vi.fn();
  mocks.api.mockResolvedValue(active);
  render(<EpisodeRetention episodeId={eid} initial={active} onRetired={retired} />);
  fireEvent.click(screen.getByRole('button', { name: '删除这一期' }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText(/2 份素材、3 个版本及 1 份投稿/)).toBeInTheDocument();
  expect(mocks.api).not.toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ method: 'DELETE' }));
  fireEvent.click(within(dialog).getByRole('button', { name: '确认删除整期' }));
  await waitFor(() => expect(retired).toHaveBeenCalledOnce());
  expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}?confirm=true`, { method: 'DELETE' });
});

it('does not claim completion while cleanup is pending or status is unavailable', async () => {
  mocks.api.mockResolvedValue({ ...active, deletion_requested: true, cleanup_pending: true });
  render(<EpisodeRetention episodeId={eid} retired onRetired={vi.fn()} />);
  expect(await screen.findByText(/正在等待任务结束或重试清理/)).toBeInTheDocument();
  expect(screen.queryByText('应用内节目内容已清理。')).not.toBeInTheDocument();
});

it('shows completion only after the server confirms cleanup', async () => {
  mocks.api.mockResolvedValue({ ...active, deletion_requested: true, cleanup_pending: false });
  render(<EpisodeRetention episodeId={eid} retired onRetired={vi.fn()} />);
  expect(await screen.findByText('应用内节目内容已清理。')).toBeInTheDocument();
});

it('keeps the user on the confirmation when deletion fails', async () => {
  const retired = vi.fn();
  mocks.api.mockResolvedValueOnce(active).mockRejectedValueOnce(new Error('未确认是否清理，请刷新核对'));
  render(<EpisodeRetention episodeId={eid} initial={active} onRetired={retired} />);
  fireEvent.click(screen.getByRole('button', { name: '删除这一期' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('button', { name: '确认删除整期' }));
  expect(await screen.findByText('未确认是否清理，请刷新核对')).toBeInTheDocument();
  expect(retired).not.toHaveBeenCalled();
});

it('allows only the scoped retention and confirmed deletion contracts', () => {
  expect(routeContract(`/episodes/${eid}/retention`, 'GET')?.kind).toBe('retention');
  expect(routeContract(`/episodes/${eid}?confirm=true`, 'DELETE')?.kind).toBe('ok');
  expect(routeContract(`/episodes/${eid}?all=true`, 'DELETE')).toBeNull();
  expect(routeContract('/episodes?confirm=true', 'DELETE')).toBeNull();
});
