import type {
  ConversationReferenceDTO,
  ConversationReferenceTemplate,
} from "@/protocol";

export class ConversationReferenceManager {
  static createFromTemplate(
    template: ConversationReferenceTemplate,
    sourceId: string,
  ): ConversationReferenceDTO {
    return {
      ...template,
      args: template.args ?? {},
      metadata: {
        ...(template.metadata ?? {}),
        slash_item_id: sourceId,
      },
      id: `${template.kind}:${template.ref}:${Date.now().toString(36)}:${Math.floor(
        Math.random() * 1e6,
      ).toString(36)}`,
    };
  }

  static hasSameReference(
    refs: ConversationReferenceDTO[],
    next: ConversationReferenceDTO,
  ): boolean {
    return refs.some(
      (item) =>
        item.kind === next.kind &&
        item.ref === next.ref &&
        item.activation === next.activation,
    );
  }

  static toClipboardText(reference: ConversationReferenceDTO): string {
    if (this.isPromptInjectedSkill(reference)) {
      return this.toMessageText(reference);
    }
    if (this.isPromptInjectedWorkflow(reference)) {
      return this.toWorkflowMessageText(reference);
    }
    return reference.source_ref
      ? `[${reference.ref}](${reference.source_ref})`
      : reference.ref;
  }

  static isPromptInjectedSkill(reference: ConversationReferenceDTO): boolean {
    return reference.kind === "skill" && reference.activation === "inject_context";
  }

  static isPromptInjectedWorkflow(reference: ConversationReferenceDTO): boolean {
    return (
      reference.kind === "workflow_strategy" &&
      (reference.activation === "start_workflow" ||
        reference.activation === "guide_payload")
    );
  }

  static isPromptInjectedReference(reference: ConversationReferenceDTO): boolean {
    return (
      this.isPromptInjectedSkill(reference) ||
      this.isPromptInjectedWorkflow(reference)
    );
  }

  static toMessageText(reference: ConversationReferenceDTO): string {
    const skillName =
      typeof reference.metadata?.name === "string" && reference.metadata.name.trim()
        ? reference.metadata.name.trim()
        : reference.ref.startsWith("skill:")
          ? reference.ref.slice("skill:".length)
          : reference.label;
    const path =
      typeof reference.source_ref === "string" && reference.source_ref.trim()
        ? reference.source_ref.trim()
        : typeof reference.metadata?.body_path === "string"
          ? reference.metadata.body_path
          : reference.ref;
    return `[$${skillName}](${path})`;
  }

  static toWorkflowMessageText(reference: ConversationReferenceDTO): string {
    const mode = this.workflowMode(reference);
    return `必须使用 ${mode} workflow 完成用户需求或任务`;
  }

  static workflowMode(reference: ConversationReferenceDTO): string {
    const modeFromMetadata = reference.metadata?.mode;
    if (typeof modeFromMetadata === "string" && modeFromMetadata.trim()) {
      return modeFromMetadata.trim();
    }
    if (reference.ref.startsWith("workflow_strategy:")) {
      const mode = reference.ref.slice("workflow_strategy:".length).trim();
      if (mode) return mode;
    }
    return reference.label.trim() || "workflow";
  }

  static toPromptInjectedText(reference: ConversationReferenceDTO): string {
    if (this.isPromptInjectedWorkflow(reference)) {
      return this.toWorkflowMessageText(reference);
    }
    if (this.isPromptInjectedSkill(reference)) {
      return this.toMessageText(reference);
    }
    return "";
  }

  static prependPromptInjectedSkills(
    text: string,
    refs: ConversationReferenceDTO[],
  ): string {
    return this.prependPromptInjectedReferences(text, refs);
  }

  static prependPromptInjectedReferences(
    text: string,
    refs: ConversationReferenceDTO[],
  ): string {
    const prefix = refs
      .filter((reference) => this.isPromptInjectedReference(reference))
      .map((reference) => this.toPromptInjectedText(reference))
      .filter((line) => line.trim())
      .join("\n");
    if (!prefix) return text;
    return text ? `${prefix}\n\n${text}` : prefix;
  }

  static passthroughReferences(
    refs: ConversationReferenceDTO[],
  ): ConversationReferenceDTO[] {
    return refs.filter((reference) => !this.isPromptInjectedReference(reference));
  }
}
