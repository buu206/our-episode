'use client';
import Link from 'next/link';
export default function PageError({ reset }: { reset: () => void }) {
  return <main id="main" className="error-page"><h1>这一页暂时没有打开</h1><p>已经保存的节目与版本仍在。可以重新读取页面。</p><button className="button primary" onClick={reset}>重新打开</button><Link className="button" href="/">返回节目列表</Link></main>;
}
