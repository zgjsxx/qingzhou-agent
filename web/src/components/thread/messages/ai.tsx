import { parsePartialJson } from "@langchain/core/output_parsers";
import { useStreamContext } from "@/providers/Stream";
import { AIMessage, Checkpoint, Message } from "@langchain/langgraph-sdk";
import { useStream } from "@langchain/langgraph-sdk/react";
import { getContentString } from "../utils";
import { BranchSwitcher, CommandBar } from "./shared";
import { MarkdownText } from "../markdown-text";
import { LoadExternalComponent } from "@langchain/langgraph-sdk/react-ui";
import { cn } from "@/lib/utils";
import { ToolCalls, ToolResult } from "./tool-calls";
import { MessageContentComplex } from "@langchain/core/messages";
import { Fragment, memo, useMemo } from "react";
import { isAgentInboxInterruptSchema } from "@/lib/agent-inbox-interrupt";
import { ThreadView } from "../agent-inbox";
import { GenericInterruptView } from "./generic-interrupt";
import { useArtifact } from "../artifact-hooks";

const AUDIO_MARKER_REGEX = /\[\[qingzhou-audio:({.*?})\]\]/g;
const VOICE_REPLY_WRAPPER_REGEX =
  /(?:^|\n{2,})(?:让我(?:来|为你|帮你)?(?:合成|生成|准备|制作)?语音回复|语音回复|Voice reply)\s*[：:]\s*/i;

interface VoiceReplyAudio {
  url: string;
  filename?: string;
  voice?: string;
}

function normalizeVoiceReplyText(text: string) {
  return text.replace(/\s+/g, "").trim();
}

function stripDuplicateVoiceReplyText(text: string) {
  const match = VOICE_REPLY_WRAPPER_REGEX.exec(text);
  if (!match || match.index <= 0) {
    return text;
  }

  const before = text.slice(0, match.index).trim();
  const after = text.slice(match.index + match[0].length).trim();
  if (!before || !after) {
    return text;
  }

  const normalizedBefore = normalizeVoiceReplyText(before);
  const normalizedAfter = normalizeVoiceReplyText(after);
  if (
    normalizedBefore &&
    (normalizedBefore === normalizedAfter ||
      normalizedAfter.startsWith(normalizedBefore) ||
      normalizedBefore.startsWith(normalizedAfter))
  ) {
    return before;
  }

  return text;
}

function parseVoiceReplyAudio(content: string): {
  text: string;
  audioItems: VoiceReplyAudio[];
} {
  const audioItems: VoiceReplyAudio[] = [];
  let text = content
    .replace(AUDIO_MARKER_REGEX, (_match, payloadText: string) => {
      try {
        const payload = JSON.parse(payloadText) as Record<string, unknown>;
        const url = typeof payload.url === "string" ? payload.url : "";
        if (url.startsWith("/api/local/downloads/")) {
          audioItems.push({
            url,
            filename:
              typeof payload.filename === "string" ? payload.filename : undefined,
            voice: typeof payload.voice === "string" ? payload.voice : undefined,
          });
        }
      } catch {
        return "";
      }
      return "";
    })
    .trim();

  if (audioItems.length > 0) {
    text = stripDuplicateVoiceReplyText(text).trim();
  }

  return { text, audioItems };
}

function VoiceReplyPlayer({ audio }: { audio: VoiceReplyAudio }) {
  return (
    <div className="border-border bg-muted/30 mt-2 max-w-xl rounded-lg border px-3 py-2">
      <audio
        className="w-full"
        controls
        autoPlay
        preload="metadata"
        src={audio.url}
      />
      {(audio.filename || audio.voice) && (
        <div className="text-muted-foreground mt-1 truncate text-xs">
          {[audio.filename, audio.voice].filter(Boolean).join(" · ")}
        </div>
      )}
    </div>
  );
}

function CustomComponent({ message }: { message: Message }) {
  const artifact = useArtifact();
  const thread = useStreamContext();
  const { values } = thread;
  const customComponents = values.ui?.filter(
    (ui) => ui.metadata?.message_id === message.id,
  );

  if (!customComponents?.length) return null;
  return (
    <Fragment key={message.id}>
      {customComponents.map((customComponent) => (
        <LoadExternalComponent
          key={customComponent.id}
          stream={thread as unknown as ReturnType<typeof useStream>}
          message={customComponent}
          meta={{ ui: customComponent, artifact }}
        />
      ))}
    </Fragment>
  );
}

function parseAnthropicStreamedToolCalls(
  content: MessageContentComplex[],
): AIMessage["tool_calls"] {
  const toolCallContents = content.filter((c) => c.type === "tool_use" && c.id);

  return toolCallContents.map((tc) => {
    const toolCall = tc as Record<string, any>;
    let json: Record<string, any> = {};
    if (toolCall?.input) {
      try {
        json = parsePartialJson(toolCall.input) ?? {};
      } catch {
        // Pass
      }
    }
    return {
      name: toolCall.name ?? "",
      id: toolCall.id ?? "",
      args: json,
      type: "tool_call",
    };
  });
}

interface InterruptProps {
  interrupt?: unknown;
  isLastMessage: boolean;
  hasNoAIOrToolMessages: boolean;
}

function Interrupt({
  interrupt,
  isLastMessage,
  hasNoAIOrToolMessages,
}: InterruptProps) {
  const fallbackValue = Array.isArray(interrupt)
    ? (interrupt as Record<string, any>[])
    : (((interrupt as { value?: unknown } | undefined)?.value ??
        interrupt) as Record<string, any>);

  return (
    <>
      {isAgentInboxInterruptSchema(interrupt) &&
        (isLastMessage || hasNoAIOrToolMessages) && (
          <ThreadView interrupt={interrupt} />
        )}
      {interrupt &&
      !isAgentInboxInterruptSchema(interrupt) &&
      (isLastMessage || hasNoAIOrToolMessages) ? (
        <GenericInterruptView interrupt={fallbackValue} />
      ) : null}
    </>
  );
}

interface AssistantMessageProps {
  message: Message | undefined;
  isLoading: boolean;
  handleRegenerate: (parentCheckpoint: Checkpoint | null | undefined) => void;
  isLastMessage: boolean;
  hasNoAIOrToolMessages: boolean;
  threadInterrupt: unknown;
  hideToolCalls: boolean;
  parentCheckpoint: Checkpoint | null | undefined;
  branch: string | undefined;
  branchOptions: string[] | undefined;
  onSetBranch: (branch: string) => void;
}

function areAssistantMessagePropsEqual(
  prev: AssistantMessageProps,
  next: AssistantMessageProps,
): boolean {
  // The last message during streaming always re-renders —
  // content/tool_calls change per token, memo comparison would miss updates.
  if (next.isLastMessage && next.isLoading) return false;

  if (prev.isLoading !== next.isLoading) return false;
  if (prev.isLastMessage !== next.isLastMessage) return false;
  if (prev.hasNoAIOrToolMessages !== next.hasNoAIOrToolMessages) return false;
  if (prev.hideToolCalls !== next.hideToolCalls) return false;
  if (prev.threadInterrupt !== next.threadInterrupt) return false;
  if (prev.parentCheckpoint !== next.parentCheckpoint) return false;
  if (prev.branch !== next.branch) return false;
  if (prev.branchOptions !== next.branchOptions) return false;

  // Skip function props — handleRegenerate and onSetBranch change reference
  // but have stable behavior (they always operate on the same StreamManager)

  // Compare message content
  if (prev.message === next.message) return true;
  if (!prev.message && !next.message) return true;
  if (!prev.message || !next.message) return false;

  if (prev.message.id !== next.message.id) return false;
  if (prev.message.type !== next.message.type) return false;
  if (getContentString(prev.message.content) !== getContentString(next.message.content))
    return false;

  // For tool messages
  if (prev.message.type === "tool") {
    if ((prev.message as any).name !== (next.message as any).name) return false;
    if ((prev.message as any).tool_call_id !== (next.message as any).tool_call_id)
      return false;
  }

  return true;
}

export const AssistantMessage = memo(function AssistantMessage({
  message,
  isLoading,
  handleRegenerate,
  isLastMessage,
  hasNoAIOrToolMessages,
  threadInterrupt,
  hideToolCalls,
  parentCheckpoint,
  branch,
  branchOptions,
  onSetBranch,
}: AssistantMessageProps) {
  const content = message?.content ?? [];
  const contentString = getContentString(content);
  const { text: displayContent, audioItems } = useMemo(
    () => parseVoiceReplyAudio(contentString),
    [contentString],
  );
  const anthropicStreamedToolCalls = Array.isArray(content)
    ? parseAnthropicStreamedToolCalls(content)
    : undefined;

  const hasToolCalls =
    message &&
    "tool_calls" in message &&
    message.tool_calls &&
    message.tool_calls.length > 0;
  const hasVoiceReplyToolCall =
    hasToolCalls &&
    message.tool_calls?.some((tc) => tc.name === "synthesize_speech_reply");
  const toolCallsHaveContents =
    hasToolCalls &&
    message.tool_calls?.some(
      (tc) => tc.args && Object.keys(tc.args).length > 0,
    );
  const hasAnthropicToolCalls = !!anthropicStreamedToolCalls?.length;
  const isToolResult = message?.type === "tool";
  const renderedDisplayContent = hasVoiceReplyToolCall ? "" : displayContent;

  if (isToolResult && hideToolCalls) {
    return null;
  }

  return (
    <div className="group mr-auto flex w-full items-start gap-2">
      <div className="flex w-full flex-col gap-2">
        {isToolResult ? (
          <>
            <ToolResult message={message} />
            <Interrupt
              interrupt={threadInterrupt}
              isLastMessage={isLastMessage}
              hasNoAIOrToolMessages={hasNoAIOrToolMessages}
            />
          </>
        ) : (
          <>
            {renderedDisplayContent.length > 0 && (
              <div className="py-1">
                <MarkdownText>{renderedDisplayContent}</MarkdownText>
              </div>
            )}

            {audioItems.map((audio) => (
              <VoiceReplyPlayer key={audio.url} audio={audio} />
            ))}

            {!hideToolCalls && (
              <>
                {(hasToolCalls && toolCallsHaveContents && (
                  <ToolCalls toolCalls={message.tool_calls} />
                )) ||
                  (hasAnthropicToolCalls && (
                    <ToolCalls toolCalls={anthropicStreamedToolCalls} />
                  )) ||
                  (hasToolCalls && (
                    <ToolCalls toolCalls={message.tool_calls} />
                  ))}
              </>
            )}

            {message && (
              <CustomComponent
                message={message}
              />
            )}
            <Interrupt
              interrupt={threadInterrupt}
              isLastMessage={isLastMessage}
              hasNoAIOrToolMessages={hasNoAIOrToolMessages}
            />
            <div className="relative h-0 overflow-visible">
              <div
                className={cn(
                  "absolute top-1 left-0 z-10 mr-auto flex items-center gap-2 transition-opacity",
                  "opacity-0 group-focus-within:opacity-100 group-hover:opacity-100",
                )}
              >
                <BranchSwitcher
                  branch={branch}
                  branchOptions={branchOptions}
                  onSelect={onSetBranch}
                  isLoading={isLoading}
                />
                <CommandBar
                  content={renderedDisplayContent}
                  isLoading={isLoading}
                  isAiMessage={true}
                  handleRegenerate={() => handleRegenerate(parentCheckpoint)}
                />
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}, areAssistantMessagePropsEqual);

export function AssistantMessageLoading() {
  return (
    <div className="mr-auto flex items-start gap-2">
      <div className="bg-muted flex h-8 items-center gap-1 rounded-2xl px-4 py-2">
        <div className="bg-foreground/50 h-1.5 w-1.5 animate-[pulse_1.5s_ease-in-out_infinite] rounded-full"></div>
        <div className="bg-foreground/50 h-1.5 w-1.5 animate-[pulse_1.5s_ease-in-out_0.5s_infinite] rounded-full"></div>
        <div className="bg-foreground/50 h-1.5 w-1.5 animate-[pulse_1.5s_ease-in-out_1s_infinite] rounded-full"></div>
      </div>
    </div>
  );
}
