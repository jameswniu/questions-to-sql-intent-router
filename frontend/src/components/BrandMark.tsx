import { BRAND_PATHS } from "@/components/brand";
import { cn } from "@/lib/cn";

export function BrandMark({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-grid size-7 shrink-0 place-items-center rounded-lg bg-accent text-white shadow-xs ring-1 ring-white/15 ring-inset",
        className,
      )}
    >
      <svg viewBox="0 0 16 16" className="size-[18px]" fill="none" stroke="currentColor" strokeWidth="1.5">
        {BRAND_PATHS.map((d) => (
          <path key={d} d={d} strokeLinecap="round" strokeLinejoin="round" />
        ))}
      </svg>
    </span>
  );
}
