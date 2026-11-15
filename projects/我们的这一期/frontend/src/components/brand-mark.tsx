import Image from 'next/image';

/** Original supplied artwork; CSS only frames its generous cream margins. */
export function BrandMark({ className = '' }: { className?: string }) {
  return <span className={`zaichang-mark ${className}`} aria-hidden="true"><Image src="/brand/zaichang-logo.png" alt="" width={1024} height={1024} unoptimized /></span>;
}
