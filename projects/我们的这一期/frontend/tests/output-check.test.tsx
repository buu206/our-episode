import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { OutputCheck } from '@/components/studio/output-check';
import { MediaAccessContext } from '@/components/ui';
import type { Job, Version } from '@/lib/types';

afterEach(cleanup);
const vid='b'.repeat(32);
const version={id:vid,title:'检查用片',state:'ready',media_url:`/api/v1/versions/${vid}/media`} as Version;
function mount(result: unknown, state='succeeded', onCheck=vi.fn()) {
  const job={id:'j',kind:'technical_review',version_id:vid,state,result} as Job;
  render(<MediaAccessContext.Provider value={{allowed:true,revision:0}}><OutputCheck version={version} jobs={[job]} busy={false} onCheck={onCheck}/></MediaAccessContext.Provider>);
  return onCheck;
}
it('shows only valid current-version inspection and never calls it creative approval',()=>{
  mount({scope:'local_technical',version_id:vid,decoded_full_output:true,status:'checked',issues:[]});
  expect(screen.getByText('画面与音轨检查完成')).toBeInTheDocument();
  expect(screen.queryByText('审片通过')).not.toBeInTheDocument();
});
it('replays a quiet interval in a dialog at the current location',()=>{
  mount({scope:'local_technical',version_id:vid,decoded_full_output:true,status:'needs_attention',
    issues:[{code:'QUIET_INTERVAL',start:2,end:4,message:'这里声音很轻，也可能是自然停顿，建议回听。'}]});
  fireEvent.click(screen.getByRole('button',{name:'回听 2—4秒'}));
  expect(screen.getByRole('dialog',{name:'回听这一处'})).toBeInTheDocument();
  expect(screen.getByLabelText('检查用片')).toHaveAttribute('src',version.media_url);
});
it('does not display another version report or malformed result as success',()=>{
  const check=mount({scope:'local_technical',version_id:'other',decoded_full_output:true,status:'checked',issues:[]});
  expect(screen.queryByText('画面与音轨检查完成')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'检查画面与声音'}));
  expect(check).toHaveBeenCalledOnce();
});
it('allows a failed local check to be retried without a render action',()=>{
  const check=mount(null,'failed');
  fireEvent.click(screen.getByRole('button',{name:'重新检查'}));
  expect(check).toHaveBeenCalledOnce();
  expect(screen.queryByText('重新生成')).not.toBeInTheDocument();
});

const visualResult={version_id:vid,scope:'sampled_visual',verification:'unverified',audio_analyzed:false,
  summary:'抽样画面中有一处字靠近脸。',duration:35,sample_times:[2,8],
  findings:[{time:8,observation:'这行字靠近人物的脸。',suggestion:'向左移一点。'}]};
function mountVisual(result:unknown,state='succeeded') {
  render(<MediaAccessContext.Provider value={{allowed:true,revision:0}}><OutputCheck version={version}
    jobs={[{id:'v',kind:'visual_review',version_id:vid,state,result} as Job]} busy={false}/></MediaAccessContext.Provider>);
}
it('keeps visual suggestions short and replays locally without confirming',()=>{
  mountVisual(visualResult);
  expect(screen.getByText('导演看了2处画面 · 1条建议')).toBeInTheDocument();
  expect(screen.getByText('仅抽样截图，未听声音；以下是待核对建议。')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'回看 8秒附近'}));
  expect(screen.getByRole('dialog',{name:'回看这一处'})).toBeInTheDocument();
  expect(screen.queryByText('审片通过')).not.toBeInTheDocument();
});
it.each([{...visualResult,version_id:'other'}, {...visualResult,findings:[{...visualResult.findings[0],time:40}]}])('rejects a foreign report or unobserved position',result=>{
  mountVisual(result);
  expect(screen.queryByText('导演看了2处画面 · 1条建议')).not.toBeInTheDocument();
});
it('does not resend a failed paid review automatically',()=>{
  mountVisual(null,'failed');
  expect(screen.getByText(/画面审片未完成，候选仍保留/)).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'检查这一版画面'})).not.toBeInTheDocument();
});

it('keeps source timing advice compact and never seeks output using source seconds',()=>{
  const subtitles=vi.fn();
  const source={scope:'source_metadata',evidence_confirmed:true,speech_checked:4,captions_checked:4,
    issue_count:4,issues:[1,2,3,4].map(n=>({code:'CAPTION_REVIEW',segment_number:n,message:`第${n}条显示偏快`,source_start:50,source_end:52}))};
  render(<OutputCheck version={version} jobs={[{id:'j',kind:'technical_review',version_id:vid,state:'succeeded',stage:'已完成',error:null,
    result:{scope:'local_technical',version_id:vid,decoded_full_output:true,status:'checked',issues:[],source_review:source}} as Job]}
    busy={false} onSubtitles={subtitles}/>);
  expect(screen.getByText('剪辑与字幕 · 4处建议')).toBeInTheDocument();
  expect(screen.queryByText(/第4条显示偏快/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:/50/})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'去校对字幕'}));
  expect(subtitles).toHaveBeenCalledOnce();
});
it('does not claim source review when evidence is unconfirmed',()=>{
  mount({scope:'local_technical',version_id:vid,decoded_full_output:true,status:'checked',issues:[],
    source_review:{scope:'source_metadata',evidence_confirmed:false,speech_checked:0,captions_checked:0,issue_count:0,issues:[]}});
  expect(screen.queryByText('原话边界与字幕时长已核对')).not.toBeInTheDocument();
});

it('opens visual playback hints as local video replay rather than audio faults',()=>{
  mount({scope:'local_technical',version_id:vid,decoded_full_output:true,status:'needs_attention',issues_omitted:2,
    issues:[{code:'STILL_INTERVAL',start:2.4,end:5.6,message:'这里画面较长时间几乎没动，也可能是定格或静态片头，建议回看。'}]});
  expect(screen.getByText('播放检查 · 3处待看')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'回看 2—6秒'}));
  expect(screen.getByRole('dialog',{name:'回看这一处'})).toBeInTheDocument();
  expect(screen.queryByText('审片通过')).not.toBeInTheDocument();
});
