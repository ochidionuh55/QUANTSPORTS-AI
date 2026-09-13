import Link from "next/link";
import { Mark } from "./Mark";

/** The lockup. Spacing is fixed here so it is never stretched or crowded. */
export function Wordmark({ size = 30 }: { size?: number }) {
  return (
    <Link href="/" className="group flex items-center gap-2.5">
      <span className="transition-transform duration-slow ease-quant group-hover:rotate-[8deg]">
        <Mark size={size} />
      </span>
      <span className="text-[15px] font-semibold tracking-[-0.03em]">
        QUANTSPORT <span className="text-emerald">AI</span>
      </span>
    </Link>
  );
}
