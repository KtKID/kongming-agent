import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Composer, type ReasoningEffort } from "@/components/Composer";
import { ModelSwitcher } from "@/components/ModelSwitcher";
import {
  ThreadProjectSelector,
  noneProjectOption,
  type ThreadProjectOption,
} from "@/components/ThreadProjectSelector";
import { useModelProvidersStore } from "@/modules/model-providers/store";
import type { ThreadMetadataDTO } from "@/protocol";
import { useThreadsStore } from "@/stores/threads";

export function GenericEmptyThreadView({
  onCreated,
}: {
  onCreated: (
    thread: ThreadMetadataDTO,
    reasoningEffort: ReasoningEffort | null,
  ) => void;
}) {
  const threads = useThreadsStore((s) => s.threads);
  const createGenericThreadFromFirstMessage = useThreadsStore(
    (s) => s.createGenericThreadFromFirstMessage,
  );
  const pendingNewSession = useThreadsStore((s) => s.pendingNewSession);
  const modelFamilies = useModelProvidersStore((s) => s.modelFamilies);
  const loadModelFamilies = useModelProvidersStore((s) => s.loadModelFamilies);
  const [selectedProject, setSelectedProject] = useState<ThreadProjectOption | null>(
    pendingNewSession?.cwd
      ? {
          cwd: pendingNewSession.cwd,
          label: pendingNewSession.projectName || pendingNewSession.cwd,
          title: pendingNewSession.cwd,
          threadCount: 0,
          source: "file_picker",
        }
      : null,
  );
  const [selectedPresetId, setSelectedPresetId] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    void loadModelFamilies();
  }, [loadModelFamilies]);

  useEffect(() => {
    if (selectedPresetId) return;
    const firstPresetId = modelFamilies.find((family) => family.presetId)?.presetId;
    if (firstPresetId) setSelectedPresetId(firstPresetId);
  }, [modelFamilies, selectedPresetId]);

  const selectedFamily = useMemo(
    () => modelFamilies.find((family) => family.presetId === selectedPresetId),
    [modelFamilies, selectedPresetId],
  );

  const submitFirstMessage = async (
    text: string,
    reasoningEffort: ReasoningEffort | null,
  ) => {
    const presetId = selectedPresetId.trim();
    if (!presetId) {
      toast.error("请选择模型");
      return false;
    }
    setSubmitting(true);
    try {
      const project = selectedProject?.source === "none" ? noneProjectOption() : selectedProject;
      const thread = await createGenericThreadFromFirstMessage({
        text,
        preset_id: presetId,
        cwd: project?.cwd ?? "",
        reasoning_effort: reasoningEffort,
      });
      onCreated(thread, reasoningEffort);
      return true;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`创建会话失败：${message}`);
      return false;
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="flex h-full min-h-0 flex-col bg-background/10"
      data-testid="generic-empty-thread-view"
    >
      <div className="flex min-h-0 flex-1 items-center justify-center px-4">
        <div className="w-full max-w-3xl">
          <Composer
            disabled={submitting}
            onSubmit={submitFirstMessage}
            leftActions={
              <ThreadProjectSelector
                threads={threads}
                value={selectedProject}
                disabled={submitting}
                onChange={setSelectedProject}
              />
            }
            modelSwitcher={
              <ModelSwitcher
                currentPresetId={selectedPresetId || selectedFamily?.presetId}
                options={modelFamilies}
                disabled={submitting}
                onSelect={setSelectedPresetId}
                onManageProviders={() => undefined}
              />
            }
            reasoningOptions={selectedFamily?.supportedReasoningEfforts}
            defaultReasoningEffort={selectedFamily?.defaultReasoningEffort}
            reasoningSelectionKey={selectedFamily?.presetId ?? selectedPresetId}
          />
        </div>
      </div>
    </div>
  );
}
