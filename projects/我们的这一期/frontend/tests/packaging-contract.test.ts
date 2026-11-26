import { expect, it } from "vitest";
import { SegmentSchema, EditorialBeatSchema } from "../src/lib/types";

it("retains per-effect palette, scale and muted action sounds through editing", () => {
  const segment = { asset_id: "a", start: 0, end: 4, caption: "", visual_beats: [{
    kind: "headline", source_time: .5, duration: 2, text: "稳住", palette: "mint", scale: .65,
    sound: "none", target: { x: .1, y: .1, width: .2, height: .2 },
  }] };
  expect(SegmentSchema.parse(segment)).toEqual(segment);
});

it("accepts bounded adaptive editorial settings without losing them on save", () => {
  const editorial = { role: "closing", treatment: "accent", reason: "已知结果", evidence_ids: ["v"],
    source_time: 1, duration: 2, text: "稳住", layout: "adaptive", sound: "resolve", sound_delay: 1.2 };
  expect(EditorialBeatSchema.parse(editorial)).toEqual(editorial);
  expect(EditorialBeatSchema.safeParse({ ...editorial, sound: "external-url" }).success).toBe(false);
  expect(EditorialBeatSchema.safeParse({ ...editorial, x: .5 }).success).toBe(false);
});
