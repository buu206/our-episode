import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SubtitleEditor } from '@/components/studio/subtitle-editor';
import { MediaAccessContext } from '@/components/ui';
import type { Version, Asset } from '@/lib/types';
const api=vi.hoisted(()=>vi.fn());
vi.mock('@/lib/api',()=>({api}));
afterEach(()=>{cleanup();vi.clearAllMocks();});
const aid='a'.repeat(32),vid='b'.repeat(32),cue='c'.repeat(32);
const version={id:vid,segments:[{asset_id:aid,start:0,end:3,caption:''}],packaging:'variety_v2'} as Version;
const asset={id:aid,duration:3} as Asset;
function mount(save=vi.fn().mockResolvedValue(false)){
 api.mockResolvedValue({version_id:vid,rows:[{cue_id:cue,segment_index:0,asset_id:aid,start:.5,end:1.5,text:'喝了',original_text:'喝了',source_text:'喝了',hidden:false,reviewed:false,locked:false,flags:['显示偏快'],media_url:`/api/v1/assets/${aid}/media`}]});
 render(<MediaAccessContext.Provider value={{allowed:true,revision:0}}><SubtitleEditor version={version} assets={[asset]} busy={false} onClose={vi.fn()} onSave={save}/></MediaAccessContext.Provider>);
 return save;
}
it('keeps an explicit correction after failed save and never changes the original version',async()=>{
 const save=mount();const input=await screen.findByLabelText('屏幕上显示的字幕');
 fireEvent.change(input,{target:{value:'不喝了！'}});
 fireEvent.click(screen.getByText('保存并生成新版'));
 await waitFor(()=>expect(save).toHaveBeenCalledWith([{...version.segments[0],subtitle_edits:[{cue_id:cue,text:'不喝了！',hidden:false,reviewed:true}]}]));
 expect(await screen.findByText(/尚未完成/)).toBeInTheDocument();expect(input).toHaveValue('不喝了！');
 expect(version.segments[0].subtitle_edits).toBeUndefined();
 fireEvent.click(screen.getByText('恢复识别文字'));expect(input).toHaveValue('喝了');
});
it('hides only the selected caption and replays from its source interval',async()=>{
 const save=mount();await screen.findByLabelText('屏幕上显示的字幕');
 const video=screen.getByLabelText('回听这一句') as HTMLVideoElement;const play=vi.spyOn(video,'play').mockResolvedValue();
 fireEvent.click(screen.getByRole('button',{name:'回听这一句'}));expect(video.currentTime).toBeCloseTo(.2);expect(play).toHaveBeenCalledOnce();
 fireEvent.click(screen.getByLabelText('不显示这一句（原声保留）'));fireEvent.click(screen.getByText('保存并生成新版'));
 await waitFor(()=>expect(save).toHaveBeenCalled());expect(save.mock.calls[0][0][0].subtitle_edits[0].hidden).toBe(true);
});

const row={cue_id:cue,segment_index:0,asset_id:aid,start:.5,end:1.5,text:'喝了',original_text:'喝了',source_text:'喝了',hidden:false,reviewed:false,locked:false,flags:[],media_url:`/api/v1/assets/${aid}/media`};
const target={version_id:vid,cue_id:cue,caption:'喝了',recognized:'不喝了'};
function targeted(rows=[{...row,cue_id:'first',text:'第一句'},row],selected=target){
 api.mockResolvedValue({version_id:vid,rows});
 const save=vi.fn().mockResolvedValue(true),close=vi.fn();
 render(<MediaAccessContext.Provider value={{allowed:true,revision:0}}><SubtitleEditor version={version} assets={[asset]} busy={false} target={selected} onClose={close} onSave={save}/></MediaAccessContext.Provider>);
 return {save,close};
}
it('opens the reported cue, requires an explicit draft choice, and saves a separate candidate',async()=>{
 const {save,close}=targeted();const input=await screen.findByLabelText('屏幕上显示的字幕');
 expect(screen.getByLabelText('选择字幕')).toHaveValue('1');expect(input).toHaveValue('喝了');
 expect(save).not.toHaveBeenCalled();expect(screen.getByText('保存并生成新版')).toBeDisabled();
 fireEvent.click(screen.getByRole('button',{name:'用作修改草稿'}));expect(input).toHaveValue('不喝了');expect(save).not.toHaveBeenCalled();
 fireEvent.click(screen.getByText('保存并生成新版'));
 await waitFor(()=>expect(close).toHaveBeenCalledOnce());
 expect(save).toHaveBeenCalledWith([{...version.segments[0],subtitle_edits:[{cue_id:cue,text:'不喝了',hidden:false,reviewed:true}]}]);
 expect(version.segments[0].subtitle_edits).toBeUndefined();
});
it.each(['missing','duplicate','changed','foreign','hidden'])('does not apply stale or ambiguous targets: %s',async mode=>{
 const rows=mode==='missing'?[{...row,cue_id:'different'}]:mode==='duplicate'?[row,{...row,segment_index:1}]:[{...row,...(mode==='changed'?{text:'新版本文字'}:mode==='hidden'?{hidden:true}:{})}];
 const {save}=targeted(rows,mode==='foreign'?{...target,version_id:'other'}:target);
 expect(await screen.findByText(/无法唯一对应当前字幕/)).toBeInTheDocument();
 expect(screen.queryByText('用作修改草稿')).not.toBeInTheDocument();expect(save).not.toHaveBeenCalled();
});
it('does not offer a replacement for missing speech and keeps protected interview text locked',async()=>{
 targeted([{...row,locked:true}]);await screen.findByLabelText('屏幕上显示的字幕');
 expect(screen.getByText('用作修改草稿')).toBeDisabled();expect(screen.getByLabelText('屏幕上显示的字幕')).toBeDisabled();
 cleanup();targeted([row],{...target,recognized:''});await screen.findByLabelText('屏幕上显示的字幕');
 expect(screen.queryByText('用作修改草稿')).not.toBeInTheDocument();expect(screen.getByLabelText('屏幕上显示的字幕')).toHaveValue('喝了');
});
it('closes an untouched suggestion without creating a new candidate',async()=>{
 const {save,close}=targeted();await screen.findByLabelText('屏幕上显示的字幕');
 fireEvent.click(screen.getByRole('button',{name:'关闭弹窗'}));expect(close).toHaveBeenCalledOnce();expect(save).not.toHaveBeenCalled();
});
