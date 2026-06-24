import { AIMessage, ToolMessage } from "@langchain/langgraph-sdk";
import { useState, useMemo } from "react";
import { motion } from "framer-motion";
import { ChevronDown, ChevronUp } from "lucide-react";

const TOOL_ARG_PREVIEW_CHARS = 100;

function isComplexValue(value: any): boolean {
  return Array.isArray(value) || (typeof value === "object" && value !== null);
}

function truncateText(value: string): { text: string; truncated: boolean } {
  if (value.length <= TOOL_ARG_PREVIEW_CHARS) {
    return { text: value, truncated: false };
  }
  return {
    text: `${value.slice(0, TOOL_ARG_PREVIEW_CHARS)}...`,
    truncated: true,
  };
}

function getArgPreview(value: unknown): { text: string; expandable: boolean } {
  if (typeof value === "string") {
    const preview = truncateText(value);
    return { text: preview.text, expandable: preview.truncated };
  }

  if (
    value === null ||
    value === undefined ||
    typeof value === "number" ||
    typeof value === "boolean" ||
    typeof value === "bigint"
  ) {
    return { text: String(value), expandable: false };
  }

  if (Array.isArray(value)) {
    return { text: `Array(${value.length})`, expandable: true };
  }

  if (typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>);
    const shownKeys = keys.slice(0, 5).join(", ");
    const suffix = keys.length > 5 ? ", ..." : "";
    return {
      text: `Object { ${shownKeys}${suffix} }`,
      expandable: true,
    };
  }

  const preview = truncateText(String(value));
  return { text: preview.text, expandable: preview.truncated };
}

const EXPAND_MAX_CHARS = 2000;

/** Scan rawStr for first 4 line breaks within 500 chars, without allocating a full split array. */
function getCollapsedPreview(rawStr: string): { preview: string; truncated: boolean } {
  let lineCount = 0;
  let lastBreak = -1;
  for (let i = 0; i < rawStr.length && i < 500; i++) {
    if (rawStr[i] === "\n") {
      lineCount++;
      lastBreak = i;
      if (lineCount >= 4) break;
    }
  }

  if (rawStr.length <= 500 && lineCount < 4) {
    return { preview: rawStr, truncated: false };
  }

  if (rawStr.length > 500) {
    return { preview: rawStr.slice(0, 500) + "...", truncated: true };
  }

  // <= 500 chars but >= 4 lines
  return { preview: rawStr.slice(0, lastBreak) + "\n...", truncated: true };
}

function stringifyExpandedArg(value: unknown): string {
  if (typeof value === "string") {
    if (value.length > EXPAND_MAX_CHARS) {
      return value.slice(0, EXPAND_MAX_CHARS) + `...（共 ${value.length} 字符）`;
    }
    return value;
  }
  try {
    const str = JSON.stringify(value, null, 2);
    if (str.length > EXPAND_MAX_CHARS) {
      return str.slice(0, EXPAND_MAX_CHARS) + `...（共 ${str.length} 字符）`;
    }
    return str;
  } catch {
    return String(value);
  }
}

function ToolArgValue({ value }: { value: unknown }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const preview = getArgPreview(value);
  const canExpand = preview.expandable;
  const expandedText = useMemo(
    () => isExpanded ? stringifyExpandedArg(value) : null,
    [isExpanded, value],
  );
  const displayedText = isExpanded ? expandedText! : preview.text;

  return (
    <div className="flex flex-col gap-1">
      <code className="rounded bg-gray-50 px-2 py-1 font-mono text-sm break-all whitespace-pre-wrap">
        {displayedText}
      </code>
      {canExpand && (
        <button
          type="button"
          onClick={() => setIsExpanded((prev) => !prev)}
          className="w-fit text-xs font-medium text-gray-500 underline-offset-2 hover:text-gray-700 hover:underline"
        >
          {isExpanded ? "折叠" : "展开"}
        </button>
      )}
    </div>
  );
}

export function ToolCalls({
  toolCalls,
}: {
  toolCalls: AIMessage["tool_calls"];
}) {
  if (!toolCalls || toolCalls.length === 0) return null;

  return (
    <div className="mx-auto grid max-w-3xl grid-rows-[1fr_auto] gap-2">
      {toolCalls.map((tc, idx) => {
        const args = tc.args as Record<string, any>;
        const hasArgs = Object.keys(args).length > 0;
        return (
          <div
            key={idx}
            className="overflow-hidden rounded-lg border border-gray-200"
          >
            <div className="border-b border-gray-200 bg-gray-50 px-4 py-2">
              <h3 className="font-medium text-gray-900">
                {tc.name}
                {tc.id && (
                  <code className="ml-2 rounded bg-gray-100 px-2 py-1 text-sm">
                    {tc.id}
                  </code>
                )}
              </h3>
            </div>
            {hasArgs ? (
              <table className="min-w-full divide-y divide-gray-200">
                <tbody className="divide-y divide-gray-200">
                  {Object.entries(args).map(([key, value], argIdx) => (
                    <tr key={argIdx}>
                      <td className="px-4 py-2 text-sm font-medium whitespace-nowrap text-gray-900">
                        {key}
                      </td>
                      <td className="px-4 py-2 text-sm text-gray-500">
                        <ToolArgValue value={value} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <code className="block p-3 text-sm">{"{}"}</code>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ToolResult({ message }: { message: ToolMessage }) {
  const [isExpanded, setIsExpanded] = useState(false);

  // Collapsed state: only slice raw string, no expensive parse/stringify
  const raw = message.content;
  const rawStr = typeof raw === "string" ? raw : String(raw);
  const { preview: collapsedPreview, truncated: shouldTruncate } = getCollapsedPreview(rawStr);

  // Expanded state: full parse + stringify (lazy — only computed when expanded)
  const expanded = useMemo(() => {
    if (!isExpanded) return null;

    let parsed: any;
    let isJson = false;

    if (typeof raw === "string") {
      const first = raw.trim()[0];
      if (first === "{" || first === "[") {
        try {
          parsed = JSON.parse(raw);
          isJson = isComplexValue(parsed);
        } catch {
          parsed = raw;
        }
      } else {
        parsed = raw;
      }
    } else {
      parsed = raw;
    }

    const str = isJson ? JSON.stringify(parsed, null, 2) : rawStr;
    return { parsedContent: parsed, isJsonContent: isJson, contentStr: str };
  }, [isExpanded, raw, rawStr]);

  const isJsonContent = expanded?.isJsonContent ?? false;
  const parsedContent = expanded?.parsedContent;
  const displayedContent = expanded?.contentStr ?? collapsedPreview;

  return (
    <div className="mx-auto grid max-w-3xl grid-rows-[1fr_auto] gap-2">
      <div className="overflow-hidden rounded-lg border border-gray-200">
        <div className="border-b border-gray-200 bg-gray-50 px-4 py-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            {message.name ? (
              <h3 className="font-medium text-gray-900">
                Tool Result:{" "}
                <code className="rounded bg-gray-100 px-2 py-1">
                  {message.name}
                </code>
              </h3>
            ) : (
              <h3 className="font-medium text-gray-900">Tool Result</h3>
            )}
            {message.tool_call_id && (
              <code className="ml-2 rounded bg-gray-100 px-2 py-1 text-sm">
                {message.tool_call_id}
              </code>
            )}
          </div>
        </div>
        <div className="min-w-full bg-gray-100">
          <div className="p-3">
                {isJsonContent ? (
                  <table className="min-w-full divide-y divide-gray-200">
                    <tbody className="divide-y divide-gray-200">
                      {(Array.isArray(parsedContent)
                        ? isExpanded
                          ? parsedContent
                          : parsedContent.slice(0, 5)
                        : Object.entries(parsedContent)
                      ).map((item, argIdx) => {
                        const [key, value] = Array.isArray(parsedContent)
                          ? [argIdx, item]
                          : [item[0], item[1]];
                        return (
                          <tr key={argIdx}>
                            <td className="px-4 py-2 text-sm font-medium whitespace-nowrap text-gray-900">
                              {key}
                            </td>
                            <td className="px-4 py-2 text-sm text-gray-500">
                              {isComplexValue(value) ? (
                                <code className="rounded bg-gray-50 px-2 py-1 font-mono text-sm break-all">
                                  {JSON.stringify(value, null, 2)}
                                </code>
                              ) : (
                                String(value)
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                ) : (
                  <code className="block text-sm">{displayedContent}</code>
                )}
          </div>
          {shouldTruncate && (
            <motion.button
              onClick={() => setIsExpanded(!isExpanded)}
              className="flex w-full cursor-pointer items-center justify-center border-t-[1px] border-gray-200 py-2 text-gray-500 transition-all duration-200 ease-in-out hover:bg-gray-50 hover:text-gray-600"
              initial={{ scale: 1 }}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              {isExpanded ? <ChevronUp /> : <ChevronDown />}
            </motion.button>
          )}
        </div>
      </div>
    </div>
  );
}
