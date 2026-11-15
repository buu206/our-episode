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
