import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import type { FinishingState, Version } from '@/lib/types';
import { FinishingEditor } from '@/components/studio/finishing-editor';
import { api } from '@/lib/api';
import { routeContract } from '@/lib/routes';

vi.mock('@/lib/api',()=>({api:vi.fn()}));
vi.mock('@/components/ui',()=>({
  Dialog:({children,onClose}:{children:ReactNode;onClose:()=>void})=><div><button onClick={onClose}>关闭</button>{children}</div>,
  Button:({children,disabled,onClick}:React.ButtonHTMLAttributes<HTMLButtonElement>&{variant?:string})=><button disabled={disabled} onClick={onClick}>{children}</button>,
  Notice:({children}:{children:ReactNode})=><div role="alert">{children}</div>,
  MediaPlayer:()=> <div>参考视频</div>,
}));
const vid='a'.repeat(32), madeId='b'.repeat(32);
const version={id:vid,title:'试剪',packaging:'variety_v2',narration_text:'',narration_ready:false,segments:[{asset_id:'c'.repeat(32),start:0,end:3,caption:'',visual_beats:[{kind:'headline',source_time:1,duration:1,text:'稳住',sound:'none',palette:'amber',target:{x:0,y:0,width:.3,height:.3}}]}]} as Version;
let state:FinishingState;
beforeEach(()=>{
  state={revision:0,step:1,completed:0,generated_version_id:null,draft:{title:'试剪',packaging:'variety_v2',narration_text:'',base_version_id:vid,segments:structuredClone(version.segments)}};
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/subtitles'))return {version_id:vid,rows:[]};
    if(path.endsWith('/commit'))return {...version,id:madeId,state:'draft',narration_text:state.draft.narration_text};
    if(path.endsWith('/render'))return {id:'job'};
    if(options?.method==='PUT'){
      const body=options.body as {draft:FinishingState['draft'];action:string};
      state={...state,revision:state.revision+1,draft:structuredClone(body.draft)};
      if(body.action==='next'){state.completed=state.step;state.step=Math.min(3,state.step+1);}
      if(body.action==='back')state.step=Math.max(1,state.step-1);
    }
    return structuredClone(state);
  });
});
afterEach(()=>{cleanup();vi.resetAllMocks();});

it('edits all three layers and requests only local composition, never film acceptance',async()=>{
  const created=vi.fn(),close=vi.fn();render(<FinishingEditor version={version} onCreated={created} onClose={close}/>);
  fireEvent.change(await screen.findByLabelText('花字 1.1'),{target:{value:'拿到了'}});
  fireEvent.click(screen.getByRole('button',{name:'保存，下一步：音效'}));
  fireEvent.change(await screen.findByLabelText('音效 1.1'),{target:{value:'wood'}});
  fireEvent.click(screen.getByRole('button',{name:'保存，下一步：旁白与包装'}));
  fireEvent.click(await screen.findByRole('button',{name:'在「拿到了」加边框'}));
  fireEvent.click(screen.getByRole('button',{name:'保存并生成新版'}));
  await waitFor(()=>expect(created).toHaveBeenCalledWith(expect.objectContaining({id:madeId})));
  expect(state.draft.segments[0].visual_beats?.[0]).toMatchObject({text:'拿到了',sound:'wood'});
  expect(state.draft.segments[0].frame_accents).toHaveLength(1);
  expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/render'))).toBe(true);
  expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/confirm')||p.endsWith('/narration'))).toBe(false);
});
it('restores a saved step and saves new narration without silently requesting paid speech',async()=>{
  state.step=3;state.completed=2;
  const created=vi.fn();render(<FinishingEditor version={version} onCreated={created} onClose={vi.fn()}/>);
  fireEvent.change(await screen.findByLabelText('开场旁白'),{target:{value:'开拍！'}});
  fireEvent.click(screen.getByRole('button',{name:'保存新版，去确认配音'}));
  await waitFor(()=>expect(created).toHaveBeenCalled());
  expect(state.draft.narration_text).toBe('开拍！');
  expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/render')||p.endsWith('/narration'))).toBe(false);
});
it('keeps unsaved input on failure and does not advance or create a version',async()=>{
  render(<FinishingEditor version={version} onCreated={vi.fn()} onClose={vi.fn()}/>);
  fireEvent.change(await screen.findByLabelText('花字 1.1'),{target:{value:'再试一次'}});
  vi.mocked(api).mockRejectedValueOnce(new Error('保存冲突'));
  fireEvent.click(screen.getByRole('button',{name:'保存，下一步：音效'}));
  await screen.findByText('保存冲突');
  expect(screen.getByLabelText('花字 1.1')).toHaveValue('再试一次');
  expect(screen.queryByLabelText('音效 1.1')).not.toBeInTheDocument();
});
it('restricts finishing routes to owner and controlled verbs',()=>{
  expect(routeContract(`/versions/${vid}/finishing`,'PUT')).toMatchObject({kind:'finishing',role:'owner',public:false});
  expect(routeContract(`/versions/${vid}/finishing/commit`,'POST')).toMatchObject({kind:'version'});
  expect(routeContract(`/versions/${vid}/finishing/commit`,'GET')).toBeNull();
});
