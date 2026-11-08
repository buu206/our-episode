import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ParticipantPortal from '@/components/participant-portal';
import SubmissionCard from '@/components/participant/submission-card';
import { MediaAccessContext } from '@/components/ui';
import { api, setCsrf } from '@/lib/api';
import type { Invitation, Submission } from '@/lib/collaboration-types';
import type { Command } from '@/components/participant/contracts';

vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), api: vi.fn(), setCsrf: vi.fn() }));
const id = (letter: string) => letter.repeat(32), timestamp = '2026-11-08T00:00:00Z';
const invitation: Invitation = { id: id('b'), role: 'interview', label: '朋友', expires_at: 1900000000, closed: false, accepting_new: true,
  context: { asset_id: id('d'), start: 0, end: 4 }, questions: [], context_job_id: id('d'), context_state: 'queued', context_error: null,
  context_preview_url: null, created_at: timestamp };
const draft = (overrides: Partial<Submission> = {}): Submission => ({
  id: id('c'), invite_id: id('b'), participant_id: id('a'), display_name: '朋友', kind: 'video', state: 'draft', text: '当前表达',
  question_id: null, filename: '本人片段.mp4', duration: 4, size: 100, preview_state: 'ready', preview_job_id: null, preview_error: null,
  media_url: `/api/v1/participant/submissions/${id('c')}/media`, created_at: timestamp, revisions: [], adoptions: [],
  can_delete_draft: true, deleted: false, deletion_pending: false, ...overrides,
});
const proposed = (): Submission => draft({ state: 'submitted', can_delete_draft: false,
  revisions: [{ id: id('e'), submission_id: id('c'), text: '修订表达', start: 1, end: 3, state: 'proposed', created_at: timestamp }] });
const command: Command = async action => ({ ok: true, value: await action(new AbortController().signal) });
const ask = vi.fn(async () => true), report = vi.fn();
function card(submission: Submission, revision = 0) {
  return <MediaAccessContext.Provider value={{ allowed: true, revision }}><SubmissionCard submission={submission} invitation={invitation}
    busy={false} command={command} ask={ask} report={report} /></MediaAccessContext.Provider>;
}
beforeEach(() => {
  vi.mocked(api).mockReset(); vi.mocked(setCsrf).mockReset(); ask.mockClear(); report.mockClear();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  window.history.replaceState({}, '', '/participate');
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('participant preview consent invalidation', () => {
  it('requires renewed playback and personal approval after an authorization revision changes', () => {
    const row = draft(), view = render(card(row));
    fireEvent.ended(screen.getByLabelText('本人稿件预览：本人片段.mp4'));
    fireEvent.click(screen.getByRole('checkbox', { name: /我已完整核对本人内容/ }));
    expect(screen.getByRole('button', { name: '确认并提交给发起人' })).toBeEnabled();
    view.rerender(card(row, 1));
    expect(screen.getByRole('checkbox', { name: /我已完整核对本人内容/ })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /我已完整核对本人内容/ })).not.toBeChecked();
    expect(screen.getByRole('button', { name: '确认并提交给发起人' })).toBeDisabled();
    expect(ask).not.toHaveBeenCalled();
  });

  it('invalidates an already approved revision when its preview is no longer available', () => {
    const row = proposed(), view = render(card(row));
    fireEvent.ended(screen.getByLabelText('待确认修订的原声区间'));
    fireEvent.click(screen.getByRole('checkbox', { name: /我已核对这份文字和区间/ }));
    expect(screen.getByRole('button', { name: '这份修订符合我的意思' })).toBeEnabled();
    view.rerender(card({ ...row, media_url: null, preview_state: 'failed' }));
    expect(screen.getByRole('button', { name: '这份修订符合我的意思' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /我已核对这份文字和区间/ })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: /我已核对这份文字和区间/ })).toBeDisabled();
  });

  it('does not carry approval to a changed revision interval or wording', () => {
    const row = proposed(), view = render(card(row));
    fireEvent.ended(screen.getByLabelText('待确认修订的原声区间'));
    fireEvent.click(screen.getByRole('checkbox', { name: /我已核对这份文字和区间/ }));
    view.rerender(card({ ...row, revisions: [{ ...row.revisions[0]!, text: '另一份表达', start: 2 }] }));
    expect(screen.getByRole('button', { name: '这份修订符合我的意思' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /我已核对这份文字和区间/ })).not.toBeChecked();
    expect(vi.mocked(api)).not.toHaveBeenCalled();
  });
});

describe('participant open confirmation revalidation', () => {
  it.each(['wording', 'withdrawal'] as const)('cancels a pending submit dialog after a polling response changes %s', async (change) => {
    vi.useFakeTimers();
    let rows = [draft({ kind: 'text', filename: null, duration: 0, size: 0, media_url: null })];
    vi.mocked(api).mockImplementation(async (path, options = {}) => {
      if (options.method) return { ok: true };
      if (path === '/participant/auth/me') return { participant_id: id('a'), invite_id: id('b'), display_name: '朋友', csrf_token: 'fixture-participant' };
      if (path === '/participant/invitation') return invitation;
      if (path === '/participant/submissions') return { items: structuredClone(rows) };
      if (path === '/participant/objections') return { items: [] };
      throw new Error('Unexpected test endpoint');
    });
    render(<ParticipantPortal />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.click(screen.getByRole('checkbox', { name: /我已完整核对本人内容/ }));
    fireEvent.click(screen.getByRole('button', { name: '确认并提交给发起人' }));
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '本人确认，提交' })).toBeEnabled();
    rows = change === 'wording' ? [{ ...rows[0]!, text: '另一标签页已修改的表达' }]
      : [{ ...rows[0]!, state: 'withdrawn', text: '' }];
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
    if (change === 'wording') {
      expect(screen.getByRole('button', { name: '确认并提交给发起人' })).toBeDisabled();
      expect(screen.getByRole('checkbox', { name: /我已完整核对本人内容/ })).not.toBeChecked();
    }
  });

  it('does not authorize the new identity with the old identity’s completed read bundle', async () => {
    let completeOld!: (value: { items: Submission[] }) => void;
    let fresh = false;
    vi.mocked(api).mockImplementation(async (path) => {
      const person = fresh ? id('f') : id('a');
      if (path === '/participant/auth/me') return { participant_id: person, invite_id: id('b'), display_name: fresh ? '新朋友' : '旧朋友', csrf_token: `fixture-${person}` };
      if (path === '/participant/invitation') return invitation;
      if (path === '/participant/submissions') {
        if (!fresh) return new Promise(resolve => { completeOld = resolve; });
        return { items: [] };
      }
      if (path === '/participant/objections') return { items: [] };
      throw new Error('Unexpected test endpoint');
    });
    render(<ParticipantPortal />);
    await waitFor(() => expect(completeOld).toBeDefined());
    fresh = true;
    await act(async () => document.dispatchEvent(new Event('visibilitychange')));
    await screen.findByRole('heading', { name: '新朋友 · 事后采访' });
    await act(async () => completeOld({ items: [draft({ text: '旧身份的私密内容' })] }));
    expect(screen.queryByText('旧身份的私密内容', { exact: false })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '新朋友 · 事后采访' })).toBeVisible();
  });
});
