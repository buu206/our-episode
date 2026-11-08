import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ParticipantPortal from '@/components/participant-portal';
import { api, AppError, setCsrf, uploadSubmission } from '@/lib/api';
import type { Invitation, Objection, ParticipantAuth, Submission } from '@/lib/collaboration-types';

vi.mock('@/lib/api', async importActual => {
  const actual = await importActual<typeof import('@/lib/api')>();
  return { ...actual, api: vi.fn(), setCsrf: vi.fn(), uploadSubmission: vi.fn() };
});
const id = (character: string) => character.repeat(32);
let auth: ParticipantAuth;
let invitation: Invitation;
let submissions: Submission[];
let objections: Objection[];
let write: (path: string, options: { method?: string; body?: unknown }) => Promise<unknown>;
let readError: Error | null;
const draft = (overrides: Partial<Submission> = {}): Submission => ({
  id: id('c'), invite_id: id('b'), participant_id: id('a'), display_name: '小林', kind: 'text', state: 'draft', text: '这是我的意思', question_id: null,
  filename: null, duration: 0, size: 0, preview_state: 'ready', preview_job_id: null, preview_error: null, media_url: null, created_at: '2026-11-08T00:00:00Z',
  revisions: [], adoptions: [], can_delete_draft: true, deleted: false, deletion_pending: false, ...overrides,
});
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes; }); return { promise, resolve }; }
const writes = (path?: string) => vi.mocked(api).mock.calls.filter(([url, options]) => options?.method && options.method !== 'GET' && (!path || url === path));
const authFixture = (): ParticipantAuth => ({ participant_id: id('a'), invite_id: id('b'), display_name: '小林', csrf_token: 'participant-fixture' });
async function ready() { const view = render(<ParticipantPortal />); await screen.findByRole('heading', { name: /小林 ·/ }); return view; }
async function acceptDialog(button: string) { const dialog = await screen.findByRole('dialog'); await userEvent.click(within(dialog).getByRole('button', { name: button })); }

beforeEach(() => {
  vi.mocked(api).mockReset(); vi.mocked(setCsrf).mockReset(); vi.mocked(uploadSubmission).mockReset();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  window.history.replaceState({}, '', '/participate');
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  auth = authFixture(); readError = null; submissions = []; objections = [];
  invitation = { id: id('b'), role: 'interview', label: '小林', expires_at: 1_900_000_000, closed: false, accepting_new: true,
    context: { asset_id: id('d'), start: 12, end: 20 }, questions: [{ id: 'question-one', text: '你当时怎么想？', skipped: false }],
    context_job_id: null, context_state: 'ready', context_error: null, context_preview_url: '/api/v1/participant/context/media', created_at: '2026-11-08T00:00:00Z' };
  write = async () => ({ ok: true });
  vi.mocked(api).mockImplementation(async (path, options = {}) => {
    if (options.method && options.method !== 'GET') return write(path, options);
    if (readError) throw readError;
    if (path === '/participant/auth/me') return auth;
    if (path === '/participant/invitation') return invitation;
    if (path === '/participant/submissions') return { items: submissions };
    if (path === '/participant/objections') return { items: objections };
    throw new Error(`Unexpected read ${path}`);
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

it('consumes an invitation fragment without storing it and requires a name plus personal confirmation to log in', async () => {
  window.history.replaceState({ page: 'same' }, '', '/participate#code=local-invite-fixture');
  readError = new AppError('PARTICIPANT_AUTH_REQUIRED', '需要本人登录', 401);
  write = async (path, options) => { expect(path).toBe('/participant/auth/login'); expect(options.body).toEqual({ invite_code: 'local-invite-fixture', display_name: '小林' }); readError = null; return auth; };
  render(<ParticipantPortal />);
  const code = await screen.findByLabelText('邀请代码');
  expect(window.location.hash).toBe(''); expect(window.history.state).toEqual({ page: 'same' });
  expect(code).toHaveValue('local-invite-fixture'); expect(code).toHaveAttribute('type', 'password');
  expect(screen.getByRole('button', { name: '进入我的任务' })).toBeDisabled();
  await userEvent.type(screen.getByLabelText('你的称呼'), '小林');
  expect(screen.getByRole('button', { name: '进入我的任务' })).toBeDisabled();
  await userEvent.click(screen.getByRole('checkbox', { name: /这是发给我的邀请/ }));
  await userEvent.click(screen.getByRole('button', { name: '进入我的任务' }));
  await screen.findByRole('heading', { name: '小林 · 事后采访' });
  expect(setCsrf).toHaveBeenCalledWith(auth.csrf_token, 'participant');
  expect(vi.mocked(setCsrf).mock.calls.every(([, role]) => role === 'participant')).toBe(true);
  expect(screen.queryByLabelText('邀请代码')).not.toBeInTheDocument();
  expect(writes()).toHaveLength(1);
});

it('does not silently replace an existing participant when another invitation link is opened', async () => {
  window.history.replaceState({}, '', '/participate#code=another-invite-fixture');
  render(<ParticipantPortal />);
  const choice = await screen.findByRole('button', { name: '继续当前任务' });
  expect(screen.queryByRole('heading', { name: '小林 · 事后采访' })).not.toBeInTheDocument();
  expect(writes()).toHaveLength(0);
  await userEvent.click(choice);
  expect(await screen.findByRole('heading', { name: '小林 · 事后采访' })).toBeVisible();
  expect(writes()).toHaveLength(0);
});

it('restricts a review invitation to its explicit context and read-only questions', async () => {
  invitation.role = 'review';
  await ready();
  expect(screen.getByLabelText('问题文字')).toBeDisabled();
  expect(screen.queryByRole('button', { name: '保存问题修改与跳过' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '保存为私密草稿' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('邀请中授权的上下文片段')).toHaveAttribute('src', '/api/v1/participant/context/media');
  expect(screen.getByText(/授权原片区间：12–20/)).toBeVisible();
  expect(vi.mocked(api).mock.calls.every(([path]) => path.startsWith('/participant/'))).toBe(true);
});

it('preserves unsaved question edits across refresh and saves the original IDs and explicit skip state', async () => {
  write = async (path, options) => { expect(path).toBe('/participant/invitation/questions'); invitation = { ...invitation, questions: (options.body as { questions: Invitation['questions'] }).questions }; return invitation; };
  await ready();
  fireEvent.change(screen.getByLabelText('问题文字'), { target: { value: '我更想分享后来发生的事' } });
  await userEvent.click(screen.getByRole('checkbox', { name: '我选择跳过这个问题' }));
  await userEvent.click(screen.getByRole('button', { name: '刷新任务状态' }));
  expect(await screen.findByLabelText('问题文字')).toHaveValue('我更想分享后来发生的事');
  await userEvent.click(screen.getByRole('button', { name: '保存问题修改与跳过' }));
  expect(writes()[0][1]?.body).toEqual({ questions: [{ id: 'question-one', text: '我更想分享后来发生的事', skipped: true }] });
  await waitFor(() => expect(screen.getByRole('button', { name: '保存问题修改与跳过' })).toBeDisabled());
});

it('saves text privately and requires a separate checkbox and dialog before submitting the exact draft', async () => {
  write = async (path, options) => {
    if (path === '/participant/submissions') { submissions = [draft({ text: (options.body as { text: string }).text })]; return submissions[0]; }
    if (path.endsWith('/submit')) { submissions = [{ ...submissions[0], state: 'submitted', can_delete_draft: false }]; return submissions[0]; }
    throw new Error('Unexpected write');
  };
  await ready(); await userEvent.type(screen.getByLabelText('我的文字表达'), '我当时只是有点紧张');
  await userEvent.click(screen.getByRole('button', { name: '保存为私密草稿' }));
  const row = await screen.findByRole('article', { name: '稿件：文字稿件' });
  expect(writes()).toHaveLength(1);
  expect(writes()[0][1]?.body).toEqual({ text: '我当时只是有点紧张', question_id: null, client_request_id: expect.any(String) });
  expect(within(row).getByRole('button', { name: '确认并提交给发起人' })).toBeDisabled();
  await userEvent.click(within(row).getByRole('checkbox', { name: /我已完整核对本人内容/ }));
  await userEvent.click(within(row).getByRole('button', { name: '确认并提交给发起人' }));
  expect(writes()).toHaveLength(1);
  await acceptDialog('本人确认，提交');
  expect(writes(`/participant/submissions/${id('c')}/submit`)[0][1]?.body).toEqual({ confirmed: true, expected_draft: { text: '我当时只是有点紧张', question_id: null } });
  expect(await screen.findByText(/文字稿件 · 已本人确认提交/)).toBeVisible();
});

it('uses real upload progress, then waits for the MP4 preview and personal confirmation for audio', async () => {
  const upload = deferred<Submission>();
  vi.mocked(uploadSubmission).mockImplementation((_fields, progress) => { progress?.(2, 4); return upload.promise; });
  await ready();
  await userEvent.selectOptions(screen.getByLabelText('草稿形式'), 'audio');
  const file = new File(['test'], '我的采访.wav', { type: 'audio/wav' });
  fireEvent.change(screen.getByLabelText('选择本人文件'), { target: { files: [file] } });
  await userEvent.click(screen.getByRole('button', { name: '保存为私密草稿' }));
  expect(screen.getByRole('progressbar', { name: '草稿文件传输进度' })).toHaveAttribute('value', '2');
  act(() => vi.mocked(uploadSubmission).mock.calls[0][1]?.(4, 4));
  expect(screen.getByText('传输完成，等待服务端校验和保存。')).toBeVisible();
  expect(screen.queryByRole('article')).not.toBeInTheDocument();
  submissions = [draft({ kind: 'audio', filename: file.name, duration: 1, size: 4, media_url: `/api/v1/participant/submissions/${id('c')}/media` })];
  await act(async () => upload.resolve(submissions[0]));
  const row = await screen.findByRole('article', { name: '稿件：我的采访.wav' });
  expect(within(row).getByRole('checkbox', { name: /我已完整核对本人内容/ })).toBeDisabled();
  fireEvent.ended(within(row).getByLabelText('本人稿件预览：我的采访.wav'));
  expect(within(row).getByRole('checkbox', { name: /我已完整核对本人内容/ })).toBeEnabled();
  expect(writes()).toHaveLength(0); expect(uploadSubmission).toHaveBeenCalledTimes(1);
});

it('submits the dialog draft snapshot and requires a fresh confirmation after DRAFT_CHANGED without losing other input', async () => {
  submissions = [draft({ text: '我刚才核对的原话', question_id: 'question-one' })];
  write = async (path, options) => {
    expect(path).toBe(`/participant/submissions/${id('c')}/submit`);
    expect(options.body).toEqual({ confirmed: true, expected_draft: { text: '我刚才核对的原话', question_id: 'question-one' } });
    throw new AppError('DRAFT_CHANGED', '草稿已在其他页面修改，请重新核对后确认。', 409);
  };
  await ready();
  await userEvent.type(screen.getByLabelText('我的文字表达'), '另一份未保存输入');
  const row = screen.getByRole('article', { name: '稿件：文字稿件' });
  await userEvent.click(within(row).getByRole('checkbox', { name: /我已完整核对本人内容/ }));
  await userEvent.click(within(row).getByRole('button', { name: '确认并提交给发起人' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('我刚才核对的原话');
  // Another tab changes the server draft after this dialog opened, without a client refresh.
  submissions = [draft({ text: '另一页面改成的新意思', question_id: null })];
  await acceptDialog('本人确认，提交');
  expect(await screen.findByRole('alert')).toHaveTextContent('草稿已在其他页面修改');
  expect(screen.getByLabelText('我的文字表达')).toHaveValue('另一份未保存输入');
  expect(screen.getByText('你的文字表达：另一页面改成的新意思')).toBeVisible();
  expect(within(row).getByRole('checkbox', { name: /我已完整核对本人内容/ })).not.toBeChecked();
  expect(within(row).getByRole('button', { name: '确认并提交给发起人' })).toBeDisabled();
  expect(writes()).toHaveLength(1);
});

it('keeps the same idempotency key after an unknown upload and never retries while refreshing status', async () => {
  vi.mocked(uploadSubmission).mockRejectedValueOnce(new AppError('NETWORK_ERROR', '结果未知', 0, true)).mockImplementationOnce(async () => { submissions = [draft({ kind: 'video', filename: '我拍的.mov', duration: 1 })]; return submissions[0]; });
  await ready(); await userEvent.selectOptions(screen.getByLabelText('草稿形式'), 'video');
  fireEvent.change(screen.getByLabelText('选择本人文件'), { target: { files: [new File(['clip'], '我拍的.mov', { type: 'video/quicktime' })] } });
  await userEvent.click(screen.getByRole('button', { name: '保存为私密草稿' }));
  await screen.findByRole('button', { name: '重试同一个请求' });
  expect(uploadSubmission).toHaveBeenCalledTimes(1);
  const key = vi.mocked(uploadSubmission).mock.calls[0][0].client_request_id;
  await userEvent.click(screen.getByRole('button', { name: '刷新核对我的稿件' }));
  expect(uploadSubmission).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole('button', { name: '重试同一个请求' }));
  expect(uploadSubmission).toHaveBeenCalledTimes(2);
  expect(vi.mocked(uploadSubmission).mock.calls[1][0].client_request_id).toBe(key);
});

it('validates absolute context intervals and locks unknown objection writes instead of repeating them', async () => {
  write = async path => { expect(path).toBe('/participant/objections'); throw new AppError('NETWORK_ERROR', '异议结果未知', 0, true); };
  await ready();
  await userEvent.type(screen.getByLabelText('哪里不符合你的意思'), '这里没有说完我的意思');
  fireEvent.change(screen.getByLabelText('异议结束（原片秒）'), { target: { value: '25' } });
  await userEvent.click(screen.getByRole('button', { name: '提交这条异议' }));
  expect(screen.getByRole('alert')).toHaveTextContent('区间必须落在'); expect(writes()).toHaveLength(0);
  fireEvent.change(screen.getByLabelText('异议开始（原片秒）'), { target: { value: '13' } });
  fireEvent.change(screen.getByLabelText('异议结束（原片秒）'), { target: { value: '15' } });
  await userEvent.click(screen.getByRole('button', { name: '提交这条异议' })); await acceptDialog('登记我的异议');
  expect(writes()[0][1]?.body).toEqual({ reason: '这里没有说完我的意思', start: 13, end: 15 });
  await waitFor(() => expect(screen.getByRole('button', { name: '提交这条异议' })).toBeDisabled());
  await userEvent.click(screen.getByRole('button', { name: '刷新异议记录' }));
  expect(writes()).toHaveLength(1); expect(screen.getByLabelText('哪里不符合你的意思')).toHaveValue('这里没有说完我的意思');
});

it('distinguishes private draft deletion, pending physical cleanup and retry from withdrawal', async () => {
  submissions = [draft()]; let first = true;
  write = async (path, options) => {
    expect(path).toBe(`/participant/submissions/${id('c')}?confirm=true`); expect(options.method).toBe('DELETE');
    submissions = [{ ...submissions[0], state: 'withdrawn', text: '', deleted: true, deletion_pending: first, can_delete_draft: first }];
    if (first) { first = false; throw new AppError('DELETION_PENDING', '文件清理待重试', 503, true); }
    return { ok: true };
  };
  await ready(); await userEvent.click(screen.getByRole('button', { name: '删除未提交草稿及应用副本' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('设备中的原文件不受影响');
  await acceptDialog('确认删除草稿');
  expect(await screen.findByText(/剩余文件仍占额度/)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '重试删除草稿文件' })); await acceptDialog('确认删除草稿');
  expect(await screen.findByText('应用副本已清理，设备中的外部原文件仍保留。')).toBeVisible();
  expect(writes()).toHaveLength(2); expect(writes().every(([path]) => !path.endsWith('/withdraw'))).toBe(true);
});

it('keeps existing revision decisions and withdrawal available after the invitation stops accepting new work', async () => {
  invitation = { ...invitation, accepting_new: false, closed: true };
  submissions = [draft({ state: 'submitted', can_delete_draft: false, revisions: [{ id: id('e'), submission_id: id('c'), text: '这是我确认的新说法', start: 0, end: 0, state: 'proposed', created_at: '2026-11-08T00:00:00Z' }] })];
  write = async (path, options) => {
    expect(path).toBe(`/participant/revisions/${id('e')}/decision`); expect(options.body).toEqual({ decision: 'confirmed' });
    const revision = { ...submissions[0].revisions[0], state: 'confirmed' }; submissions = [{ ...submissions[0], revisions: [revision] }]; return revision;
  };
  await ready();
  expect(screen.queryByRole('button', { name: '保存为私密草稿' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '撤回使用授权' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: '删除未提交草稿及应用副本' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('checkbox', { name: /我已核对这份文字和区间/ }));
  await userEvent.click(screen.getByRole('button', { name: '这份修订符合我的意思' })); await acceptDialog('确认这份修订');
  expect(writes()).toHaveLength(1);
  expect(await screen.findByText(/表达修订：本人已确认/)).toBeVisible();
});

it('freezes media but preserves an unfinished expression during an offline foreground check, then isolates a new identity', async () => {
  await ready(); await userEvent.type(screen.getByLabelText('我的文字表达'), '只属于小林的未保存文字');
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(screen.queryByLabelText('邀请中授权的上下文片段')).not.toBeInTheDocument();
  readError = new AppError('NETWORK_ERROR', '暂时离线');
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  expect(screen.getByLabelText('我的文字表达')).toHaveValue('只属于小林的未保存文字');
  expect(screen.getByLabelText('我的文字表达')).not.toBeVisible();
  readError = null;
  await userEvent.click(screen.getByRole('button', { name: '重新核对任务' }));
  await waitFor(() => expect(screen.getByLabelText('我的文字表达')).toBeVisible());
  expect(screen.getByLabelText('我的文字表达')).toHaveValue('只属于小林的未保存文字');
  auth = { ...auth, participant_id: id('f'), display_name: '另一位朋友' };
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  expect(await screen.findByRole('heading', { name: '另一位朋友 · 事后采访' })).toBeVisible();
  expect(screen.getByLabelText('我的文字表达')).toHaveValue('');
});

it('clears participant media immediately on expiration and ignores a late upload without affecting owner credentials', async () => {
  const upload = deferred<Submission>(); vi.mocked(uploadSubmission).mockReturnValue(upload.promise);
  await ready(); await userEvent.selectOptions(screen.getByLabelText('草稿形式'), 'video');
  fireEvent.change(screen.getByLabelText('选择本人文件'), { target: { files: [new File(['clip'], '尚未确定.mp4', { type: 'video/mp4' })] } });
  await userEvent.click(screen.getByRole('button', { name: '保存为私密草稿' }));
  const signal = vi.mocked(uploadSubmission).mock.calls[0][2];
  act(() => window.dispatchEvent(new Event('episode:participant-auth-expired')));
  expect(signal?.aborted).toBe(true); expect(screen.queryByLabelText('邀请中授权的上下文片段')).not.toBeInTheDocument();
  expect(screen.getByLabelText('邀请代码')).toBeVisible();
  await act(async () => upload.resolve(draft()));
  expect(screen.queryByRole('heading', { name: /小林 ·/ })).not.toBeInTheDocument();
  expect(vi.mocked(setCsrf).mock.calls.every(([, role]) => role === 'participant')).toBe(true);
});

it('keeps unfinished input when logout is cancelled or fails, and only clears it after a successful logout', async () => {
  let fail = true;
  write = async path => { expect(path).toBe('/participant/auth/logout'); if (fail) throw new AppError('NETWORK_ERROR', '退出失败', 0, true); return { ok: true }; };
  await ready(); await userEvent.type(screen.getByLabelText('我的文字表达'), '没有保存的采访文字');
  await userEvent.click(screen.getByRole('button', { name: '退出朋友身份' })); await acceptDialog('取消');
  expect(writes()).toHaveLength(0);
  await userEvent.click(screen.getByRole('button', { name: '退出朋友身份' })); await acceptDialog('退出并放弃未保存输入');
  expect(await screen.findByRole('alert')).toHaveTextContent('退出失败');
  expect(screen.getByLabelText('我的文字表达')).toHaveValue('没有保存的采访文字');
  fail = false;
  await userEvent.click(screen.getByRole('button', { name: '退出朋友身份' })); await acceptDialog('退出并放弃未保存输入');
  expect(await screen.findByLabelText('邀请代码')).toBeVisible();
  expect(screen.queryByLabelText('我的文字表达')).not.toBeInTheDocument();
});

it('backs off pending-work read failures, stops in the background and returns to low-frequency idle checks', async () => {
  await ready(); vi.useFakeTimers();
  invitation = { ...invitation, context_state: 'queued', context_preview_url: null };
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '刷新任务状态' })));
  const reads = () => vi.mocked(api).mock.calls.filter(([path]) => path === '/participant/auth/me').length;
  const before = reads(); readError = new AppError('NETWORK_ERROR', '暂时离线');
  await act(async () => vi.advanceTimersByTimeAsync(2000)); expect(reads()).toBe(before + 1);
  await act(async () => vi.advanceTimersByTimeAsync(1999)); expect(reads()).toBe(before + 1);
  await act(async () => vi.advanceTimersByTimeAsync(1)); expect(reads()).toBe(before + 2);
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  await act(async () => vi.advanceTimersByTimeAsync(60000)); expect(reads()).toBe(before + 2);
  readError = null; invitation = { ...invitation, context_state: 'ready', context_preview_url: '/api/v1/participant/context/media' };
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  const after = reads();
  await act(async () => vi.advanceTimersByTimeAsync(4999)); expect(reads()).toBe(after);
  await act(async () => vi.advanceTimersByTimeAsync(1)); expect(reads()).toBe(after + 1);
  expect(screen.getByLabelText('邀请中授权的上下文片段')).toBeVisible();
});

it('receives a new owner revision during idle polling without overwriting an unfinished expression', async () => {
  submissions = [draft({ state: 'submitted', can_delete_draft: false })];
  await ready(); vi.useFakeTimers();
  fireEvent.change(screen.getByLabelText('我的文字表达'), { target: { value: '还没保存的新想法' } });
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '刷新任务状态' })));
  submissions = [{ ...submissions[0], revisions: [{ id: id('e'), submission_id: id('c'), text: '发起人新提出的表达', start: 0, end: 0, state: 'proposed', created_at: '2026-11-08T00:00:00Z' }] }];
  await act(async () => vi.advanceTimersByTimeAsync(4999));
  expect(screen.queryByText('发起人提出的修订，等你决定')).not.toBeInTheDocument();
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByText('发起人提出的修订，等你决定')).toBeVisible();
  expect(screen.getByLabelText('我的文字表达')).toHaveValue('还没保存的新想法');
  expect(writes()).toHaveLength(0);
});
