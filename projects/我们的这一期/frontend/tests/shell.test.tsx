import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Dashboard } from '@/components/dashboard';
import { NavigationGuard, SessionBoundary } from '@/components/session-boundary';
import { safeNext } from '@/components/login';
import { Dialog, MediaAccessContext, MediaPlayer } from '@/components/ui';
import { api, AppError, setCsrf } from '@/lib/api';
import { clearWorkspaceDrafts, readWorkspaceDraft, setDraftOwner, writeWorkspaceDraft } from '@/components/studio/draft-cache';

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => router, usePathname: () => '/episodes/' + 'a'.repeat(32), useSearchParams: () => new URLSearchParams() }));
vi.mock('@/lib/api', async importActual => {
  const real = await importActual<typeof import('@/lib/api')>();
  return { ...real, api: vi.fn(), setCsrf: vi.fn() };
});
const status = { cloud_enabled:false, cloud_reason:'演示', media_ready:true, stage:'阶段3-1', workflow_enabled:true, model_mode:'demo' };
beforeEach(() => { clearWorkspaceDrafts(); Object.defineProperty(window,'navigation',{configurable:true,value:undefined}); vi.mocked(api).mockReset(); vi.mocked(setCsrf).mockClear(); router.push.mockReset(); router.replace.mockReset(); Object.defineProperty(document,'hidden',{configurable:true,value:false}); vi.spyOn(HTMLMediaElement.prototype,'pause').mockImplementation(() => {}); vi.spyOn(HTMLMediaElement.prototype,'load').mockImplementation(() => {}); window.history.replaceState({},'', '/'); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('accepts only internal episode return addresses', () => {
  const path = '/episodes/' + 'a'.repeat(32) + '?step=preview';
  expect(safeNext(path)).toBe(path);
  for (const suffix of ['/collaboration', '/sharing', '/sharing?from=preview']) {
    const nested = '/episodes/' + 'a'.repeat(32) + suffix;
    expect(safeNext(nested)).toBe(nested);
  }
  for (const suffix of ['/collaboration/../../api', '/watch', '/sharing#code=private', '/sharing?next=\\evil', '/sharing?x=\n']) {
    expect(safeNext('/episodes/' + 'a'.repeat(32) + suffix)).toBe('/');
  }
  for (const value of ['https://evil.invalid', '//evil.invalid', '/\\evil.invalid', '/api/v1/episodes', '/episodes/invalid', null]) expect(safeNext(value)).toBe('/');
});

it('waits for authenticated session and hides private children as soon as it expires', async () => {
  vi.mocked(api).mockImplementation(async path => path === '/status' ? status : { user_id:'demo-viewer', csrf_token:'fixture' });
  render(<SessionBoundary><p>私有节目内容</p></SessionBoundary>);
  expect(screen.queryByText('私有节目内容')).not.toBeInTheDocument();
  expect(await screen.findByText('私有节目内容')).toBeVisible();
  expect(setCsrf).toHaveBeenCalledWith('fixture');
  act(() => window.dispatchEvent(new Event('episode:auth-expired')));
  expect(screen.queryByText('私有节目内容')).not.toBeInTheDocument();
  expect(router.replace).toHaveBeenCalledWith(expect.stringContaining('/login?next='));
  expect(setCsrf).toHaveBeenLastCalledWith(null);
});

it('asks before leaving unsaved work and does not log out if declined', async () => {
  vi.mocked(api).mockImplementation(async path => path === '/status' ? status : { user_id:'demo-viewer', csrf_token:'fixture' });
  const confirm = vi.spyOn(window,'confirm').mockReturnValue(false);
  render(<SessionBoundary><p>编辑中</p></SessionBoundary>);
  await screen.findByText('编辑中');
  act(() => window.dispatchEvent(new CustomEvent('episode:dirty',{detail:true})));
  await userEvent.click(screen.getByRole('button',{name:'退出发起人身份'}));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(vi.mocked(api).mock.calls.some(([path]) => path === '/auth/logout')).toBe(false);
});

it('distinguishes a failed list request from an empty programme list', async () => {
  vi.mocked(api).mockRejectedValue(new AppError('NETWORK_ERROR','本机连接中断'));
  render(<Dashboard />);
  expect(await screen.findByRole('alert')).toHaveTextContent('本机连接中断');
  expect(screen.queryByText('第一期，从你的素材开始')).not.toBeInTheDocument();
  vi.mocked(api).mockResolvedValue({items:[]});
  await userEvent.click(screen.getByRole('button',{name:'刷新节目列表'}));
  expect(await screen.findByText('第一期，从你的素材开始')).toBeVisible();
});

it('does not repeat programme creation after an uncertain response and retains the entered draft', async () => {
  vi.mocked(api).mockImplementation(async (_path, options) => {
    if (options?.method === 'POST') throw new AppError('NETWORK_ERROR','结果未知',0,true);
    return {items:[]};
  });
  render(<Dashboard />);
  await screen.findByText('第一期，从你的素材开始');
  await userEvent.type(screen.getByRole('textbox',{name:'这一期的想法'}),'技术验收一期');
  await userEvent.click(screen.getByRole('button',{name:'开拍'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('结果未知');
  expect(screen.getByRole('textbox',{name:'这一期的想法'})).toHaveValue('技术验收一期');
  expect(screen.getByRole('textbox',{name:'这一期的想法'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'开拍'})).toBeDisabled();
  expect(vi.mocked(api).mock.calls.filter(([,options]) => options?.method === 'POST')).toHaveLength(1);
  expect(router.push).not.toHaveBeenCalled();
});

it('plays only the chosen source interval and never renders an external media address', async () => {
  const ended=vi.fn();
  const view=render(<MediaPlayer src={'/api/v1/assets/'+ 'a'.repeat(32)+'/media'} title="原片" start={2} end={4} onEnded={ended} />);
  const video=screen.getByLabelText('原片') as HTMLVideoElement;
  Object.defineProperty(video,'duration',{configurable:true,value:8});
  const pause=vi.spyOn(video,'pause').mockImplementation(() => {});
  fireEvent.loadedMetadata(video);
  expect(video.currentTime).toBe(2);
  video.currentTime=4; fireEvent.timeUpdate(video); fireEvent.timeUpdate(video);
  expect(pause).toHaveBeenCalled(); expect(ended).toHaveBeenCalledTimes(1);
  view.rerender(<MediaPlayer src="https://external.invalid/private.mp4" title="不允许的地址" />);
  await waitFor(() => expect(view.container.querySelector('video')).toBeNull());
});

it('stops and removes background media, then waits for permission revalidation to remount it', async () => {
  const src='/api/v1/assets/'+ 'a'.repeat(32)+'/media';
  const view=render(<MediaAccessContext.Provider value={{allowed:true,revision:1}}><MediaPlayer src={src} title="已授权原片" /></MediaAccessContext.Provider>);
  const video=screen.getByLabelText('已授权原片');
  Object.defineProperty(document,'hidden',{configurable:true,value:true});
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
  expect(video).not.toHaveAttribute('src');
  expect(view.container.querySelector('video')).toBeNull();
  view.rerender(<MediaAccessContext.Provider value={{allowed:false,revision:1}}><MediaPlayer src={src} title="已授权原片" /></MediaAccessContext.Provider>);
  Object.defineProperty(document,'hidden',{configurable:true,value:false});
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(view.container.querySelector('video')).toBeNull();
  view.rerender(<MediaAccessContext.Provider value={{allowed:true,revision:2}}><MediaPlayer src={src} title="已授权原片" /></MediaAccessContext.Provider>);
  expect(screen.getByLabelText('已授权原片')).toHaveAttribute('src',src);
});

it('offers a failed media retry that waits for a fresh server check', async () => {
  let finish!:()=>void;
  const revalidate=vi.fn(() => new Promise<void>(resolve => { finish=resolve; }));
  render(<MediaAccessContext.Provider value={{allowed:true,revision:0,revalidate}}><MediaPlayer src="/api/v1/assets/one/media" title="待重试原片" /></MediaAccessContext.Provider>);
  fireEvent.error(screen.getByLabelText('待重试原片'));
  fireEvent.click(screen.getByRole('button',{name:'重新核对并加载视频'}));
  expect(revalidate).toHaveBeenCalledTimes(1);
  expect(screen.queryByLabelText('待重试原片')).not.toBeInTheDocument();
  await act(async()=>{finish();});
  expect(screen.getByLabelText('待重试原片')).toBeInTheDocument();
});

it('closes a top-layer confirmation during permission checks without discarding its form', () => {
  const form=<Dialog open title="确认修改" onClose={()=>{}}><input aria-label="弹窗草案" defaultValue="保留" /></Dialog>;
  const view=render(<MediaAccessContext.Provider value={{allowed:true,revision:0}}>{form}</MediaAccessContext.Provider>);
  const field=screen.getByLabelText('弹窗草案');
  fireEvent.change(field,{target:{value:'未提交表达'}});
  expect(view.container.querySelector('dialog')).toHaveAttribute('open');
  view.rerender(<MediaAccessContext.Provider value={{allowed:false,revision:0}}>{form}</MediaAccessContext.Provider>);
  expect(view.container.querySelector('dialog')).not.toHaveAttribute('open');
  view.rerender(<MediaAccessContext.Provider value={{allowed:true,revision:1}}>{form}</MediaAccessContext.Provider>);
  expect(view.container.querySelector('dialog')).toHaveAttribute('open');
  expect(field).toHaveValue('未提交表达');
});

it('retains mounted draft fields while foreground session revalidation fails and is retried', async () => {
  vi.mocked(api).mockImplementation(async path => path==='/status'?status:{user_id:'demo-viewer',csrf_token:'fixture'});
  render(<SessionBoundary><label>草案<input defaultValue="未保存文字" /></label></SessionBoundary>);
  const draft=await screen.findByLabelText('草案');
  fireEvent.change(draft,{target:{value:'保留这段修改'}});
  Object.defineProperty(document,'hidden',{configurable:true,value:true});
  act(()=>document.dispatchEvent(new Event('visibilitychange')));
  expect(draft).toBeInTheDocument(); expect(draft).not.toBeVisible();
  vi.mocked(api).mockRejectedValue(new AppError('NETWORK_ERROR','暂时离线'));
  Object.defineProperty(document,'hidden',{configurable:true,value:false});
  await act(async()=>document.dispatchEvent(new Event('visibilitychange')));
  expect(draft).toHaveValue('保留这段修改'); expect(draft).not.toBeVisible();
  expect(screen.getByRole('alert')).toHaveTextContent('未保存编辑仍保留');
  vi.mocked(api).mockImplementation(async path => path==='/status'?status:{user_id:'demo-viewer',csrf_token:'fresh'});
  await userEvent.click(screen.getByRole('button',{name:'重新连接'}));
  await waitFor(()=>expect(draft).toBeVisible());
  expect(draft).toHaveValue('保留这段修改'); expect(setCsrf).toHaveBeenLastCalledWith('fresh');
});

it('does not resurrect protected content from a late auth response after session expiration', async () => {
  vi.mocked(api).mockImplementation(async path => path==='/status'?status:{user_id:'demo-viewer',csrf_token:'fixture'});
  render(<SessionBoundary><p>仍需有效登录</p></SessionBoundary>);
  await screen.findByText('仍需有效登录');
  let finish!:(value:unknown)=>void;
  vi.mocked(api).mockImplementation(path => path==='/status'?Promise.resolve(status):new Promise(resolve=>{finish=resolve;}));
  act(()=>document.dispatchEvent(new Event('visibilitychange')));
  act(()=>window.dispatchEvent(new Event('episode:auth-expired')));
  await act(async()=>{finish({user_id:'demo-viewer',csrf_token:'stale'});});
  expect(screen.queryByText('仍需有效登录')).not.toBeInTheDocument();
  expect(setCsrf).not.toHaveBeenCalledWith('stale');
});

it('remounts the workspace and clears prior user memory when foreground auth changes identity', async () => {
  let user='alice';
  vi.mocked(api).mockImplementation(async path=>path==='/status'?status:{user_id:user,csrf_token:'fixture'});
  render(<SessionBoundary><label>账号内草案<input defaultValue="新账号初始值" /></label></SessionBoundary>);
  const previous=await screen.findByLabelText('账号内草案');
  fireEvent.change(previous,{target:{value:'alice未保存内容'}});
  writeWorkspaceDraft('a'.repeat(32),{draft:{title:'alice记忆',segments:[],packaging:'light',narration_text:''}});
  user='bob';
  await act(async()=>document.dispatchEvent(new Event('visibilitychange')));
  expect(previous).not.toBeInTheDocument();
  expect(screen.getByLabelText('账号内草案')).toHaveValue('新账号初始值');
  expect(readWorkspaceDraft('a'.repeat(32))).toBeNull();
});

it('retains unsaved memory if logout fails and clears it only after logout succeeds', async () => {
  let fail=true;
  vi.mocked(api).mockImplementation(async path=>{
    if(path==='/auth/logout') { if(fail) throw new AppError('NETWORK_ERROR','退出未完成'); return {ok:true}; }
    return path==='/status'?status:{user_id:'alice',csrf_token:'fixture'};
  });
  vi.spyOn(window,'confirm').mockReturnValue(true);
  render(<SessionBoundary><p>已登录内容</p></SessionBoundary>); await screen.findByText('已登录内容');
  writeWorkspaceDraft('a'.repeat(32),{draft:{title:'仍需保留',segments:[],packaging:'light',narration_text:''}});
  window.dispatchEvent(new CustomEvent('episode:dirty',{detail:true}));
  await userEvent.click(screen.getByRole('button',{name:'退出发起人身份'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('退出未完成');
  expect(readWorkspaceDraft('a'.repeat(32))?.draft?.title).toBe('仍需保留');
  expect(router.replace).not.toHaveBeenCalled();
  fail=false; await userEvent.click(screen.getByRole('button',{name:'退出发起人身份'}));
  await waitFor(()=>expect(router.replace).toHaveBeenCalledWith('/login'));
  expect(readWorkspaceDraft('a'.repeat(32))).toBeNull();
});

it('cancels capable native traversals before commit and discards memory only on an explicit leave', () => {
  const original='/episodes/'+'a'.repeat(32)+'?step=edit';
  window.history.replaceState({},'',original);
  setDraftOwner('alice');
  writeWorkspaceDraft('a'.repeat(32),{draft:{title:'未保存',segments:[],packaging:'light',narration_text:''}});
  const navigation=new EventTarget(); Object.defineProperty(window,'navigation',{configurable:true,value:navigation});
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);
  render(<NavigationGuard />);
  act(()=>window.dispatchEvent(new CustomEvent('episode:dirty',{detail:true})));
  const traverse=(url:string,cancelable=true)=>{const event=new Event('navigate',{cancelable});Object.defineProperties(event,{navigationType:{value:'traverse'},destination:{value:{url}}});navigation.dispatchEvent(event);return event;};
  expect(traverse(window.location.origin+original.replace('edit','preview')).defaultPrevented).toBe(false);
  expect(confirm).not.toHaveBeenCalled();
  expect(traverse(window.location.origin+'/').defaultPrevented).toBe(true);
  expect(readWorkspaceDraft('a'.repeat(32))?.draft?.title).toBe('未保存');
  expect(window.location.pathname+window.location.search).toBe(original);
  confirm.mockReturnValue(true);
  expect(traverse(window.location.origin+'/').defaultPrevented).toBe(false);
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(readWorkspaceDraft('a'.repeat(32))).toBeNull();
  window.dispatchEvent(new PopStateEvent('popstate'));
  expect(confirm).toHaveBeenCalledTimes(2);
});

it('does not offer false cancellation for a noncancelable traversal and retains the memory draft', () => {
  window.history.replaceState({},'', '/episodes/'+'a'.repeat(32));
  setDraftOwner('alice'); writeWorkspaceDraft('a'.repeat(32),{draft:{title:'兼容草案',segments:[],packaging:'light',narration_text:''}});
  const navigation=new EventTarget(); Object.defineProperty(window,'navigation',{configurable:true,value:navigation});
  const confirm=vi.spyOn(window,'confirm'); render(<NavigationGuard />);
  window.dispatchEvent(new CustomEvent('episode:dirty',{detail:true}));
  const event=new Event('navigate',{cancelable:false});Object.defineProperties(event,{navigationType:{value:'traverse'},destination:{value:{url:window.location.origin+'/'}}});navigation.dispatchEvent(event);
  expect(confirm).not.toHaveBeenCalled();expect(readWorkspaceDraft('a'.repeat(32))?.draft?.title).toBe('兼容草案');
  window.dispatchEvent(new Event('episode:auth-expired'));
  expect(readWorkspaceDraft('a'.repeat(32))).toBeNull();
});

it('does not navigate when a programme creation response arrives after its page unmounts', async () => {
  let finish!:(value:unknown)=>void;
  vi.mocked(api).mockImplementation((_path,options)=>options?.method==='POST'?new Promise(resolve=>{finish=resolve;}):Promise.resolve({items:[]}));
  const view=render(<Dashboard />); await screen.findByText('第一期，从你的素材开始');
  fireEvent.change(screen.getByRole('textbox',{name:'这一期的想法'}),{target:{value:'迟到的创建'}});
  fireEvent.click(screen.getByRole('button',{name:'开拍'}));
  view.unmount();
  await act(async()=>{finish({id:'a'.repeat(32)});});
  expect(router.push).not.toHaveBeenCalled();
});

it('removes old dashboard video thumbnails until the foreground list recheck succeeds', async () => {
  const ep={id:'a'.repeat(32),title:'有成片的一期',created_at:'2026-11-08T10:00:00Z',assets:[],jobs:[],versions:[{id:'b'.repeat(32),state:'confirmed',media_url:'/api/v1/versions/one/media'}]};
  vi.mocked(api).mockResolvedValue({items:[ep]});
  const view=render(<Dashboard />); await screen.findByText('有成片的一期');
  expect(view.container.querySelector('video')).not.toBeNull();
  Object.defineProperty(document,'hidden',{configurable:true,value:true});
  act(()=>document.dispatchEvent(new Event('visibilitychange')));
  expect(view.container.querySelector('video')).toBeNull();
  let finish!:(value:unknown)=>void; vi.mocked(api).mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
  Object.defineProperty(document,'hidden',{configurable:true,value:false});
  act(()=>document.dispatchEvent(new Event('visibilitychange')));
  expect(view.container.querySelector('video')).toBeNull();
  await act(async()=>{finish({items:[{...ep,versions:[{...ep.versions[0],state:'restricted',media_url:null}]}]});});
  expect(view.container.querySelector('video')).toBeNull();
});
