import { lazy, Suspense } from "react";

import { ChatPage } from "@/chat/ChatPage";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { PageName } from "@/page";

// The dashboard is for operators only, so its code loads only on its own page.
const DashboardPage = lazy(() => import("@/dashboard/DashboardPage"));

export function App({ page }: { page: PageName }) {
  return (
    <TooltipProvider delayDuration={300}>
      {page === "dashboard" ? (
        <Suspense fallback={null}>
          <DashboardPage />
        </Suspense>
      ) : (
        <ChatPage />
      )}
    </TooltipProvider>
  );
}
