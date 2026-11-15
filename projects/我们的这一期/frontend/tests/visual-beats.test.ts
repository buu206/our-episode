import { describe, expect, it } from 'vitest';
import { SegmentSchema, type Version } from '../src/lib/types';
import { draftFromVersion } from '../src/components/studio/model';

const segment = { asset_id:'a',start:10,end:15,caption:'',visual_beats:[{
  kind:'focus',source_time:11,duration:1.5,text:'细节',position:'top_right',
  target:{x:.1,y:.2,width:.3,height:.3},
}] };
describe('visual accents survive the product editing round trip',()=>{
  it('retains editorial intent and rejects executable extra fields',()=>{
    const editorial={role:'action',treatment:'none',reason:'保留完整过程',evidence_ids:['v']};
    const parsed=SegmentSchema.parse({asset_id:'a',start:0,end:3,caption:'',editorial});
    const version={title:'test',id:'v',segments:[parsed],packaging:'variety_v2',narration_text:''} as Version;
    expect(draftFromVersion(version).segments[0].editorial).toEqual(editorial);
    expect(SegmentSchema.safeParse({...parsed,editorial:{...editorial,command:'execute'}}).success).toBe(false);
  });
  it('keeps the reviewed silhouette when reopening and saving a cutout version',()=>{
    const b={...segment.visual_beats[0],kind:'cutout',contour:[{x:.1,y:.2},{x:.3,y:.2},{x:.2,y:.4}]};
    const parsed=SegmentSchema.parse({...segment,visual_beats:[b]});
    const version={title:'cutout',id:'v',segments:[parsed],packaging:'variety_v2',narration_text:''} as Version;
    expect(draftFromVersion(version).segments[0].visual_beats).toEqual([b]);
    expect(SegmentSchema.safeParse({...segment,visual_beats:[{...b,contour:undefined}]}).success).toBe(false);
  });
  it('retains target coordinates and absolute source time in a new draft',()=>{
    const parsed=SegmentSchema.parse(segment);
    expect(parsed.visual_beats).toEqual(segment.visual_beats);
    const version={title:'test',id:'v',segments:[parsed],packaging:'variety_v2',narration_text:''} as Version;
    expect(draftFromVersion(version).segments[0].visual_beats).toEqual(segment.visual_beats);
  });
  it('rejects offscreen targets and unsupported effect names',()=>{
    expect(SegmentSchema.safeParse({...segment,visual_beats:[{...segment.visual_beats[0],target:{x:.9,y:0,width:.3,height:.2}}]}).success).toBe(false);
    expect(SegmentSchema.safeParse({...segment,visual_beats:[{...segment.visual_beats[0],kind:'execute'}]}).success).toBe(false);
  });
});
