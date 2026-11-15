import Link from 'next/link';
import { BrandMark } from '@/components/brand-mark';
import { ProductionChrome } from '@/components/production-chrome';
export default function NotFound() { return <div className="production-shell"><ProductionChrome label="回到开场" /><main id="main" className="error-page"><Link href="/" className="zaichang-brand-link" aria-label="在场，返回开场"><BrandMark />在场</Link><span className="error-ticket" aria-hidden="true">这一幕，暂时走错片场</span><span className="eyebrow">没有找到这一页</span><h1>我们先回到开场</h1><p>地址可能已变化，也可能不属于你当前的访问范围。</p><Link className="button primary" href="/">查看我的节目</Link></main></div>; }
