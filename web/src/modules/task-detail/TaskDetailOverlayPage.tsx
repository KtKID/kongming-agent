import { useMemo } from "react";
import type { ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, FileText, Workflow } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { WorkflowViewerEmbed } from "@/modules/agent-workflow-viewer";
import { ThreadFileBrowser } from "./ThreadFileBrowser";

type TaskDetailTab = "session" | "workflows";

export function TaskDetailOverlayPage() {
  const params = useParams<{
    thread_id?: string;
    artifact_id?: string;
    workflow_id?: string;
  }>();
  const location = useLocation();
  const navigate = useNavigate();
  const threadId = params.thread_id;
  const activeTab: TaskDetailTab = location.pathname.includes("/agent-workflows")
    ? "workflows"
    : "session";

  const tabLinks = useMemo(
    () =>
      threadId
        ? {
            session: `/chat/${threadId}/task-detail`,
            workflows: `/chat/${threadId}/agent-workflows`,
          }
        : { session: "/chat", workflows: "/chat" },
    [threadId],
  );

  if (!threadId) return null;

  const selectArtifact = (artifactId: string) => {
    navigate(`/chat/${threadId}/task-detail/files/${encodeURIComponent(artifactId)}`);
  };

  const selectWorkflow = (workflowId: string) => {
    navigate(`/chat/${threadId}/agent-workflows/${encodeURIComponent(workflowId)}`);
  };

  return (
    <div
      className="absolute inset-0 z-40 bg-background/72 backdrop-blur-sm"
      data-testid="task-detail-overlay"
    >
      <div className="flex h-full min-h-0 p-2 sm:p-3">
        <section className="obsidian-panel-soft flex min-h-0 min-w-0 flex-1 overflow-hidden rounded-xl border border-border/80 bg-background/96 shadow-2xl">
          <aside className="flex w-44 shrink-0 flex-col border-r border-border/70 bg-card/60 p-3">
            <Button
              variant="ghost"
              size="sm"
              className="mb-3 h-8 w-full justify-start gap-1.5 px-2 text-muted-foreground hover:text-foreground"
              onClick={() => navigate(`/chat/${threadId}`)}
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              返回对话
            </Button>
            <div className="mb-4 min-w-0">
              <div className="truncate text-sm font-semibold text-foreground">任务详情</div>
              <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                {threadId}
              </div>
            </div>
            <nav className="space-y-1">
              <TabLink
                to={tabLinks.session}
                active={activeTab === "session"}
                icon={<FileText className="h-4 w-4" />}
                label="会话内容"
              />
              <TabLink
                to={tabLinks.workflows}
                active={activeTab === "workflows"}
                icon={<Workflow className="h-4 w-4" />}
                label="Workflows"
              />
            </nav>
          </aside>

          <main className="flex min-h-0 min-w-0 flex-1 flex-col p-3">
            {activeTab === "session" ? (
              <ThreadFileBrowser
                threadId={threadId}
                selectedArtifactId={params.artifact_id}
                onSelectArtifact={selectArtifact}
              />
            ) : (
              <WorkflowViewerEmbed
                threadId={threadId}
                workflowId={params.workflow_id}
                onSelectWorkflow={selectWorkflow}
                showStandaloneHeader={false}
              />
            )}
          </main>
        </section>
      </div>
    </div>
  );
}

function TabLink({
  to,
  active,
  icon,
  label,
}: {
  to: string;
  active: boolean;
  icon: ReactNode;
  label: string;
}) {
  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex h-9 items-center gap-2 rounded-lg border px-2.5 text-sm font-medium transition-colors",
        active
          ? "border-border/80 bg-secondary/90 text-foreground shadow-sm"
          : "border-transparent text-muted-foreground hover:border-border/60 hover:bg-secondary/60 hover:text-foreground",
      )}
    >
      {icon}
      <span className="truncate">{label}</span>
    </Link>
  );
}
