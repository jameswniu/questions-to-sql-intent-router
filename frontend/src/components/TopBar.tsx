import type { ReactNode } from "react";

import { BrandMark } from "@/components/BrandMark";
import { cn } from "@/lib/cn";

export type Page = "chat" | "dashboard";

const PAGES: { page: Page; href: string; label: string }[] = [
  { page: "chat", href: "/", label: "Chat" },
  { page: "dashboard", href: "/dashboard", label: "Dashboard" },
];

/**
 * The sticky header: the product on the left, the pages an operator can open, and who is asking on the right. Only an
 * operator sees the pages, since the dashboard is theirs alone.
 */
export function TopBar({ page, ops, identity }: { page: Page; ops: boolean; identity?: ReactNode }) {
  return (
    <header className="topbar sticky top-0 z-30 border-b border-border bg-background/85 backdrop-blur-md">
      <div className="topbar-grid mx-auto max-w-[1200px] px-4 py-2.5 sm:px-6">
        <a href="/" className="brand flex min-w-0 items-center gap-2.5 rounded-md font-semibold tracking-tight">
          <BrandMark />
          <span className="truncate">Claims Q&amp;A</span>
        </a>
        <nav className="pages flex items-center gap-1 justify-self-end md:justify-self-start" aria-label="Pages">
          {ops &&
            PAGES.map((link) => (
              <a
                key={link.page}
                href={link.href}
                aria-current={link.page === page ? "page" : undefined}
                className={cn(
                  "nav rounded-md px-3 py-1.5 text-sm font-medium text-muted transition-colors hover:bg-surface-hover hover:text-foreground",
                  "aria-[current=page]:bg-surface-hover aria-[current=page]:text-foreground",
                )}
              >
                {link.label}
              </a>
            ))}
        </nav>
        {identity && <div className="identity min-w-0">{identity}</div>}
      </div>
    </header>
  );
}
