import { v4 as uuidv4 } from "uuid";
import { ReactNode, useCallback, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import { useStreamContext } from "@/providers/Stream";
import { useState, FormEvent } from "react";
import { Button } from "../ui/button";
import { Checkpoint, Message } from "@langchain/langgraph-sdk";
import { AssistantMessage, AssistantMessageLoading } from "./messages/ai";
import { HumanMessage } from "./messages/human";
import {
  DO_NOT_RENDER_ID_PREFIX,
  ensureToolCallsHaveResponses,
} from "@/lib/ensure-tool-responses";
import { QingzhouLogo } from "../icons/qingzhou";
import { TooltipIconButton } from "./tooltip-icon-button";
import {
  ArrowDown,
  LoaderCircle,
  Mic,
  PanelRightOpen,
  PanelRightClose,
  SquarePen,
  Square,
  XIcon,
  Plus,
} from "lucide-react";
import { useQueryState, parseAsBoolean } from "nuqs";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";
import ThreadHistory from "./history";
import { toast } from "sonner";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { useFileUpload } from "@/hooks/use-file-upload";
import { uploadedFileBlockToText } from "@/lib/multimodal-utils";
import { ContentBlocksPreview } from "./ContentBlocksPreview";
import {
  ArtifactContent,
  ArtifactTitle,
} from "./artifact";
import { useArtifactContext, useArtifactOpen } from "./artifact-hooks";
import { getContentString } from "./utils";

function StickyToBottomContent(props: {
  content: ReactNode;
  footer?: ReactNode;
  className?: string;
  contentClassName?: string;
}) {
  const context = useStickToBottomContext();
  return (
    <div
      ref={context.scrollRef}
      style={{ width: "100%", height: "100%" }}
      className={props.className}
    >
      <div
        ref={context.contentRef}
        className={props.contentClassName}
      >
        {props.content}
      </div>

      {props.footer}
    </div>
  );
}

function ScrollToBottom(props: { className?: string }) {
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();

  if (isAtBottom) return null;
  return (
    <Button
      variant="outline"
      className={props.className}
      onClick={() => scrollToBottom()}
    >
      <ArrowDown className="h-4 w-4" />
      <span>Scroll to bottom</span>
    </Button>
  );
}

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

const CONTEXT_REFERENCE_RE =
  /(@(?:file|folder):(?:`[^`]+`(?::\d+(?:-\d+)?)?|"[^"]+"(?::\d+(?:-\d+)?)?|'[^']+'(?::\d+(?:-\d+)?)?|[^\s,，。；;!?！？、]+))/gi;

function HighlightedComposerText({ text }: { text: string }) {
  if (!text) {
    return <span className="text-muted-foreground">Type your message...</span>;
  }

  const parts: ReactNode[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(CONTEXT_REFERENCE_RE)) {
    const value = match[0];
    const index = match.index ?? 0;
    if (index > lastIndex) {
      parts.push(text.slice(lastIndex, index));
    }
    parts.push(
      <span
        key={`${index}-${value}`}
        className="rounded bg-sky-500/10 text-sky-700 dark:text-sky-300"
      >
        {value}
      </span>,
    );
    lastIndex = index + value.length;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return <>{parts}</>;
}

function isToolOnlyAssistantMessage(message: Message): boolean {
  if (message.type !== "ai") return false;
  const toolCalls = (message as { tool_calls?: unknown[] }).tool_calls;
  if (!toolCalls?.length) return false;
  return getContentString(message.content).trim().length === 0;
}

export function Thread() {
  const [artifactContext, setArtifactContext] = useArtifactContext();
  const [artifactOpen, closeArtifact] = useArtifactOpen();

  const [threadId, _setThreadId] = useQueryState("threadId");
  const [chatHistoryOpen, setChatHistoryOpen] = useQueryState(
    "chatHistoryOpen",
    parseAsBoolean.withDefault(false),
  );
  const [hideToolCalls, setHideToolCalls] = useQueryState(
    "hideToolCalls",
    parseAsBoolean.withDefault(false),
  );
  const [input, setInput] = useState("");
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<BlobPart[]>([]);
  const {
    contentBlocks,
    setContentBlocks,
    handleFileUpload,
    dropRef,
    removeBlock,
    resetBlocks: _resetBlocks,
    dragOver,
    handlePaste,
  } = useFileUpload();
  const [firstTokenReceived, setFirstTokenReceived] = useState(false);
  const isLargeScreen = useMediaQuery("(min-width: 1024px)");

  const stream = useStreamContext();
  const messages = stream.messages;
  const isLoading = stream.isLoading;
  const contextUsage = stream.values.context_usage;

  const lastError = useRef<string | undefined>(undefined);

  const setThreadId = (id: string | null) => {
    _setThreadId(id);

    // close artifact and reset artifact context
    closeArtifact();
    setArtifactContext({});
  };

  useEffect(() => {
    if (!stream.error) {
      lastError.current = undefined;
      return;
    }
    try {
      const message = (stream.error as any).message;
      if (!message || lastError.current === message) {
        // Message has already been logged. do not modify ref, return early.
        return;
      }

      // Message is defined, and it has not been logged yet. Save it, and send the error
      lastError.current = message;
      toast.error("An error occurred. Please try again.", {
        description: (
          <p>
            <strong>Error:</strong> <code>{message}</code>
          </p>
        ),
        richColors: true,
        closeButton: true,
      });
    } catch {
      // no-op
    }
  }, [stream.error]);

  // TODO: this should be part of the useStream hook
  const prevMessageLength = useRef(0);
  useEffect(() => {
    if (
      messages.length !== prevMessageLength.current &&
      messages?.length &&
      messages[messages.length - 1].type === "ai"
    ) {
      setFirstTokenReceived(true);
    }

    prevMessageLength.current = messages.length;
  }, [messages]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if ((input.trim().length === 0 && contentBlocks.length === 0) || isLoading)
      return;
    setFirstTokenReceived(false);

    const newHumanMessage: Message = {
      id: uuidv4(),
      type: "human",
      content: [
        ...(input.trim().length > 0 ? [{ type: "text", text: input }] : []),
        ...contentBlocks.map(
          (block) => uploadedFileBlockToText(block) ?? block,
        ),
      ] as Message["content"],
    };

    const toolMessages = ensureToolCallsHaveResponses(stream.messages);

    const context =
      Object.keys(artifactContext).length > 0 ? artifactContext : undefined;

    stream.submit(
      { messages: [...toolMessages, newHumanMessage], context },
      {
        streamMode: ["values"],
        streamSubgraphs: true,
        streamResumable: false,
        optimisticValues: (prev) => ({
          ...prev,
          context,
          messages: [
            ...(prev.messages ?? []),
            ...toolMessages,
            newHumanMessage,
          ],
        }),
      },
    );

    setInput("");
    setContentBlocks([]);
  };

  const transcribeAudio = useCallback(async (blob: Blob) => {
    if (!blob.size) return;

    setIsTranscribing(true);
    try {
      const formData = new FormData();
      formData.append("file", blob, "recording.webm");
      formData.append("language", "auto");
      const response = await fetch("/api/local/asr", {
        method: "POST",
        body: formData,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body?.error || "Speech recognition failed.");
      }
      const text = String(body?.text || "").trim();
      if (!text) {
        toast.error("No speech recognized.");
        return;
      }
      setInput((current) => {
        const prefix = current.trim() ? `${current.trimEnd()}\n` : "";
        return `${prefix}${text}`;
      });
    } catch (error) {
      toast.error("Speech recognition failed.", {
        description:
          error instanceof Error ? error.message : "Please try again.",
        richColors: true,
        closeButton: true,
      });
    } finally {
      setIsTranscribing(false);
    }
  }, []);

  const handleVoiceInput = useCallback(async () => {
    if (isTranscribing) return;

    if (isRecording) {
      mediaRecorderRef.current?.stop();
      return;
    }

    if (!navigator.mediaDevices?.getUserMedia) {
      toast.error("Microphone recording is not supported in this browser.");
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm";
      const recorder = new MediaRecorder(stream, { mimeType });
      audioChunksRef.current = [];
      mediaRecorderRef.current = recorder;

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
        }
      };
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        mediaRecorderRef.current = null;
        setIsRecording(false);
        const audioBlob = new Blob(audioChunksRef.current, {
          type: recorder.mimeType || "audio/webm",
        });
        audioChunksRef.current = [];
        void transcribeAudio(audioBlob);
      };
      recorder.onerror = () => {
        stream.getTracks().forEach((track) => track.stop());
        mediaRecorderRef.current = null;
        audioChunksRef.current = [];
        setIsRecording(false);
        toast.error("Recording failed. Please try again.");
      };

      recorder.start();
      setIsRecording(true);
    } catch (error) {
      toast.error("Unable to access microphone.", {
        description:
          error instanceof Error ? error.message : "Please check browser permissions.",
        richColors: true,
        closeButton: true,
      });
    }
  }, [isRecording, isTranscribing, transcribeAudio]);

  const handleRegenerate = (
    parentCheckpoint: Checkpoint | null | undefined,
  ) => {
    // Do this so the loading state is correct
    prevMessageLength.current = prevMessageLength.current - 1;
    setFirstTokenReceived(false);
    stream.submit(undefined, {
      checkpoint: parentCheckpoint,
      streamMode: ["values"],
      streamSubgraphs: true,
      streamResumable: false,
    });
  };

  const chatStarted = !!threadId || !!messages.length;
  const hasNoAIOrToolMessages = !messages.find(
    (m) => m.type === "ai" || m.type === "tool",
  );
  const visibleMessages = messages.filter((message) => {
    if (message.id?.startsWith(DO_NOT_RENDER_ID_PREFIX)) return false;
    if (!hideToolCalls) return true;
    if (message.type === "tool") return false;
    return !isToolOnlyAssistantMessage(message);
  });
  const lastVisibleMessageId = visibleMessages.at(-1)?.id;
  const threadInterrupt = stream.interrupt;
  const handleSetBranch = useCallback(
    (branch: string) => stream.setBranch(branch),
    [stream],
  );

  return (
    <div className="flex h-screen w-full overflow-hidden">
      <div className="relative hidden lg:flex">
        <motion.div
          className="absolute z-20 h-full overflow-hidden border-r bg-white"
          style={{ width: 300 }}
          animate={
            isLargeScreen
              ? { x: chatHistoryOpen ? 0 : -300 }
              : { x: chatHistoryOpen ? 0 : -300 }
          }
          initial={{ x: -300 }}
          transition={
            isLargeScreen
              ? { type: "spring", stiffness: 300, damping: 30 }
              : { duration: 0 }
          }
        >
          <div
            className="relative h-full"
            style={{ width: 300 }}
          >
            <ThreadHistory />
          </div>
        </motion.div>
      </div>

      <div
        className={cn(
          "grid w-full grid-cols-[1fr_0fr] transition-all duration-500",
          artifactOpen && "grid-cols-[3fr_2fr]",
        )}
      >
        <motion.div
          className={cn(
            "relative flex min-w-0 flex-1 flex-col overflow-hidden",
            !chatStarted && "grid-rows-[1fr]",
          )}
          layout={isLargeScreen}
          animate={{
            marginLeft: chatHistoryOpen ? (isLargeScreen ? 300 : 0) : 0,
            width: chatHistoryOpen
              ? isLargeScreen
                ? "calc(100% - 300px)"
                : "100%"
              : "100%",
          }}
          transition={
            isLargeScreen
              ? { type: "spring", stiffness: 300, damping: 30 }
              : { duration: 0 }
          }
        >
          {!chatStarted && (
            <div className="absolute top-0 left-0 z-10 flex w-full items-center justify-between gap-3 p-2 pl-4">
              <div>
                {(!chatHistoryOpen || !isLargeScreen) && (
                  <Button
                    className="hover:bg-gray-100"
                    variant="ghost"
                    onClick={() => setChatHistoryOpen((p) => !p)}
                  >
                    {chatHistoryOpen ? (
                      <PanelRightOpen className="size-5" />
                    ) : (
                      <PanelRightClose className="size-5" />
                    )}
                  </Button>
                )}
              </div>
            </div>
          )}
          {chatStarted && (
            <div className="relative z-10 flex items-center justify-between gap-3 p-2">
              <div className="relative flex items-center justify-start gap-2">
                <div className="absolute left-0 z-10">
                  {(!chatHistoryOpen || !isLargeScreen) && (
                    <Button
                      className="hover:bg-gray-100"
                      variant="ghost"
                      onClick={() => setChatHistoryOpen((p) => !p)}
                    >
                      {chatHistoryOpen ? (
                        <PanelRightOpen className="size-5" />
                      ) : (
                        <PanelRightClose className="size-5" />
                      )}
                    </Button>
                  )}
                </div>
                <motion.button
                  className="flex cursor-pointer items-center gap-2"
                  onClick={() => setThreadId(null)}
                  animate={{
                    marginLeft: !chatHistoryOpen ? 48 : 0,
                  }}
                  transition={{
                    type: "spring",
                    stiffness: 300,
                    damping: 30,
                  }}
                >
                  <QingzhouLogo
                    width={32}
                    height={32}
                  />
                  <span className="text-xl font-semibold tracking-tight">
                    qingzhou-agent
                  </span>
                </motion.button>
              </div>

              <div className="flex items-center gap-4">
                <TooltipIconButton
                  size="lg"
                  className="p-4"
                  tooltip="New thread"
                  variant="ghost"
                  onClick={() => setThreadId(null)}
                >
                  <SquarePen className="size-5" />
                </TooltipIconButton>
              </div>

              <div className="from-background to-background/0 absolute inset-x-0 top-full h-5 bg-gradient-to-b" />
            </div>
          )}

          <StickToBottom className="relative flex-1 overflow-hidden">
            <StickyToBottomContent
              className={cn(
                "absolute inset-0 overflow-y-scroll px-4 [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-gray-300 [&::-webkit-scrollbar-track]:bg-transparent",
                !chatStarted && "mt-[25vh] flex flex-col items-stretch",
                chatStarted && "grid grid-rows-[1fr_auto]",
              )}
              contentClassName="pt-8 pb-16 max-w-3xl mx-auto flex flex-col gap-4 w-full"
              content={
                <>
                  {visibleMessages
                    .map((message, index) =>
                      message.type === "human" ? (
                        <HumanMessage
                          key={message.id || `${message.type}-${index}`}
                          message={message}
                          isLoading={isLoading}
                        />
                      ) : (
                        <AssistantMessage
                          key={message.id || `${message.type}-${index}`}
                          message={message}
                          isLoading={isLoading}
                          handleRegenerate={handleRegenerate}
                          isLastMessage={
                            lastVisibleMessageId === message.id
                          }
                          hasNoAIOrToolMessages={hasNoAIOrToolMessages}
                          threadInterrupt={threadInterrupt}
                          hideToolCalls={hideToolCalls ?? false}
                          parentCheckpoint={
                            message
                              ? stream.getMessagesMetadata(message)
                                  ?.firstSeenState?.parent_checkpoint
                              : undefined
                          }
                          branch={
                            message
                              ? stream.getMessagesMetadata(message)?.branch
                              : undefined
                          }
                          branchOptions={
                            message
                              ? stream.getMessagesMetadata(message)
                                  ?.branchOptions
                              : undefined
                          }
                          onSetBranch={handleSetBranch}
                        />
                      ),
                    )}
                  {/* Special rendering case where there are no AI/tool messages, but there is an interrupt.
                    We need to render it outside of the messages list, since there are no messages to render */}
                  {hasNoAIOrToolMessages && !!stream.interrupt && (
                    <AssistantMessage
                      key="interrupt-msg"
                      message={undefined}
                      isLoading={isLoading}
                      handleRegenerate={handleRegenerate}
                      isLastMessage={true}
                      hasNoAIOrToolMessages={hasNoAIOrToolMessages}
                      threadInterrupt={threadInterrupt}
                      hideToolCalls={hideToolCalls ?? false}
                      parentCheckpoint={undefined}
                      branch={undefined}
                      branchOptions={undefined}
                      onSetBranch={handleSetBranch}
                    />
                  )}
                  {isLoading && !firstTokenReceived && (
                    <AssistantMessageLoading />
                  )}
                </>
              }
              footer={
                <div className="sticky bottom-0 flex flex-col items-center gap-8 bg-white">
                  {!chatStarted && (
                    <div className="flex items-center gap-4">
                      <QingzhouLogo
                        width={44}
                        height={44}
                        className="h-11 w-11 flex-shrink-0"
                      />
                      <h1 className="text-2xl font-semibold tracking-tight">
                        qingzhou-agent
                      </h1>
                    </div>
                  )}

                  <ScrollToBottom className="animate-in fade-in-0 zoom-in-95 absolute bottom-full left-1/2 mb-4 -translate-x-1/2" />

                  <div
                    ref={dropRef}
                    className={cn(
                      "bg-muted relative z-10 mx-auto mb-8 w-full max-w-3xl rounded-2xl shadow-xs transition-all",
                      dragOver
                        ? "border-primary border-2 border-dotted"
                        : "border border-solid",
                    )}
                  >
                    <form
                      onSubmit={handleSubmit}
                      className="mx-auto grid max-w-3xl grid-rows-[1fr_auto] gap-2"
                    >
                      <ContentBlocksPreview
                        blocks={contentBlocks}
                        onRemove={removeBlock}
                      />
                      <div className="relative">
                        <pre
                          aria-hidden="true"
                          className="pointer-events-none absolute inset-0 p-3.5 pb-0 font-sans text-base leading-normal break-words whitespace-pre-wrap md:text-sm"
                        >
                          <HighlightedComposerText text={input} />
                        </pre>
                        <textarea
                          value={input}
                          onChange={(e) => setInput(e.target.value)}
                          onPaste={handlePaste}
                          onKeyDown={(e) => {
                            if (
                              e.key === "Enter" &&
                              !e.shiftKey &&
                              !e.metaKey &&
                              !e.nativeEvent.isComposing
                            ) {
                              e.preventDefault();
                              const el = e.target as HTMLElement | undefined;
                              const form = el?.closest("form");
                              form?.requestSubmit();
                            }
                          }}
                          placeholder="Type your message..."
                          className="caret-foreground relative z-10 field-sizing-content w-full resize-none border-none bg-transparent p-3.5 pb-0 font-sans text-base leading-normal break-words whitespace-pre-wrap text-transparent shadow-none ring-0 outline-none placeholder:text-transparent focus:ring-0 focus:outline-none md:text-sm"
                        />
                      </div>

                      <div className="flex flex-wrap items-center gap-4 p-2 pt-4">
                        <div>
                          <div className="flex items-center space-x-2">
                            <Switch
                              id="render-tool-calls"
                              checked={hideToolCalls ?? false}
                              onCheckedChange={setHideToolCalls}
                            />
                            <Label
                              htmlFor="render-tool-calls"
                              className="text-sm text-gray-600"
                            >
                              Hide Tool Calls
                            </Label>
                          </div>
                        </div>
                        <Label
                          htmlFor="file-input"
                          className="flex cursor-pointer items-center gap-2"
                        >
                          <Plus className="size-5 text-gray-600" />
                          <span className="text-sm text-gray-600">
                            Upload PDF or Image
                          </span>
                        </Label>
                        <input
                          id="file-input"
                          type="file"
                          onChange={handleFileUpload}
                          multiple
                          accept="image/jpeg,image/png,image/gif,image/webp,application/pdf"
                          className="hidden"
                        />
                        <TooltipIconButton
                          type="button"
                          tooltip={
                            isRecording
                              ? "Stop recording"
                              : isTranscribing
                                ? "Transcribing"
                                : "Voice input"
                          }
                          className={cn(
                            "size-8",
                            isRecording && "text-red-600",
                          )}
                          disabled={isTranscribing}
                          onClick={handleVoiceInput}
                        >
                          {isTranscribing ? (
                            <LoaderCircle className="size-5 animate-spin" />
                          ) : isRecording ? (
                            <Square className="size-5" />
                          ) : (
                            <Mic className="size-5" />
                          )}
                        </TooltipIconButton>
                        <div className="ml-auto flex items-center gap-3">
                          <div
                            className="text-muted-foreground bg-background/70 rounded-md border px-2.5 py-1 text-xs whitespace-nowrap"
                            title={
                              contextUsage?.input_tokens != null
                                ? `${contextUsage.input_tokens.toLocaleString()} input tokens${contextUsage.output_tokens != null ? `, ${contextUsage.output_tokens.toLocaleString()} output tokens` : ""}${contextUsage.total_tokens != null ? `, ${contextUsage.total_tokens.toLocaleString()} total tokens` : ""}, ${contextUsage.message_count.toLocaleString()} messages${contextUsage.includes_tools ? ", tools included" : ""}. Source: ${contextUsage.counter ?? "model tokenizer"}`
                                : contextUsage?.error
                                  ? `Exact context usage unavailable: ${contextUsage.error}`
                                  : "Exact context usage will appear after the next model call."
                            }
                          >
                            {contextUsage?.input_tokens != null ? (
                              <>
                                Context {formatCount(contextUsage.input_tokens)}{" "}
                                tok | {contextUsage.message_count} msgs
                              </>
                            ) : contextUsage?.error ? (
                              "Context unavailable"
                            ) : (
                              "Context pending"
                            )}
                          </div>
                          {stream.isLoading ? (
                            <Button
                              key="stop"
                              onClick={() => stream.stop()}
                            >
                              <LoaderCircle className="h-4 w-4 animate-spin" />
                              Cancel
                            </Button>
                          ) : (
                            <Button
                              type="submit"
                              className="shadow-md transition-all"
                              disabled={
                                isLoading ||
                                (!input.trim() && contentBlocks.length === 0)
                              }
                            >
                              Send
                            </Button>
                          )}
                        </div>
                      </div>
                    </form>
                  </div>
                </div>
              }
            />
          </StickToBottom>
        </motion.div>
        <div className="relative flex flex-col border-l">
          <div className="absolute inset-0 flex min-w-[30vw] flex-col">
            <div className="grid grid-cols-[1fr_auto] border-b p-4">
              <ArtifactTitle className="truncate overflow-hidden" />
              <button
                onClick={closeArtifact}
                className="cursor-pointer"
              >
                <XIcon className="size-5" />
              </button>
            </div>
            <ArtifactContent className="relative flex-grow" />
          </div>
        </div>
      </div>
    </div>
  );
}
