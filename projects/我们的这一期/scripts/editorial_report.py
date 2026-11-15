"""Read persisted editorial experiment metadata; never invokes a provider.

This is a component record, not the complete phase AI smoke acceptance.
"""
import argparse
from pathlib import Path
import json
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.config import Settings
from app.services import media


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record',type=Path,required=True)
    args=parser.parse_args()
    manifest=json.loads(args.record.read_text())
    settings=Settings.from_env()
    with sqlite3.connect(f'file:{settings.data_dir/"app.sqlite3"}?mode=ro',uri=True) as db:
        rows=db.execute('select kind,state,created_at,usage,elapsed from model_calls where job_id in (?,?) order by created_at',
                        (manifest['chat_job'],manifest['direction_job'])).fetchall()
        assert rows
        versions=[]
        for key in ('version_id','polished_version_id'):
            row=db.execute('select id,state,output_path,segments from versions where id=?',(manifest[key],)).fetchone()
            assert row and row[1] in ('ready','confirmed')
            info=media.probe(settings.data_dir/row[2])
            versions.append((row,info))
    lines=['# 后期编导节点实调记录','',
           '由 scripts/editorial_report.py 从本机账本与实际输出自动生成。只覆盖本轮导演文字/方向与本地合成，不替代完整阶段冒烟或成片质量验收。','',
           '| 调用 | 状态 | 时间 | 模型 | 输入/输出tokens | 费用元 | 耗时秒 |',
           '|---|---|---|---|---|---|---|']
    total=0
    for kind,state,created,raw,elapsed in rows:
        usage=json.loads(raw)
        assert usage.get('real_call') and usage['cost_status']=='known'
        total+=usage['cost_yuan']
        lines.append(f'| {kind} | {state} | {created} | {usage["model"]} | {usage["input_tokens"]}/{usage["output_tokens"]} | {usage["cost_yuan"]:.8f} | {elapsed:.3f} |')
    lines+=['',f'共{len(rows)}次真实调用（含结构失败与复测），本轮账本费用合计{total:.8f}元；未调用TTS。','']
    for label,(row,info) in zip(('模型原方案','助手修正文案版'),versions):
        segments=json.loads(row[3]);treatments=[s.get('editorial',{}).get('treatment') for s in segments]
        lines.append(f'- {label}：版本 `{row[0]}`，状态{row[1]}，{info["duration"]:.3f}秒，{info["width"]}×{info["height"]}，音轨={info["has_audio"]}；编排={" → ".join(treatments)}。')
    lines+=['','助手修正文案版仅调整开场标题、说明与理由；模型原版保留，不将人工修正记为模型自动质量通过。',
            '真实结果暴露的问题：模型初始结构失败两次；通过后的整屏文案仍照搬场景笔记；当前选段约11秒，未验证完整事件和最终综艺观感。提示词的文案反例是在本次实调后追加，尚未再次实调验证。',
            '下一步应补完整事件的时间范围与连续动作依据，再验证编排、定位和整片节奏，不用这份短片替代最终质量验收。','']
    (ROOT/'docs/evidence/阶段3-2/后期编导实调记录-20261115.md').write_text('\n'.join(lines))
    print(json.dumps({'calls':len(rows),'cost_yuan':round(total,8),'written':True}))

if __name__=='__main__':main()
