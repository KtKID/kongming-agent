import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  BarChart3,
  Boxes,
  ChevronRight,
  ClipboardList,
  Clock3,
  FileText,
  MessageSquareText,
  RefreshCw,
  Route,
  Split,
  Workflow,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import {
  conversationKey,
  useAgentWorkflowViewerStore,
} from "./store";
import type {
  ConversationDTO,
  ConversationMessageDTO,
  SubAgentReportSummaryDTO,
  WorkflowArtifactContentDTO,
  WorkflowArtifactRefDTO,
  WorkflowDetailDTO,
  WorkflowDiagnosticDTO,
  WorkflowFlowEdgeDTO,
  WorkflowFlowNodeDTO,
  WorkflowListItemDTO,
  WorkflowPanelDTO,
  WorkflowUsageDTO,
} from "./types";

export function WorkflowRunViewerPage(): ReactNode {
  const { thread_id: threadId, workflow_id: workflowId } = useParams<{
    thread_id?: string;
    workflow_id?: string;
  }>();
  const navigate = useNavigate();
  const [selectedTaskRunId, setSelectedTaskRunId] = useState<string | null>(null);
  const list = useAgentWorkflowViewerStore((s) => s.list);
  const detail = useAgentWorkflowViewerStore((s) => s.detail);
  const conversations = useAgentWorkflowViewerStore((s) => s.conversations);
  const artifact = useAgentWorkflowViewerStore((s) => s.artifact);
  const threadUsage = useAgentWorkflowViewerStore((s) => s.threadUsage);
  const loadingList = useAgentWorkflowViewerStore((s) => s.loadingList);
  const loadingDetail = useAgentWorkflowViewerStore((s) => s.loadingDetail);
  const loadingConversation = useAgentWorkflowViewerStore(
    (s) => s.loadingConversation,
  );
  const loadingArtifact = useAgentWorkflowViewerStore((s) => s.loadingArtifact);
  const loadingThreadUsage = useAgentWorkflowViewerStore(
    (s) => s.loadingThreadUsage,
  );
  const error = useAgentWorkflowViewerStore((s) => s.error);
  const artifactError = useAgentWorkflowViewerStore((s) => s.artifactError);
  const clearThread = useAgentWorkflowViewerStore((s) => s.clearThread);
  const clearArtifact = useAgentWorkflowViewerStore((s) => s.clearArtifact);
  const loadList = useAgentWorkflowViewerStore((s) => s.loadList);
  const loadThreadUsage = useAgentWorkflowViewerStore(
    (s) => s.loadThreadUsage,
  );
  const loadDetail = useAgentWorkflowViewerStore((s) => s.loadDetail);
  const loadConversation = useAgentWorkflowViewerStore(
    (s) => s.loadConversation,
  );
  const loadArtifact = useAgentWorkflowViewerStore((s) => s.loadArtifact);

  useEffect(() => {
    if (!threadId) return;
    clearThread(threadId);
    void loadList(threadId);
    void loadThreadUsage(threadId);
  }, [clearThread, loadList, loadThreadUsage, threadId]);

  useEffect(() => {
    if (!threadId || !workflowId) return;
    setSelectedTaskRunId(null);
    void loadDetail(threadId, workflowId);
  }, [loadDetail, threadId, workflowId]);

  useEffect(() => {
    if (!detail) return;
    const preferred =
      detail.reports.find((report) => report.conversation_available) ??
      detail.reports[0] ??
      null;
    setSelectedTaskRunId((current) =>
      current && detail.reports.some((report) => report.task_run_id === current)
        ? current
        : preferred?.task_run_id ?? null,
    );
  }, [detail]);

  useEffect(() => {
    if (!threadId || !workflowId || !selectedTaskRunId) return;
    const selected = detail?.reports.find(
      (report) => report.task_run_id === selectedTaskRunId,
    );
    if (!selected?.conversation_available) return;
    const key = conversationKey(workflowId, selectedTaskRunId);
    if (conversations[key]) return;
    void loadConversation(threadId, workflowId, selectedTaskRunId);
  }, [
    conversations,
    detail,
    loadConversation,
    selectedTaskRunId,
    threadId,
    workflowId,
  ]);

  if (!threadId) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        需要先选择一个 thread
      </div>
    );
  }

  const selectedConversation =
    workflowId && selectedTaskRunId
      ? conversations[conversationKey(workflowId, selectedTaskRunId)]
      : undefined;
  const selectedReport = detail?.reports.find(
    (report) => report.task_run_id === selectedTaskRunId,
  );

  const openWorkflow = (id: string) => {
    clearArtifact();
    navigate(`/chat/${threadId}/agent-workflows/${id}`);
  };

  const refresh = () => {
    void loadList(threadId);
    void loadThreadUsage(threadId);
    if (workflowId) void loadDetail(threadId, workflowId);
  };

  return (
    <div
      className="flex h-full min-h-0 flex-col px-3 pt-3"
      data-testid="workflow-viewer-page"
    >
      <section className="obsidian-panel-soft mb-3 flex shrink-0 flex-wrap items-center gap-3 rounded-2xl px-4 py-3">
        <Link
          to={`/chat/${threadId}`}
          className="inline-flex items-center gap-1.5 rounded-xl border border-border/70 bg-card/70 px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Chat
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Workflow className="h-4 w-4 text-primary" />
            Agent Workflow Viewer
          </div>
          <div className="mt-0.5 truncate text-xs text-muted-foreground">
            {workflowId ?? "选择 workflow 查看流程、子 agent 对话和 token 审计"}
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={refresh}
          disabled={loadingList || loadingDetail}
          className="gap-1.5"
        >
          <RefreshCw
            className={cn(
              "h-3.5 w-3.5",
              loadingList || loadingDetail ? "animate-spin" : "",
            )}
          />
          刷新
        </Button>
      </section>

      {error ? (
        <div className="mb-3 rounded-xl border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      <div className="grid min-h-0 min-w-0 flex-1 gap-3 lg:grid-cols-[22rem_minmax(0,1fr)]">
        <WorkflowListPanel
          workflows={list?.workflows ?? []}
          selectedWorkflowId={workflowId ?? null}
          loading={loadingList}
          onSelect={openWorkflow}
        />
        <ScrollArea className="min-h-0 min-w-0 rounded-2xl border border-border/70 bg-card/50">
          <div className="min-w-0 space-y-3 p-3">
            {workflowId && loadingDetail ? (
              <LoadingBlock label="加载 workflow 明细" />
            ) : detail ? (
              <>
                <WorkflowHeader item={detail.item} usage={detail.usage} />
                <ThreadUsagePanel
                  usage={threadUsage}
                  loading={loadingThreadUsage}
                />
                <Diagnostics diagnostics={detail.diagnostics} />
                <FlowGraph nodes={detail.flow_nodes} edges={detail.flow_edges} />
                <ModePanels panels={detail.panels} />
                <SubAgentConversationSection
                  reports={detail.reports}
                  selectedTaskRunId={selectedTaskRunId}
                  selectedReport={selectedReport}
                  conversation={selectedConversation}
                  loading={loadingConversation}
                  onSelect={setSelectedTaskRunId}
                />
                <ArtifactSection
                  artifacts={detail.artifacts}
                  artifact={artifact}
                  loading={loadingArtifact}
                  error={artifactError}
                  onOpen={(artifactId) => {
                    void loadArtifact(threadId, detail.item.workflow_id, artifactId);
                  }}
                  onClose={clearArtifact}
                />
                <TimelineSection detail={detail} />
              </>
            ) : (
              <EmptyState />
            )}
          </div>
        </ScrollArea>
      </div>
    </div>
  );
}

function WorkflowListPanel(props: {
  workflows: WorkflowListItemDTO[];
  selectedWorkflowId: string | null;
  loading: boolean;
  onSelect: (workflowId: string) => void;
}) {
  return (
    <aside className="obsidian-panel-soft flex min-h-0 min-w-0 flex-col rounded-2xl">
      <div className="shrink-0 border-b border-border/70 px-4 py-3">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-foreground">Workflow Runs</h2>
          <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
            {props.loading ? "..." : props.workflows.length}
          </span>
        </div>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-2 p-2">
          {props.workflows.map((workflow) => (
            <button
              key={workflow.workflow_id}
              type="button"
              onClick={() => props.onSelect(workflow.workflow_id)}
              className={cn(
                "w-full rounded-xl border p-3 text-left transition-colors",
                props.selectedWorkflowId === workflow.workflow_id
                  ? "border-primary/35 bg-primary/10"
                  : "border-border/70 bg-card/65 hover:bg-secondary/70",
              )}
            >
              <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between sm:gap-2">
                <span className="min-w-0 truncate text-sm font-semibold text-foreground">
                  {workflow.title}
                </span>
                <StatusPill status={workflow.status} />
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                <BadgeText>{workflow.mode}</BadgeText>
                <BadgeText>{workflow.report_count} reports</BadgeText>
                <BadgeText>{formatUsage(workflow.usage.totals)}</BadgeText>
              </div>
              <div className="mt-2 truncate font-mono text-[11px] text-muted-foreground">
                {workflow.workflow_id}
              </div>
            </button>
          ))}
          {!props.loading && props.workflows.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border/80 px-3 py-8 text-center text-sm text-muted-foreground">
              当前 thread 暂无 workflow 记录
            </div>
          ) : null}
        </div>
      </ScrollArea>
    </aside>
  );
}

function WorkflowHeader(props: {
  item: WorkflowListItemDTO;
  usage: WorkflowUsageDTO;
}) {
  const totalTokens = tokenTotal(props.usage.totals);
  return (
    <section
      className="obsidian-panel-soft min-w-0 rounded-2xl p-4"
      data-testid="workflow-header"
    >
      <div className="flex flex-col gap-3">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <h1 className="min-w-0 truncate text-lg font-semibold text-foreground">
              {props.item.title}
            </h1>
            <StatusPill status={props.item.status} />
          </div>
          <div className="mt-1 grid gap-1.5 text-xs text-muted-foreground sm:flex sm:flex-wrap sm:gap-2">
            <BadgeText>{props.item.mode}</BadgeText>
            <BadgeText>{props.item.workflow_id}</BadgeText>
            {props.item.started_at ? (
              <BadgeText>start {formatTime(props.item.started_at)}</BadgeText>
            ) : null}
            {props.item.finished_at ? (
              <BadgeText>end {formatTime(props.item.finished_at)}</BadgeText>
            ) : null}
          </div>
        </div>
        <div className="grid w-full grid-cols-1 gap-2 sm:grid-cols-3">
          <Metric label="reports" value={props.item.report_count} />
          <Metric label="records" value={props.usage.records.length} />
          <Metric label="tokens" value={formatNumber(totalTokens)} />
        </div>
      </div>
      <UsageTable usage={props.usage} compact />
    </section>
  );
}

function ThreadUsagePanel(props: { usage: unknown; loading: boolean }) {
  if (props.loading) {
    return <LoadingBlock label="加载 thread token 用量" muted />;
  }
  if (props.usage == null) return null;
  const total = extractUsageTotal(props.usage);
  return (
    <section className="obsidian-panel-soft min-w-0 rounded-2xl p-4">
      <SectionTitle
        icon={<BarChart3 className="h-4 w-4" />}
        title="Thread Usage"
      />
      <div className="mt-3 grid gap-3 lg:grid-cols-[16rem_minmax(0,1fr)]">
        <Metric
          label="thread tokens"
          value={total == null ? "-" : formatNumber(total)}
        />
        <JsonBlock value={props.usage} />
      </div>
    </section>
  );
}

function FlowGraph(props: {
  nodes: WorkflowFlowNodeDTO[];
  edges: WorkflowFlowEdgeDTO[];
}) {
  return (
    <section
      className="obsidian-panel-soft rounded-2xl p-4"
      data-testid="workflow-flow"
    >
      <SectionTitle icon={<Route className="h-4 w-4" />} title="流程" />
      <div className="mt-3 overflow-x-auto pb-2">
        <div className="flex min-w-max items-stretch gap-2">
          {props.nodes.map((node, index) => (
            <div key={node.id} className="flex items-center gap-2">
              <div className="min-h-28 w-48 rounded-xl border border-border/70 bg-card/80 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                    {node.kind}
                  </span>
                  {node.status ? <StatusPill status={node.status} /> : null}
                </div>
                <div className="mt-3 line-clamp-3 text-sm font-medium text-foreground">
                  {node.label}
                </div>
                <div className="mt-2 line-clamp-2 text-xs text-muted-foreground">
                  {metadataSummary(node.metadata)}
                </div>
              </div>
              {index < props.nodes.length - 1 ? (
                <div className="flex h-px w-8 shrink-0 bg-border" />
              ) : null}
            </div>
          ))}
        </div>
      </div>
      {props.edges.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {props.edges.slice(0, 18).map((edge) => (
            <BadgeText key={edge.id}>
              {edge.source} → {edge.target}
            </BadgeText>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function ModePanels(props: { panels: WorkflowPanelDTO[] }) {
  if (props.panels.length === 0) return null;
  return (
    <section className="grid gap-3 xl:grid-cols-2">
      {props.panels.map((panel) => (
        <PanelCard key={panel.panel_id} panel={panel} />
      ))}
    </section>
  );
}

function PanelCard({ panel }: { panel: WorkflowPanelDTO }) {
  const isReviewBoard = panel.kind === "review_board";
  return (
    <div
      className={cn(
        "obsidian-panel-soft min-w-0 rounded-2xl p-4",
        isReviewBoard ? "xl:col-span-2" : "",
      )}
    >
      <SectionTitle
        icon={<Boxes className="h-4 w-4" />}
        title={panel.title}
        description={
          isReviewBoard
            ? "圆桌评审证据板，按上下文、来源、主张、反驳、共识和最终报告分块查看。"
            : undefined
        }
      />
      {panel.available ? (
        <div className="mt-3">{renderPanelPayload(panel)}</div>
      ) : (
        <div className="mt-3 rounded-xl border border-dashed border-border/70 px-3 py-4 text-sm text-muted-foreground">
          {panel.missing_reason ?? "panel empty"}
        </div>
      )}
    </div>
  );
}

function renderPanelPayload(panel: WorkflowPanelDTO): ReactNode {
  if (panel.kind === "review_board") {
    return <ReviewBoardPanel panel={panel} />;
  }
  if (panel.kind === "map_reduce") {
    return <JsonBlock value={panel.payload} />;
  }
  if (panel.kind === "table") {
    return <GenericTable payload={panel.payload} />;
  }
  if (panel.kind === "markdown") {
    return <TextBlock text={stringValue(panel.payload.content)} />;
  }
  return <JsonBlock value={panel.payload} />;
}

function ReviewBoardPanel({ panel }: { panel: WorkflowPanelDTO }) {
  const claims = arrayValue(panel.payload.claims);
  const rebuttals = arrayValue(panel.payload.rebuttals);
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3">
        <Metric label="claims" value={numberValue(panel.payload.claim_count)} />
        <Metric
          label="rebuttals"
          value={numberValue(panel.payload.rebuttal_count)}
        />
        <Metric label="topic" value={stringValue(panel.payload.topic) || "-"} />
      </div>
      <ReviewBoardBlock
        title="Context"
        description="评审任务输入、角色约束和证据绑定规则。"
        defaultOpen
        scroll
      >
        <TextBlock text={stringValue(panel.payload.context)} />
      </ReviewBoardBlock>
      <ReviewBoardBlock
        title="Sources"
        description="本次评审使用的源文件、映射路径和材料规模。"
        scroll
      >
        <TextBlock text={stringValue(panel.payload.sources)} />
      </ReviewBoardBlock>
      <ReviewBoardBlock
        title="Claims"
        description="子 agent 提出的结构化主张、证据、风险和建议，逐条来自 claims.jsonl。"
        count={claims.length}
        defaultOpen
      >
        <RecordList records={claims} testId="workflow-claims-list" />
      </ReviewBoardBlock>
      <ReviewBoardBlock
        title="Rebuttals"
        description="交叉质询阶段的评论、反驳和证据追问，逐条来自 rebuttals.jsonl。"
        count={rebuttals.length}
      >
        <RecordList records={rebuttals} testId="workflow-rebuttals-list" />
      </ReviewBoardBlock>
      <ReviewBoardBlock
        title="Consensus"
        description="评审汇总后的共识、分歧和需要保留的不确定性。"
        scroll
      >
        <TextBlock text={stringValue(panel.payload.consensus)} />
      </ReviewBoardBlock>
      <ReviewBoardBlock
        title="Final Report"
        description="仲裁 agent 输出的最终结论与可交付报告。"
        scroll
      >
        <TextBlock text={stringValue(panel.payload.final_report)} />
      </ReviewBoardBlock>
    </div>
  );
}

function SubAgentConversationSection(props: {
  reports: SubAgentReportSummaryDTO[];
  selectedTaskRunId: string | null;
  selectedReport: SubAgentReportSummaryDTO | undefined;
  conversation: ConversationDTO | undefined;
  loading: boolean;
  onSelect: (taskRunId: string) => void;
}) {
  return (
    <section
      className="obsidian-panel-soft rounded-2xl p-4"
      data-testid="workflow-conversation-section"
    >
      <SectionTitle
        icon={<MessageSquareText className="h-4 w-4" />}
        title="子 agent 详情"
      />
      <div className="mt-3 grid min-h-[26rem] gap-3 xl:grid-cols-[18rem_minmax(0,1fr)]">
        <div className="space-y-2">
          {props.reports.map((report) => (
            <button
              key={report.task_run_id}
              type="button"
              onClick={() => props.onSelect(report.task_run_id)}
              className={cn(
                "w-full rounded-xl border p-3 text-left transition-colors",
                props.selectedTaskRunId === report.task_run_id
                  ? "border-accent/40 bg-accent/10"
                  : "border-border/70 bg-card/70 hover:bg-secondary/70",
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium text-foreground">
                  {report.task_name ?? report.task_id ?? report.task_run_id}
                </span>
                <StatusPill status={report.status ?? "unknown"} />
              </div>
              <div className="mt-2 line-clamp-2 text-xs text-muted-foreground">
                {report.summary ?? report.error_message ?? report.task_run_id}
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                <BadgeText>{formatUsage(report.usage)}</BadgeText>
                <BadgeText>{report.activity_events.length} events</BadgeText>
                {report.conversation_available ? (
                  <BadgeText>conversation</BadgeText>
                ) : null}
              </div>
            </button>
          ))}
          {props.reports.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border/70 px-3 py-6 text-center text-sm text-muted-foreground">
              暂无子 agent report
            </div>
          ) : null}
        </div>
        <SubAgentDetailPanel
          report={props.selectedReport}
          conversation={props.conversation}
          loading={props.loading}
        />
      </div>
    </section>
  );
}

function SubAgentDetailPanel(props: {
  report: SubAgentReportSummaryDTO | undefined;
  conversation: ConversationDTO | undefined;
  loading: boolean;
}) {
  if (!props.report) {
    return <LoadingBlock label="选择子 agent" muted />;
  }
  return (
    <div className="min-w-0 space-y-3" data-testid="workflow-subagent-detail">
      <TaskSnapshotPanel report={props.report} />
      <ConversationPanel
        report={props.report}
        conversation={props.conversation}
        loading={props.loading}
      />
      <ActivityTimelinePanel report={props.report} />
    </div>
  );
}

function TaskSnapshotPanel(props: { report: SubAgentReportSummaryDTO }) {
  const snapshot = props.report.snapshot ?? {};
  const rows = [
    ["task", props.report.task_name ?? props.report.task_id ?? props.report.task_run_id],
    ["status", props.report.status ?? "-"],
    ["session", props.report.session_id ?? "-"],
    ["run", props.report.run_id ?? "-"],
    ["workdir", props.report.working_dir ?? stringValue(snapshot.working_dir) ?? "-"],
    ["usage", formatUsage(props.report.usage)],
  ];
  return (
    <div
      className="rounded-xl border border-border/70 bg-background/35"
      data-testid="workflow-task-snapshot"
    >
      <div className="flex items-center gap-2 border-b border-border/70 px-3 py-2 text-sm font-medium text-foreground">
        <ClipboardList className="h-4 w-4 text-primary" />
        Task Snapshot
      </div>
      <div className="grid gap-2 p-3 sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div key={label} className="min-w-0 rounded-lg border border-border/60 bg-card/60 px-3 py-2">
            <div className="text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
              {label}
            </div>
            <div className="mt-1 truncate text-xs text-foreground">{value}</div>
          </div>
        ))}
      </div>
      <div className="px-3 pb-3">
        <details className="group rounded-lg border border-border/60 bg-card/60">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs text-muted-foreground [&::-webkit-details-marker]:hidden">
            <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
            raw snapshot
          </summary>
          <div className="border-t border-border/60 p-2">
            <JsonBlock value={snapshot} />
          </div>
        </details>
      </div>
    </div>
  );
}

function ConversationPanel(props: {
  report: SubAgentReportSummaryDTO;
  conversation: ConversationDTO | undefined;
  loading: boolean;
}) {
  if (props.loading) {
    return <LoadingBlock label="加载完整对话" />;
  }
  if (!props.report.conversation_available) {
    return (
      <div className="rounded-xl border border-dashed border-border/70 p-4 text-sm text-muted-foreground">
        对话日志待补齐：{props.report.conversation_source ?? props.report.task_run_id}
      </div>
    );
  }
  if (!props.conversation) {
    return <LoadingBlock label="等待对话数据" muted />;
  }
  return (
    <div
      className="min-h-0 rounded-xl border border-border/70 bg-background/35"
      data-testid="workflow-conversation-panel"
    >
      <div className="flex items-center gap-2 border-b border-border/70 px-3 py-2 text-sm font-medium text-foreground">
        <MessageSquareText className="h-4 w-4 text-primary" />
        <span className="min-w-0 truncate">
          Conversation · {props.conversation.child_session_id ?? props.report.session_id ?? "session"}
        </span>
      </div>
      <div className="max-h-[42rem] space-y-2 overflow-y-auto p-3 scrollbar-overlay">
        {props.conversation.messages.map((message) => (
          <ConversationMessage key={message.record_index} message={message} />
        ))}
      </div>
    </div>
  );
}

function ActivityTimelinePanel(props: { report: SubAgentReportSummaryDTO }) {
  const events = props.report.activity_events ?? [];
  return (
    <div
      className="rounded-xl border border-border/70 bg-background/35"
      data-testid="workflow-activity-timeline"
    >
      <div className="flex items-center gap-2 border-b border-border/70 px-3 py-2 text-sm font-medium text-foreground">
        <Activity className="h-4 w-4 text-primary" />
        Activity Timeline
      </div>
      <div className="max-h-[28rem] space-y-2 overflow-y-auto p-3 scrollbar-overlay">
        {events.map((event) => (
          <details
            key={event.activity_id}
            className="group rounded-xl border border-border/70 bg-card/70"
          >
            <summary className="flex cursor-pointer list-none items-start gap-2 px-3 py-2 [&::-webkit-details-marker]:hidden">
              <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-foreground">
                    {event.title}
                  </span>
                  {event.status ? <StatusPill status={event.status} /> : null}
                </span>
                <span className="mt-1 block line-clamp-2 text-xs text-muted-foreground">
                  {event.summary ?? event.source_action ?? event.activity_type}
                </span>
              </span>
              <span className="shrink-0 text-[11px] text-muted-foreground">
                {formatTime(event.ts)}
              </span>
            </summary>
            <div className="border-t border-border/60 p-3">
              <JsonBlock value={event.payload} />
            </div>
          </details>
        ))}
        {events.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border/70 px-3 py-6 text-center text-sm text-muted-foreground">
            暂无行为事件
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ConversationMessage({ message }: { message: ConversationMessageDTO }) {
  return (
    <article className="rounded-xl border border-border/60 bg-card/75 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <BadgeText>{message.role}</BadgeText>
          {message.message_type ? <BadgeText>{message.message_type}</BadgeText> : null}
        </div>
        <span className="text-[11px] text-muted-foreground">
          #{message.record_index} {formatTime(message.created_at)}
        </span>
      </div>
      <pre className="mt-2 whitespace-pre-wrap break-words text-xs leading-5 text-foreground">
        {message.content}
      </pre>
      {message.tool_calls.length > 0 || message.usage ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {message.tool_calls.length > 0 ? (
            <BadgeText>{message.tool_calls.length} tool calls</BadgeText>
          ) : null}
          {message.usage ? <BadgeText>{formatUnknownUsage(message.usage)}</BadgeText> : null}
        </div>
      ) : null}
    </article>
  );
}

function ArtifactSection(props: {
  artifacts: WorkflowArtifactRefDTO[];
  artifact: WorkflowArtifactContentDTO | null;
  loading: boolean;
  error: string | null;
  onOpen: (artifactId: string) => void;
  onClose: () => void;
}) {
  return (
    <section
      className="obsidian-panel-soft rounded-2xl p-4"
      data-testid="workflow-artifact-section"
    >
      <SectionTitle icon={<FileText className="h-4 w-4" />} title="产物" />
      <div className="mt-3 grid gap-3 xl:grid-cols-[22rem_minmax(0,1fr)]">
        <div className="max-h-[30rem] space-y-2 overflow-y-auto pr-1 scrollbar-overlay">
          {props.artifacts.map((artifact) => (
            <button
              key={artifact.artifact_id}
              type="button"
              disabled={!artifact.available}
              onClick={() => props.onOpen(artifact.artifact_id)}
              className="w-full rounded-xl border border-border/70 bg-card/70 p-3 text-left text-sm transition-colors hover:bg-secondary/70 disabled:opacity-55"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-medium text-foreground">
                  {artifact.title}
                </span>
                <BadgeText>{artifact.kind}</BadgeText>
              </div>
              <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                {artifact.path}
              </div>
            </button>
          ))}
        </div>
        <ArtifactPreview
          artifact={props.artifact}
          loading={props.loading}
          error={props.error}
          onClose={props.onClose}
        />
      </div>
    </section>
  );
}

function ArtifactPreview(props: {
  artifact: WorkflowArtifactContentDTO | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  if (props.loading) {
    return <LoadingBlock label="加载产物内容" dataTestId="workflow-artifact-loading" />;
  }
  if (props.error) {
    return (
      <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
        {props.error}
      </div>
    );
  }
  if (!props.artifact) {
    return <LoadingBlock label="选择产物查看内容" muted />;
  }
  return (
    <div
      className="rounded-xl border border-border/70 bg-background/35"
      data-testid="workflow-artifact-preview"
    >
      <div className="flex items-center justify-between gap-2 border-b border-border/70 px-3 py-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-foreground">
            {props.artifact.title}
          </div>
          <div className="truncate font-mono text-[11px] text-muted-foreground">
            {props.artifact.path}
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={props.onClose}>
          关闭
        </Button>
      </div>
      <div className="max-h-[34rem] overflow-auto p-3 scrollbar-overlay">
        {typeof props.artifact.content === "string" ? (
          <pre className="whitespace-pre-wrap break-words text-xs leading-5">
            {props.artifact.content}
          </pre>
        ) : (
          <JsonBlock value={props.artifact.content} />
        )}
      </div>
    </div>
  );
}

function TimelineSection({ detail }: { detail: WorkflowDetailDTO }) {
  return (
    <section
      className="obsidian-panel-soft rounded-2xl p-4"
      data-testid="workflow-timeline"
    >
      <SectionTitle
        icon={<Clock3 className="h-4 w-4" />}
        title="Audit Timeline"
        description="workflow 生命周期审计事件，按写入顺序展示，长列表在区域内部滚动。"
      />
      <div
        className="mt-3 max-h-[30rem] space-y-2 overflow-y-auto pr-1 scrollbar-overlay"
        data-testid="workflow-timeline-scroll"
      >
        {detail.timeline.map((event) => (
          <div
            key={event.event_id}
            className="rounded-xl border border-border/70 bg-card/70 px-3 py-2"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm font-medium text-foreground">
                {event.label}
              </span>
              <span className="text-[11px] text-muted-foreground">
                {formatTime(event.timestamp)}
              </span>
            </div>
            <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
              {compactJson(event.payload)}
            </div>
          </div>
        ))}
        {detail.timeline.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border/70 px-3 py-6 text-center text-sm text-muted-foreground">
            暂无审计事件
          </div>
        ) : null}
      </div>
    </section>
  );
}

function UsageTable(props: { usage: WorkflowUsageDTO; compact?: boolean }) {
  const records = props.compact
    ? props.usage.records.slice(0, 5)
    : props.usage.records;
  if (records.length === 0) return null;
  return (
    <>
      <div className="mt-4 space-y-2 md:hidden">
        {records.map((record, index) => (
          <div
            key={`${record.task_run_id ?? record.task_id ?? index}`}
            className="rounded-xl border border-border/70 bg-card/70 p-3 text-xs"
          >
            <div className="break-words font-medium text-foreground">
              {record.task_name ?? record.task_id ?? record.task_run_id ?? "-"}
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <BadgeText>{record.provider}</BadgeText>
              <BadgeText>{record.source}</BadgeText>
              <BadgeText>{formatUsage(record.usage)}</BadgeText>
            </div>
          </div>
        ))}
      </div>
      <div className="mt-4 hidden overflow-x-auto rounded-xl border border-border/70 md:block">
        <table className="w-full min-w-[42rem] table-fixed text-left text-xs">
          <thead className="bg-muted/60 text-muted-foreground">
            <tr>
              <th className="w-[28%] px-3 py-2 font-medium">task</th>
              <th className="w-[18%] px-3 py-2 font-medium">provider</th>
              <th className="w-[18%] px-3 py-2 font-medium">source</th>
              <th className="px-3 py-2 font-medium">usage</th>
            </tr>
          </thead>
          <tbody>
            {records.map((record, index) => (
              <tr
                key={`${record.task_run_id ?? record.task_id ?? index}`}
                className="border-t border-border/60"
              >
                <td className="truncate px-3 py-2">
                  {record.task_name ?? record.task_id ?? record.task_run_id ?? "-"}
                </td>
                <td className="truncate px-3 py-2">{record.provider}</td>
                <td className="truncate px-3 py-2">{record.source}</td>
                <td className="truncate px-3 py-2 font-mono">
                  {formatUsage(record.usage)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Diagnostics({ diagnostics }: { diagnostics: WorkflowDiagnosticDTO[] }) {
  if (diagnostics.length === 0) return null;
  return (
    <section className="rounded-2xl border border-warning/30 bg-warning/5 p-3">
      <div className="text-sm font-medium text-foreground">Diagnostics</div>
      <div className="mt-2 space-y-1">
        {diagnostics.slice(0, 8).map((diagnostic, index) => (
          <div
            key={`${diagnostic.code}-${index}`}
            className="text-xs text-muted-foreground"
          >
            <span className="font-mono text-foreground">{diagnostic.code}</span>
            {" · "}
            {diagnostic.message}
            {diagnostic.path ? ` · ${diagnostic.path}` : ""}
          </div>
        ))}
      </div>
    </section>
  );
}

function SectionTitle(props: {
  icon: ReactNode;
  title: string;
  description?: string;
}) {
  return (
    <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
      <span className="flex h-8 w-8 items-center justify-center rounded-xl border border-border/70 bg-card/80 text-primary">
        {props.icon}
      </span>
      <span className="min-w-0">
        <span className="block truncate">{props.title}</span>
        {props.description ? (
          <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
            {props.description}
          </span>
        ) : null}
      </span>
    </div>
  );
}

function Metric(props: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-xl border border-border/60 bg-card/75 px-3 py-2">
      <div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
        {props.label}
      </div>
      <div className="mt-1 truncate text-sm font-semibold text-foreground">
        {props.value}
      </div>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const normalized = status.toLowerCase();
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium",
        normalized === "completed" || normalized === "done"
          ? "bg-success/12 text-success"
          : normalized === "failed" || normalized === "error"
            ? "bg-destructive/12 text-destructive"
            : "bg-muted text-muted-foreground",
      )}
    >
      {status}
    </span>
  );
}

function BadgeText({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex min-w-0 max-w-full items-center rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
      <span className="truncate">{children}</span>
    </span>
  );
}

function TextBlock(props: { title?: string; text: string }) {
  if (!props.text) return null;
  return (
    <div className="rounded-xl border border-border/70 bg-background/35 p-3">
      {props.title ? (
        <div className="mb-2 text-xs font-medium text-muted-foreground">
          {props.title}
        </div>
      ) : null}
      <pre className="whitespace-pre-wrap break-words text-xs leading-5 text-foreground">
        {props.text}
      </pre>
    </div>
  );
}

function ReviewBoardBlock(props: {
  title: string;
  description: string;
  children: ReactNode;
  count?: number;
  defaultOpen?: boolean;
  scroll?: boolean;
}) {
  return (
    <details
      className="group overflow-hidden rounded-xl border border-border/70 bg-background/35"
      open={props.defaultOpen}
    >
      <summary className="flex cursor-pointer list-none items-center gap-3 px-3 py-3 transition-colors hover:bg-secondary/55 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-foreground">
            {props.title}
          </span>
          <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">
            {props.description}
          </span>
        </span>
        {typeof props.count === "number" ? (
          <BadgeText>{props.count} 条</BadgeText>
        ) : null}
      </summary>
      <div className="border-t border-border/70 p-3">
        <div
          className={cn(
            props.scroll ? "max-h-[28rem] overflow-y-auto pr-1 scrollbar-overlay" : "",
          )}
        >
          {props.children}
        </div>
      </div>
    </details>
  );
}

function RecordList(props: { records: unknown[]; testId?: string }) {
  if (props.records.length === 0) {
    return (
      <div
        className="rounded-xl border border-dashed border-border/80 px-3 py-4 text-xs text-muted-foreground"
        data-testid={props.testId}
      >
        暂无结构化记录
      </div>
    );
  }
  return (
    <div
      className="max-h-[28rem] space-y-2 overflow-y-auto pr-1 scrollbar-overlay"
      data-testid={props.testId}
    >
      {props.records.map((record, index) => (
        <article
          key={index}
          className="rounded-xl border border-border/70 bg-card/70 p-3 text-xs leading-5 text-foreground"
        >
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium text-foreground">
              {recordTitle(record, index)}
            </span>
            <div className="flex flex-wrap gap-1.5">
              {recordBadges(record).map((badge) => (
                <BadgeText key={badge}>{badge}</BadgeText>
              ))}
            </div>
          </div>
          <pre
            className="whitespace-pre-wrap break-words text-xs leading-5 text-foreground"
          >
            {prettyJson(record)}
          </pre>
        </article>
      ))}
    </div>
  );
}

function recordTitle(record: unknown, index: number): string {
  const object = plainObject(record);
  const claimId = stringValue(object?.claim_id);
  const commentId = stringValue(object?.comment_id);
  const id = claimId || commentId;
  return id ? `#${index + 1} · ${id}` : `#${index + 1}`;
}

function recordBadges(record: unknown): string[] {
  const object = plainObject(record);
  if (!object) return [];
  return [
    stringValue(object.agent),
    stringValue(object.severity),
    stringValue(object.confidence),
  ].filter(Boolean);
}

function plainObject(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function prettyJson(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function GenericTable({ payload }: { payload: Record<string, unknown> }) {
  const rows =
    arrayValue(payload.tasks).length > 0
      ? arrayValue(payload.tasks)
      : arrayValue(payload.artifacts);
  if (rows.length === 0) return <JsonBlock value={payload} />;
  const objects = rows.filter(isRecord);
  const keys = Array.from(
    new Set(objects.flatMap((row) => Object.keys(row).slice(0, 6))),
  ).slice(0, 5);
  if (objects.length === 0 || keys.length === 0) return <JsonBlock value={rows} />;
  return (
    <div className="overflow-x-auto rounded-xl border border-border/70">
      <table className="min-w-full text-left text-xs">
        <thead className="bg-muted/60 text-muted-foreground">
          <tr>
            {keys.map((key) => (
              <th key={key} className="px-3 py-2 font-medium">
                {key}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {objects.slice(0, 10).map((row, index) => (
            <tr key={index} className="border-t border-border/60">
              {keys.map((key) => (
                <td key={key} className="max-w-64 truncate px-3 py-2">
                  {displayValue(row[key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-[32rem] w-full max-w-full overflow-auto rounded-xl border border-border/70 bg-background/45 p-3 text-xs leading-5 scrollbar-overlay">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function LoadingBlock(props: {
  label: string;
  muted?: boolean;
  dataTestId?: string;
}) {
  return (
    <div
      className={cn(
        "flex min-h-44 items-center justify-center rounded-xl border border-dashed border-border/70 px-3 text-center text-sm",
        props.muted ? "text-muted-foreground" : "text-foreground",
      )}
      data-testid={props.dataTestId}
    >
      {props.label}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex min-h-[32rem] flex-col items-center justify-center rounded-2xl border border-dashed border-border/70 text-center">
      <Split className="mb-3 h-8 w-8 text-muted-foreground" />
      <div className="text-sm font-medium text-foreground">选择一个 workflow</div>
      <div className="mt-1 text-xs text-muted-foreground">
        左侧列表展示当前 thread 的全部 workflow run
      </div>
    </div>
  );
}

function formatUsage(usage: Record<string, number>): string {
  const total = tokenTotal(usage);
  if (total > 0) return `${formatNumber(total)} tokens`;
  const first = Object.entries(usage)[0];
  return first ? `${first[0]} ${formatNumber(first[1])}` : "0 tokens";
}

function formatUnknownUsage(usage: Record<string, unknown>): string {
  const numeric: Record<string, number> = {};
  for (const [key, value] of Object.entries(usage)) {
    if (typeof value === "number" && Number.isFinite(value)) numeric[key] = value;
  }
  return formatUsage(numeric);
}

function tokenTotal(usage: Record<string, number>): number {
  if (typeof usage.total_tokens === "number") return usage.total_tokens;
  return Object.entries(usage)
    .filter(([key]) => key.endsWith("_tokens") || key.endsWith("_token_count"))
    .reduce((sum, [, value]) => sum + value, 0);
}

function extractUsageTotal(value: unknown): number | null {
  if (!isRecord(value)) return null;
  const direct = value.total_tokens ?? value.totalTokens;
  if (typeof direct === "number" && Number.isFinite(direct)) return direct;
  const totals = value.totals;
  if (isRecord(totals)) {
    const total = totals.total_tokens ?? totals.totalTokens;
    if (typeof total === "number" && Number.isFinite(total)) return total;
  }
  for (const nested of Object.values(value)) {
    const total = extractUsageTotal(nested);
    if (total != null) return total;
  }
  return null;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function formatTime(value: string | number | null | undefined): string {
  if (value == null || value === "") return "-";
  const date =
    typeof value === "number"
      ? new Date(value > 10_000_000_000 ? value : value * 1000)
      : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function metadataSummary(value: Record<string, unknown>): string {
  if (!value || Object.keys(value).length === 0) return "";
  if (typeof value.summary === "string") return value.summary;
  return compactJson(value);
}

function compactJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number | string {
  return typeof value === "number" && Number.isFinite(value) ? value : "-";
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function displayValue(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return compactJson(value);
}
