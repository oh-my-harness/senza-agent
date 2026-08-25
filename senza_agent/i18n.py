"""
i18n for QevosAgent — terminal/UI strings AND LLM-facing protocol strings.

Language is detected from the system locale; set QEVOS_LANG=zh or QEVOS_LANG=en
to override.

String categories:
  interrupt.*   User interrupt terminal output
  status.*      /status display
  log.*         /log display
  advisor.*     Advisor context and injection strings
  artifact.*    Artifact index manifest strings
"""

import locale
import os

# ── Language detection ────────────────────────────────────────────────────────

def _detect_lang() -> str:
    override = os.environ.get("QEVOS_LANG", "")
    if override:
        return "zh" if override.lower().startswith("zh") else "en"
    try:
        sys_locale = locale.getlocale()[0] or ""
    except Exception:
        sys_locale = ""
    return "zh" if sys_locale.lower().startswith("zh") else "en"

LANG: str = _detect_lang()

# ── String tables ─────────────────────────────────────────────────────────────

_STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        # ── artifact_index.py（确定性落盘清单，非 LLM 生成）──────────────────
        "artifact.manifest_header": "## 已落盘文件清单（系统生成 · 完整可信）",
        "artifact.manifest_hint": (
            "以下文件已确实写入磁盘，其内容**不在**上下文中。"
            "需要时用 read_file / shell / run_python 按需读取，不要臆测其内容，也不要重复生成。"
        ),
        "artifact.manifest_omitted": "（更早的 {n} 个产物已从清单省略，可在 artifacts/ 目录下自行查找）",
        "artifact.chars":            "{n} 字符",
        "artifact.src.spill":        "工具输出溢出",
        "artifact.src.watcher":      "watcher 溢出",
        "artifact.src.write_file":   "write_file",
        "artifact.src.other":        "落盘",

        # ── advisor.py ────────────────────────────────────────────────────────
        "advisor.trigger_msg": (
            "触发原因：{reason}\n\n"
            "请审视以下 Agent 的当前状态，给出战略性指导意见。\n\n"
            "---\n{context}\n---"
        ),
        "advisor.ctx.iter":       "## 当前迭代轮次\n第 {iter} 轮",
        "advisor.ctx.goal":       "## 原始任务目标\n{goal}",
        "advisor.ctx.user_inj":   "## 用户后续指令（按时间顺序，不得概括，必须优先满足）\n{items}",
        "advisor.ctx.user_inj_empty": "## 用户后续指令\n（暂无用户中途下达的额外指令）",
        "advisor.ctx.sp":         "## 草稿本（Agent 当前工作状态）\n{sp}",
        "advisor.ctx.sp_empty":   "## 草稿本\n（草稿本为空）",
        "advisor.ctx.progress":   "## 工作进展日志（主 Agent 自述，来源={method}，更新于 iter={iter}；可能存在自我偏置，请结合最近原始片段交叉验证）\n{log}",
        "advisor.ctx.tools":      "## 可用工具与能力（仅名称与简介）\n{items}",
        "advisor.ctx.tools_empty": "## 可用工具与能力\n（无工具）",
        "advisor.ctx.skills_header": "### 可用领域技能（agent 可用 read_skill 读取全文）",
        "advisor.ctx.history":    "## 最近原始执行片段（最后 {n} 条，供与自述对账）\n{hist}",
        "advisor.ctx.no_history": "（暂无历史记录）",
        "advisor.ctx.truncated":  "…[截断]",
        "advisor.ctx.graph": "## 执行图（结构化进展骨架）\n{graph}",
        "advisor.sys.conv_header": "\n\n# 项目规范摘要（AGENTS.md）\n以下为本次运行的项目规范，主 Agent 必须遵守；你在给出建议时应优先与之对齐。\n",
        "advisor.sys.read_rules": "\n\n## 阅读约定\n- 用户上下文中的「## 工作进展日志」为主 Agent 自述，可能存在自我偏置；请结合「## 最近原始执行片段」交叉验证。\n- 「## 用户后续指令」必须优先满足。发现主 Agent 未执行用户原文要求时直接指出。\n- 给出具体指导前，先检查「## 可用工具与能力」，避免建议主 Agent 自行造轮子。\n",

        # user_interrupt.py — terminal interaction
        "interrupt.pause_detected":
            "[干预] 检测到 /，Agent 将在当前操作结束后暂停。"
            "请输入命令后回车，或直接回车显示帮助：",
        "interrupt.ack":           "[用户干预] 已收到 {name}，将在当前工具调用结束后生效。",
        "interrupt.webcmd":        "[Web看板] 注入命令: {cmd}",
        "interrupt.pause":         "[用户干预] /pause 已收到，Agent 将在当前操作结束后暂停。",
        "interrupt.pause_awaiting": "Agent 已暂停。请告诉我下一步该做什么，或输入 /exit 退出。",
        "interrupt.stop":
            "[用户干预] /stop 已生效：当前工具将被终止，Agent 继续执行。"
            "（如需退出程序，请输入 /exit）",
        "interrupt.exit":          "[用户干预] /exit：Agent 即将退出。",
        "interrupt.newtask_usage": "[用户干预] 用法: /newtask <新任务目标>",
        "interrupt.newtask_done":  "[用户干预] 新目标已注入：{arg}",
        "interrupt.inject_usage":  "[用户干预] 用法: /inject <消息内容>",
        "interrupt.inject_done":   "[用户干预] 消息已注入，下轮 LLM 可感知。",
        "interrupt.compress":
            "[压缩] 已标记：下次 LLM 调用前将压缩上下文，"
            "保留最近 {keep} 条（当前共 {before} 条）。",
        "interrupt.rigor_on":      "[用户干预] thought 严密模式已开启（观察→推断→决策），下轮生效。",
        "interrupt.rigor_off":     "[用户干预] thought 严密模式已关闭，下轮生效。",
        "interrupt.rigor_usage":   "[用户干预] 用法: /rigor on|off（当前: {state}）",
        "interrupt.add_iters":     "[用户干预] 已增加 {n} 次迭代，累计待增加: {total} 次。",
        "interrupt.add_iters_usage":"[用户干预] 用法: /+<正整数>，例如 /+50",
        "interrupt.unknown_cmd":   "[用户干预] 未知命令: {name}。输入 /help 查看可用命令。",

        # user_interrupt.py — /status display
        "status.header":           "[状态]  迭代: {i}  工具数: {tools}  长期记忆: {lt} 条",
        "status.current_tool":     "  当前工具: {tool}  已耗时: {elapsed}",
        "status.idle":             "  当前工具: (空闲中)",
        "status.scratchpad":       "草稿本:",
        "status.truncated":        "\n...[截断]",

        # user_interrupt.py — /log display
        "log.header":              "[执行记录] 最近 {n} / 共 {total} 条",
        "log.tool":                "🔧 [#{i}] 工具: {tool}",
        "log.done":                "✨ [#{i}] 完成",
        "log.thought":             "💭 [#{i}] 思考",
        "log.result_tag":          "📥 结果",

        # user_interrupt.py — HELP_TEXT
        "interrupt.help": """\
[用户干预命令] - 输入 / 即可触发：
  /help              立即显示此帮助（不等当前工具结束）
  /pause             当前操作结束后暂停，等待用户下一步指令
  /stop              终止当前正在执行的工具，Agent 继续下一步
  /exit              退出整个 Agent 程序
  /inject <消息>     将消息注入 Agent 上下文，下轮 LLM 可感知
  /newtask <目标>    注入新任务目标（nostop 模式专用，解除等待并开始新一轮）
  /compress [N]      下次 LLM 调用前压缩上下文（保留最近 N 条，默认 8）
  /rigor on|off      切换 thought 严密模式（观察→推断→决策），下轮生效
  /status            显示当前状态：迭代号、正在执行的工具、草稿本
  /log [N]           显示最近 N 条执行记录（默认 5 条）
  /+N                增加 N 次最大迭代次数（例如 /+50）
  （/status 和 /log 在工具执行中也会立即响应）
提示: 只需输入 / 即可暂停，完整命令后按回车生效。
""",
    },

    "en": {
        # ── artifact_index.py (deterministic spill manifest, not LLM-generated) ──
        "artifact.manifest_header": "## Files already written to disk (system-generated · authoritative)",
        "artifact.manifest_hint": (
            "The files below are definitely on disk; their contents are **not** in the context. "
            "Read them on demand with read_file / shell / run_python — do not guess their contents "
            "and do not regenerate them."
        ),
        "artifact.manifest_omitted": "({n} older artifacts omitted from this list; look under artifacts/ if needed)",
        "artifact.chars":            "{n} chars",
        "artifact.src.spill":        "tool output spill",
        "artifact.src.watcher":      "watcher spill",
        "artifact.src.write_file":   "write_file",
        "artifact.src.other":        "written",

        # ── advisor.py ────────────────────────────────────────────────────────
        "advisor.trigger_msg": (
            "Trigger: {reason}\n\n"
            "Please review the Agent's current state below and provide strategic guidance.\n\n"
            "---\n{context}\n---"
        ),
        "advisor.ctx.iter":       "## Current Iteration\nIteration {iter}",
        "advisor.ctx.goal":       "## Original Task Goal\n{goal}",
        "advisor.ctx.user_inj":   "## User Follow-up Instructions (chronological, do not summarise, MUST be addressed first)\n{items}",
        "advisor.ctx.user_inj_empty": "## User Follow-up Instructions\n(none yet)",
        "advisor.ctx.sp":         "## Scratchpad (Agent current state)\n{sp}",
        "advisor.ctx.sp_empty":   "## Scratchpad\n(empty)",
        "advisor.ctx.progress":   "## Work Progress Log (main agent's self-account, source={method}, updated at iter={iter}; may contain self-bias — cross-check against the recent raw fragments)\n{log}",
        "advisor.ctx.tools":      "## Available Tools & Capabilities (names + one-line summaries)\n{items}",
        "advisor.ctx.tools_empty": "## Available Tools & Capabilities\n(none)",
        "advisor.ctx.skills_header": "### Available domain skills (the agent can read_skill for full text)",
        "advisor.ctx.history":    "## Recent Raw Execution Fragments (last {n}, for cross-checking the self-account)\n{hist}",
        "advisor.ctx.no_history": "(no history yet)",
        "advisor.ctx.truncated":  "…[truncated]",
        "advisor.ctx.graph": "## Execution graph (structured progress skeleton)\n{graph}",
        "advisor.sys.conv_header": "\n\n# Project Conventions (AGENTS.md)\nThe following are this run's project conventions. The main agent must obey them; align your advice with these rules.\n",
        "advisor.sys.read_rules": "\n\n## Reading Conventions\n- The '## Work Progress Log' in the user context is the main agent's self-account and may contain self-bias; cross-check it against '## Recent Raw Execution Fragments'.\n- '## User Follow-up Instructions' MUST be addressed first. Flag any user requirement the main agent has not actually executed.\n- Before giving concrete advice, consult '## Available Tools & Capabilities' to avoid suggesting that the main agent reinvent the wheel.\n",

        # user_interrupt.py — terminal interaction
        "interrupt.pause_detected":
            "[Interrupt] / detected — Agent will pause after the current operation. "
            "Enter a command and press Enter, or press Enter alone for help:",
        "interrupt.ack":           "[Interrupt] {name} received — will take effect after the current tool call.",
        "interrupt.webcmd":        "[Web dashboard] Injecting command: {cmd}",
        "interrupt.pause":         "[Interrupt] /pause received — Agent will pause after the current operation.",
        "interrupt.pause_awaiting": "Agent paused. Tell me what to do next, or type /exit to quit.",
        "interrupt.stop":
            "[Interrupt] /stop applied: current tool will be terminated, Agent continues. "
            "(Use /exit to quit the program)",
        "interrupt.exit":          "[Interrupt] /exit: Agent is about to quit.",
        "interrupt.newtask_usage": "[Interrupt] Usage: /newtask <new goal>",
        "interrupt.newtask_done":  "[Interrupt] New goal injected: {arg}",
        "interrupt.inject_usage":  "[Interrupt] Usage: /inject <message>",
        "interrupt.inject_done":   "[Interrupt] Message injected — LLM will see it next turn.",
        "interrupt.compress":
            "[Compress] Marked: context will be compressed before the next LLM call, "
            "keeping the latest {keep} (currently {before}).",
        "interrupt.rigor_on":      "[Interrupt] thought rigor mode ON (observe→infer→decide); effective next turn.",
        "interrupt.rigor_off":     "[Interrupt] thought rigor mode OFF; effective next turn.",
        "interrupt.rigor_usage":   "[Interrupt] Usage: /rigor on|off (current: {state})",
        "interrupt.add_iters":     "[Interrupt] Added {n} iterations — queued total: {total}.",
        "interrupt.add_iters_usage":"[Interrupt] Usage: /+<positive int>, e.g. /+50",
        "interrupt.unknown_cmd":   "[Interrupt] Unknown command: {name}. Type /help for available commands.",

        # user_interrupt.py — /status display
        "status.header":           "[Status]  Iter: {i}  Tools: {tools}  Long-term: {lt}",
        "status.current_tool":     "  Current tool: {tool}  Elapsed: {elapsed}",
        "status.idle":             "  Current tool: (idle)",
        "status.scratchpad":       "Scratchpad:",
        "status.truncated":        "\n...[truncated]",

        # user_interrupt.py — /log display
        "log.header":              "[Log] Last {n} / {total} entries",
        "log.tool":                "🔧 [#{i}] Tool: {tool}",
        "log.done":                "✨ [#{i}] Done",
        "log.thought":             "💭 [#{i}] Thought",
        "log.result_tag":          "📥 Result",

        # user_interrupt.py — HELP_TEXT
        "interrupt.help": """\
[User Commands] - type / to trigger:
  /help              Show this help immediately (without waiting for the current tool)
  /pause             Pause after the current operation and wait for your next instruction
  /stop              Terminate the current tool; Agent continues to the next step
  /exit              Quit the Agent program
  /inject <msg>      Inject a message into Agent context; LLM sees it next turn
  /newtask <goal>    Inject a new goal (nostop mode: unblocks the wait loop)
  /compress [N]      Compress context before the next LLM call (keep latest N, default 8)
  /rigor on|off      Toggle thought rigor mode (observe→infer→decide); effective next turn
  /status            Show current state: iteration, active tool, scratchpad
  /log [N]           Show the last N execution records (default 5)
  /+N                Add N more max iterations (e.g. /+50)
  (/status and /log respond immediately even during tool execution)
Tip: just type / to pause; enter the full command then press Enter.
""",
    },
}

# ── Public API ────────────────────────────────────────────────────────────────

def t(key: str, **kwargs) -> str:
    """Return the localised string for *key*, interpolating any *kwargs*."""
    table = _STRINGS.get(LANG, _STRINGS["zh"])
    s = table.get(key) or _STRINGS["zh"].get(key, key)
    if not kwargs:
        return s
    try:
        return s.format(**kwargs)
    except (ValueError, KeyError, IndexError):
        # 模板含未转义的字面 {/}（str.format 会炸）——降级为逐占位符替换，
        # 翻译层绝不能把可恢复的解析错误升级成致命异常
        for k, v in kwargs.items():
            s = s.replace("{" + k + "}", str(v))
        return s
