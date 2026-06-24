# 前端流式输出时页面卡顿问题

**日期**：2026-06-24

## 问题现象

后台等待 LLM 输出时，前端页面鼠标无法滚动，整个 UI 变成无响应状态。
之前的 ToolResult JSON.parse/stringify 优化已实施，但卡顿依然存在，说明根因不在组件内部计算。

## Root cause

**核心问题：`useStream` 未启用 throttle，每个 SSE 事件都立即触发 React 全量 re-render。**

### 完整调用链分析

1. **LangGraph SSE 流** — 后端 `streamMode: ["values"]` 模式，每个 token/状态更新发送一个 SSE `values` 事件，包含完整的 state（含全部 messages 数组）。LLM 流式输出 token 频率约 20-60 次/秒。

2. **StreamManager 无 throttle** — `providers/Stream.tsx:100` 调用 `useTypedStream` 时没有传 `throttle` 参数，SDK 默认 `options.throttle ?? false`，即 `throttle: false`。

   SDK `StreamManager.subscribe` 的实现（`manager.js:287-305`）：
   ```js
   subscribe = (listener) => {
     if (this.throttle === false) {
       // throttle: false → 每次 notifyListeners() 直接调用 listener
       // 即每个 SSE 事件 → 立即触发 React re-render
       this.listeners.add(listener);
       return () => this.listeners.delete(listener);
     }
     // throttle: 50 → setTimeout(fn, 50ms)
     // 50ms 内的新更新取消前一个 timer 重新计时
     // React 最多每 50ms 渲染一次，中间事件被合并
     const timeoutMs = this.throttle === true ? 0 : this.throttle;
     let timeoutId;
     const throttledListener = () => {
       clearTimeout(timeoutId);
       timeoutId = setTimeout(() => { listener(); }, timeoutMs);
     };
     ...
   };
   ```

3. **useSyncExternalStore 触发全组件树 re-render** — `StreamManager` 用 `useSyncExternalStore` 桥接 React。每个 SSE 事件 → `setStreamValues` → `setState`（bump version）→ `notifyListeners` → React 检测 snapshot 变化 → Thread 及所有子组件 re-render。

4. **所有已完成消息也 re-render** — `AssistantMessage` 内部使用了 `useStreamContext()` 和 `useQueryState()`，React context 变化会穿透 React.memo，导致已完成消息也全量 re-render。

5. **流式消息的 MarkdownText 每帧重新解析** — 正在输出的 AI 消息 content 每个 token 变化，`MarkdownText`（虽有 memo）的 children prop 改变 → 触发 ReactMarkdown + remarkGfm + remarkMath + rehypeKatex 全量重新解析。

6. **StickToBottom 每帧检查滚动位置** — `use-stick-to-bottom` 在每次内容变化时重新计算 scroll 位置，高频更新下也增加开销。

### 频率估算（修复前）

- LLM 输出约 30 token/秒
- 每个 token → 1 个 SSE `values` 事件 → 1 次 React re-render
- **30 次/秒的全组件树 re-render**，主线程被密集渲染占满 → 无法响应用户输入

## 已实施的修复

### 修复 1：throttle: 50（`providers/Stream.tsx`）

给 `useTypedStream` 加 `throttle: 50`，将 React 渲染频率限制到约 20fps：

```tsx
const streamValue = useTypedStream({
  apiUrl,
  apiKey: apiKey ?? undefined,
  assistantId,
  throttle: 50,  // ← 新增
  ...
});
```

效果：
- SSE 内部状态仍然正常累积（每个 token 都存进 state，不丢数据）
- React 只在 timer 到期时拿最新状态渲染一次
- 50ms ≈ 20fps，流式文字显示完全够用，用户感知不到延迟
- 主线程有充足空闲处理滚动事件

### 修复 2：React.memo 包裹 AssistantMessage（`messages/ai.tsx` + `index.tsx`）

**关键设计：移除 AssistantMessage 内部的 context 依赖，改为由 Thread 组件传入 props。**

原因：`React.memo` 只能拦截因父组件 re-render 传递未变 props 而触发的子组件 re-render。如果 AssistantMessage 内部调用 `useStreamContext()`，context 变化会直接穿透 memo，导致即使 props 未变也强制 re-render。

**改动细节：**

1. `ai.tsx` — 从 `AssistantMessage` 移除 `useStreamContext()` 和 `useQueryState("hideToolCalls")`，改为接收以下 props：
   - `isLastMessage`, `hasNoAIOrToolMessages`, `threadInterrupt`, `hideToolCalls`
   - `parentCheckpoint`, `branch`, `branchOptions`, `onSetBranch`

2. `ai.tsx` — 用 `memo(AssistantMessage, areAssistantMessagePropsEqual)` 包裹，自定义比较器：
   - 比较 message.id + getContentString(message.content) 判断内容是否变化
   - 比较 isLoading, isLastMessage, hasNoAIOrToolMessages, hideToolCalls 等布尔值
   - **跳过函数 props（handleRegenerate, onSetBranch）**：它们每次创建新引用但行为稳定（始终操作同一个 StreamManager 实例）
   - 结果：已完成消息 → comparator 返回 true → 跳过 re-render；流式消息 → content 变化 → comparator 返回 false → 正常 re-render

3. `index.tsx` — Thread 组件计算上述 props 并传入 `AssistantMessage`

4. `ai.tsx` — `CustomComponent` 不再接收 `thread` prop，改为自己调用 `useStreamContext()`。CustomComponent 仍然订阅 stream context，会独立 re-render，但它是轻量组件，不影响 AssistantMessage 的 memo 效果。

**原理：** 当 Thread re-render 时（因 stream context 变化），React 检查每个 `AssistantMessage` 的 props。对于已完成消息，comparator 判断 props 未变 → 跳过 re-render。只有正在流式输出的消息因 content 变化而 re-render。这意味着每次渲染周期只处理 1 条消息，而不是 N 条全部消息。

## 与之前 ToolResult 优化的关系

三组优化互补，各有分工：

| 优化 | 解决的问题 | 效果 |
|------|-----------|------|
| throttle: 50 | re-render 频率过高 | 30次/秒 → 20次/秒 |
| React.memo + 自定义 comparator | 已完成消息也 re-render | N条 → 只渲染1条流式消息 |
| useMemo / 移除 height:auto / 大字符串截断 | 单次渲染内部昂贵计算 | 每次渲染更快 |

如果只做 throttle：频率降低但每次渲染仍处理 N 条消息。
如果只做 memo：频率不变，但每次渲染只处理 1 条消息。
如果只做 useMemo：频率不变、消息数不变，但每次渲染稍快。
三者配合：20fps × 1条消息 × 更快的单次渲染 → 主线程大部分时间空闲 → 滚动流畅。
