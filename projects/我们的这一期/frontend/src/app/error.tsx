'use client';
import Link from 'next/link';
import { BrandMark } from '@/components/brand-mark';
import { ProductionChrome } from '@/components/production-chrome';
export default function PageError({ reset }: { reset: () => void }) {
  return <div className="production-shell"><ProductionChrome label="稍等一下" /><main id="main" className="error-page"><Link href="/" className="zaichang-brand-link" aria-label="在场，返回开场"><BrandMark />在场</Link><span className="error-ticket" aria-hidden="true">稍等一下，好戏继续</span><h1>这一页暂时没有打开</h1><p>已经保存的节目与版本仍在。可以重新读取页面。</p><button className="button primary" onClick={reset}>重新打开</button><Link className="button" href="/">返回节目列表</Link></main></div>;
}
