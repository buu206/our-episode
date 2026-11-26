import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AudioCheck } from '@/components/studio/audio-check';
import { MediaAccessContext } from '@/components/ui';
import type { Job, Version } from '@/lib/types';
afterEach(cleanup);
const vid='b'.repeat(32);
const version={id:vid,title:'对白核对测试',media_url:`/api/v1/versions/${vid}/media`} as Version;
const finding={code:'CAPTION_AUDIO_DIFFERENCE',cue_id:'a',start:1.2,end:3.4,message:'这句需要回听。',caption:'先喝口',recognized:'先和口'};
const report={version_id:vid,scope:'output_audio_asr',verification:'unverified',duration:35,captions_checked:22,issue_count:1,findings_omitted:0,findings:[finding]};
function mount(result:unknown=report,state='succeeded',onCheck=vi.fn(),onSubtitles=vi.fn()) {
 const jobs=(state==='none'?[]:[{id:'j',kind:'audio_review',version_id:vid,state,result,error:state==='failed'?{message:'费用待核对。'}:null}]) as Job[];
 render(<MediaAccessContext.Provider value={{allowed:true,revision:0}}><AudioCheck version={version} jobs={jobs} busy={false} onCheck={onCheck} onSubtitles={onSubtitles}/></MediaAccessContext.Provider>);
 return {onCheck,onSubtitles};
}
it('requires deliberate entry before sending audio and does not auto-start',()=>{
 const {onCheck}=mount(null,'none');expect(onCheck).not.toHaveBeenCalled();
 expect(screen.getByText(/会将这一版的声音发送/)).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'核对成片对白'}));expect(onCheck).toHaveBeenCalledOnce();
});
it('shows uncertain comparison, opens local replay and navigates to correction',()=>{
 const {onSubtitles}=mount();expect(screen.getByText('对白与字幕 · 1处待听')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'回听 1—4秒'}));
 expect(screen.getByRole('dialog',{name:'听听这句原话'})).toBeInTheDocument();
 expect(screen.getByLabelText('对白核对测试')).toHaveAttribute('src',version.media_url);
 fireEvent.click(screen.getByRole('button',{name:'关闭弹窗'}));
 fireEvent.click(screen.getByRole('button',{name:'全部字幕'}));expect(onSubtitles).toHaveBeenCalledOnce();
 expect(screen.queryByText('字幕已正确')).not.toBeInTheDocument();
});
it('does not resend failed or running paid jobs',()=>{
 const {onCheck}=mount(null,'failed');expect(onCheck).not.toHaveBeenCalled();
 expect(screen.queryByRole('button',{name:'核对成片对白'})).not.toBeInTheDocument();
});
it('does not claim zero differences means verified truth',()=>{
 mount({...report,issue_count:0,findings:[]});expect(screen.getByText('已对照22句字幕')).toBeInTheDocument();
 expect(screen.getByText(/重新识别也可能听错/)).toBeInTheDocument();
});
it.each([{...report,version_id:'other'},{...report,findings:[{...finding,end:80}]},{...report,verification:'confirmed'}])('rejects stale or malformed reports',r=>{
 mount(r);expect(screen.queryByText('对白与字幕 · 1处待听')).not.toBeInTheDocument();
});
it('passes the exact version and cue to the correction entry without applying ASR',()=>{
 const {onSubtitles,onCheck}=mount();
 fireEvent.click(screen.getByRole('button',{name:'校对这句'}));
 expect(onSubtitles).toHaveBeenCalledWith({version_id:vid,cue_id:finding.cue_id,caption:finding.caption,recognized:finding.recognized});
 expect(onCheck).not.toHaveBeenCalled();
});

it('opens the matching cue in the director without invoking ASR or editing',()=>{
 const handler=vi.fn();window.addEventListener('director:review-cue',handler);
 try {
  const {onCheck,onSubtitles}=mount();fireEvent.click(screen.getByRole('button',{name:'让导演看这句'}));
  expect(handler).toHaveBeenCalledOnce();expect(handler.mock.calls[0][0].detail).toEqual({versionId:vid,cueId:'a',label:'这句字幕 · 1—4秒'});
  expect(onCheck).not.toHaveBeenCalled();expect(onSubtitles).not.toHaveBeenCalled();
 }finally{window.removeEventListener('director:review-cue',handler);}
});
