import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { FinishingRehearsal } from '@/components/studio/finishing-rehearsal';
import { SegmentSchema } from '@/lib/types';

afterEach(cleanup);
const points = ['文字','音效','旁白与包装'].map((label,i) => ({ version_id:String(i),label,choice:`选择${i}` }));
const episode = { versions:points.map(p=>({id:p.version_id})), jobs:[{id:'job',kind:'finishing_rehearsal',state:'succeeded',stage:'完成',error:null,version_id:'2',result:{simulation:true,checkpoints:points}}] };
it('navigates saved checkpoints without confirming or creating a film',()=>{
  const change=vi.fn(); const view=render(<FinishingRehearsal episode={episode} versionId="0" busy={false} onVersion={change}/>);
  fireEvent.click(screen.getByRole('button',{name:'看下一步 →'}));expect(change).toHaveBeenCalledWith('1');
  expect(screen.getByText(/助手模拟选择/)).toBeInTheDocument();
  view.rerender(<FinishingRehearsal episode={episode} versionId="2" busy={false} onVersion={change}/>);
  expect(screen.queryByRole('button',{name:'看下一步 →'})).not.toBeInTheDocument();
  expect(screen.getByText('下方是合并后的完整候选')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:/01\s*文字/}));expect(change).toHaveBeenLastCalledWith('0');
});
it('hides incomplete, unrelated and invalid rehearsal references',()=>{
  const view=render(<FinishingRehearsal episode={{...episode,versions:[]}} versionId="0" busy={false} onVersion={vi.fn()}/>);
  expect(view.container).toBeEmptyDOMElement();
  view.rerender(<FinishingRehearsal episode={episode} versionId="other" busy={false} onVersion={vi.fn()}/>);
  expect(view.container).toBeEmptyDOMElement();
});
it('preserves frame selections through the public response schema',()=>{
  const segment={asset_id:'a',start:0,end:5,caption:'',frame_accents:[{source_time:1,duration:1.5,palette:'mint'}]};
  expect(SegmentSchema.parse(segment)).toEqual(segment);
});
