# 工作日志

## 2026-08-27 | SKILLS 目录布局迁移 + karpathy-guidelines skill + config.py 默认路径修复

### 本次做了什么

1. **SKILLS 格式迁移**：SDK（senza / llm-harness）只认 `<name>/SKILL.md` 目录布局，`load_skills` 对平铺 `.md` 静默返回空列表（只发 warning）。把原有 6 个平铺文件迁移为目录布局，并新增 `karpathy-guidelines`（上游 multica-ai/andrej-karpathy-skills，2518B 逐字副本）：
   - `coding.md` → `coding/SKILL.md`（frontmatter name: `coding`）
   - `data_analysis.md` → `data_analysis/SKILL.md`（name: `data-analysis`）
   - `web_research.md` → `web_research/SKILL.md`（name: `web-research`）
   - `tscircuit.md` → `tscircuit/SKILL.md`（name: `tscircuit`）
   - `kicad_mcp.md` → `kicad_mcp/SKILL.md`（name: `kicad-mcp`）
   - `ui_app.md` → `ui_app/SKILL.md`（name: `ui-app`）
   - `SKILLS/karpathy-guidelines/SKILL.md`（name: `karpathy-guidelines`，MIT）

   **关键决策**：目录名保留下划线（与 `cli.py --skills` 帮助文本 `coding,data_analysis` 一致），但 frontmatter `name` 必须用连字符——SDK `is_valid_skill_name` 只允许 `[a-z0-9-]`。正文逐字保留（已用 `git show HEAD:SKILLS/<f>.md` 逐一比对确认），仅添加 frontmatter（name + description 一行）。

2. **config.py `_apply_derived` 修复**（已征得用户同意；config.py 属核心文件）：原实现用相对路径 `Path("senza-agent")/SKILLS` 推导默认 `skills_dir`，只有 cwd 为 `~/` 时才解析成功；从仓库根 cwd 运行时 `skills_dir=''` → skills 根本不加载。修复后按优先级尝试：
   1. `$SENZA_AGENT_DIR/SKILLS`（desktop/main.js 与 CLI 包装器会设置该环境变量）
   2. `<repo root>/SKILLS`（按 `__file__` 定位，与 cwd 无关）
   3. `./senza-agent/SKILLS`（保留旧行为兜底）

### 验证结果

- `senza.load_skills('SKILLS')` → 7 个 skill 全部加载（coding, data-analysis, web-research, tscircuit, kicad-mcp, ui-app, karpathy-guidelines）。
- `load_config().skills_dir` 三场景实测：仓库根 cwd ✓、`~` cwd ✓、`SENZA_AGENT_DIR` 优先级 ✓；端到端 config → load_skills 链路 7/7。
- `pytest tests/`：247 passed；3 个 FAILED（`test_context_injector.py` ×2、`test_integration.py` ×1，均为 `create_tool() got unexpected keyword 'parameters'`）与 `test_web_app.py`/`test_webserver.py` 收集失败（缺 aiohttp）**均为存量问题**——已用 `git stash` 在改动前的工作树上复现同样的失败确认。

### 踩坑与结论

- SDK 的 `<available-skills>` 系统提示段：`format_skills_for_system_prompt()` 在 SDK 中已导出但**当前无调用方**——skill 注册后生效的是自动注入的 `skill_read` 工具（`skill_read(skill_name, path?)`），模型通过该工具按需读 skill 全文；`<available-skills>` 段落需应用层自行注册 before_run hook 拼入系统提示（senza-agent 目前没做，属后续可选增强）。
- 面板（webserver）的 `skills/` 小写目录 + `/api/skills` 平铺读写是另一套独立体系，其 skill 同样无法被 SDK 加载；`panel.html` launch 发送的 `skills: [...gActiveSkills]` 在 `_api_launch` 中被忽略。均未纳入本次范围。
- `cli.py --skills` 参数只写 `SENZA_AGENT_SKILLS` 环境变量，全仓库无消费方（死管道），未动。
- venv（3.13）无 pytest；系统 python3 是 3.6 跑不了本仓库测试。用 conda env `guiarcgen`（pytest 9.1.1 + PYTHONPATH 指向仓库）跑通。

### 遗留项 / 下一步

- （可选）为 SDK skills 注册 before_run hook，把 `format_skills_for_system_prompt` 的 `<available-skills>` 段拼进系统提示——当前模型只能"盲猜"调用 skill_read，还是需要在提示里列出可用 skill 名。
- （可选）统一面板 `skills/` 平铺体系与 SDK 目录布局，或让 `_api_launch` 消费 `skills` 字段。
- omp 用户级 skills 已装 `~/.omp/agent/skills/karpathy-guidelines/SKILL.md`，下个新会话生效。
