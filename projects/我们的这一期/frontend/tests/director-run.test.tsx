import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {DirectorRun} from '@/components/studio/director-run';
import {isActiveJob} from '@/components/studio/model';
import type {Job} from '@/lib/types';
afterEach(cleanup);
const run=(state:string)=>({id:'r',kind:'director_run',state,stage:'状态说明',error:null} as Job);
it('keeps controls available while work runs and describes deferred pause honestly',()=>{
 const action=vi.fn();render(<DirectorRun run={run('active')} busy={false} onControl={action}/>);
 fireEvent.click(screen.getByRole('button',{name:'暂停导演'}));expect(action).toHaveBeenLastCalledWith('pause');
 fireEvent.click(screen.getByRole('button',{name:'结束委托'}));expect(action).toHaveBeenLastCalledWith('cancel');
 expect(screen.getByText(/正在合成的任务会收尾保存/)).toBeInTheDocument();
});
it.each(['paused','blocked'])('resumes %s without offering confirmation',state=>{
 const action=vi.fn();render(<DirectorRun run={run(state)} busy={false} onControl={action}/>);
 fireEvent.click(screen.getByRole('button',{name:'继续制作'}));expect(action).toHaveBeenCalledWith('resume');
 expect(screen.queryByRole('button',{name:'确认当前版本'})).not.toBeInTheDocument();
});
it.each(['awaiting_user','cancelled','cancelling'])('does not offer a restart in %s',state=>{
 render(<DirectorRun run={run(state)} busy={false} onControl={vi.fn()}/>);
 expect(screen.queryByRole('button',{name:'继续制作'})).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'暂停导演'})).not.toBeInTheDocument();
});
it('polls only states with ongoing work',()=>{
 for(const s of ['active','pausing','cancelling'])expect(isActiveJob(run(s))).toBe(true);
 for(const s of ['paused','blocked','awaiting_user','cancelled'])expect(isActiveJob(run(s))).toBe(false);
 expect(isActiveJob({...run('active'),kind:'unknown'})).toBe(false);
});

it('describes accepted revisions without implying a new original draft or approval',()=>{
 render(<DirectorRun run={{...run('active'),result:{scope:'approved_revision_render_and_local_check'}}} busy={false}/>);
 expect(screen.getByText(/按你已确认的修改生成新候选/)).toBeInTheDocument();
 expect(screen.queryByText(/按已选拍法合成原声初稿/)).not.toBeInTheDocument();
 expect(screen.getByText(/完成后停在候选，不替你确认/)).toBeInTheDocument();
});
