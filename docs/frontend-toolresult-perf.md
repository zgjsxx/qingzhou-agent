# 前端 ToolResult 渲染性能问题

**日期**：2026-06-23

## 问题现象

对话执行到一定步数后，前端 UI 变得很卡，鼠标滚轮无法滑动。随着 tool call 数量增多和内容变大，卡顿越来越严重。

## Root cause

`frontend/src/components/thread/messages/tool-calls.tsx` 中有三个性能瓶颈：

### 1. ToolResult 每次 render 都重新 JSON.parse + JSON.stringify（第 148-158 行）

```js
parsedContent = JSON.parse(message.content);       // 每次 render 都 parse
contentStr = JSON.stringify(parsedContent, null, 2); // 每次 render 都 stringify + 格式化
```

当 tool result 内容很大时（shell 命令输出 12000 字符），每次组件 re-render 都重新 parse/stringify，是 O(n) 开销。而且 `message.content` 大多数情况下不是 JSON，仍然每次都尝试 parse。

### 2. ToolArgValue 对 write_file 的 content 参数做 JSON.stringify（第 56-62 行）

```js
stringifyExpandedArg(value) => JSON.stringify(value, null, 2)
```

write_file 的 content 参数可能包含整个脚本内容，展开时 JSON.stringify 整个脚本字符串，加上 `null, 2` 格式化双倍开销。

### 3. framer-motion 的 height:"auto" 动画（第 194-195 行）

```js
animate={{ height: "auto" }}
transition={{ duration: 0.3 }}
```

`height: "auto"` 动画需要 framer-motion 每帧计算实际高度，对大内容块触发昂贵的布局重排。

## 修复方向

- 用 `useMemo` 缓存 JSON.parse/stringify 结果，避免每次 render 重复计算
- 对大内容跳过 JSON.parse 尝试（先检查内容是否以 `{` 或 `[` 开头）
- 对大内容块禁用 height:"auto" 动画，或改用固定高度 + overflow 方案
- 对 write_file content 等大字符串参数，限制展开后的显示长度
