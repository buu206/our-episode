import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import CollaborationManager from '@/components/collaboration-manager';
import type { Objection, Submission } from '@/lib/collaboration-types';

const mock = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('@/lib/api', async () => ({ ...await vi.importActual<typeof import('@/lib/api')>('@/lib/api'), api: mock.api }));
const eid = 'e'.repeat(32), sid = 'a'.repeat(32), rid = 'b'.repeat(32), pid = 'c'.repeat(32), date = '2026-11-08T00:00:00Z';
const original: Submission = { id: sid, invite_id: sid, participant_id: pid, display_name: '朋友', kind: 'video', state: 'submitted',
  text: '原表达', question_id: null, filename: 'test.mp4', duration: 2, size: 100, preview_state: 'ready', preview_job_id: null,
  preview_error: null, media_url: `/api/v1/collaboration/submissions/${sid}/media`, created_at: date,
  revisions: [{ id: rid, submission_id: sid, text: '待采用表达', start: 0, end: 2, state: 'confirmed', created_at: date }],
  adoptions: [], can_delete_draft: false, deleted: false, deletion_pending: false };
let submissions: Submission[], objections: Objection[];
beforeEach(() => {
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  submissions = [structuredClone(original)]; objections = [];
  mock.api.mockReset(); mock.api.mockImplementation(async (path: string, options?: { method?: string }) => {
    if (options?.method === 'POST') return {};
    if (path === `/episodes/${eid}`) return { id: eid, title: '测试一期', assets: [] };
    if (path.endsWith('/invitations')) return { items: [] };
    if (path.endsWith('/submissions')) return { items: structuredClone(submissions) };
    if (path.endsWith('/objections')) return { items: structuredClone(objections) };
    throw new Error('Unexpected endpoint');
  });
});

describe('owner consent regressions', () => {
  it('invalidates an already open adoption confirmation when the person withdraws', async () => {
    render(<CollaborationManager episodeId={eid} />);
    fireEvent.click(await screen.findByRole('button', { name: '采用这段已确认表达' }));
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '确认采用这一修订' })).toBeEnabled();
    submissions = [{ ...original, state: 'withdrawn', text: '', media_url: null, revisions: [] }];
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await screen.findByText('本人已撤回，内容和媒体不再展示；关联采用会停用。');
    await waitFor(() => {
      const button = screen.queryByRole('button', { name: '确认采用这一修订' });
      expect(button === null || (button as HTMLButtonElement).disabled).toBe(true);
    });
    expect(mock.api.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
    expect(document.querySelectorAll('video[src]')).toHaveLength(0);
  });

  it('never reports an unknown objection state as resolved', async () => {
    objections = [{ id: sid, invite_id: sid, participant_id: pid, display_name: '朋友', submission_id: sid, asset_id: null,
      start: null, end: null, reason: '需要核对', state: 'future_review', created_at: date, resolved_at: null }];
    render(<CollaborationManager episodeId={eid} />);
    await screen.findByText('需要核对');
    expect(screen.queryByText('本人已确认解决')).not.toBeInTheDocument();
    expect(screen.getByText('状态待核对')).toBeInTheDocument();
  });
});
