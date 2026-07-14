"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { KanbanPanel } from "@/components/thread/kanban-panel";
import { Button } from "@/components/ui/button";
import { Toaster } from "@/components/ui/sonner";

export default function KanbanPage() {
  return (
    <main className="bg-muted/20 min-h-screen">
      <Toaster />
      <header className="bg-background sticky top-0 z-20 border-b">
        <div className="mx-auto flex max-w-[1800px] items-center justify-between gap-4 px-5 py-4">
          <div className="flex min-w-0 items-center gap-3">
            <Button
              variant="ghost"
              size="icon"
              asChild
            >
              <Link
                href="/"
                aria-label="Back to chat"
              >
                <ArrowLeft className="size-5" />
              </Link>
            </Button>
            <div className="min-w-0">
              <h1 className="text-lg font-semibold">Kanban</h1>
              <p className="text-muted-foreground text-xs">
                Durable multi-agent task queue and execution board.
              </p>
            </div>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-[1800px] px-5 py-6">
        <KanbanPanel />
      </div>
    </main>
  );
}
