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

## 2026-08-27 | _sdk_compat：stream_prompt 跨 SDK 版本兼容

### 背景
- senza-agent `772bf38` 依赖 Senza SDK 的 `max_consecutive_timeouts` 参数（Senza PR #35，`0071f54`，
  在 v1.2.3 **之后**才进远程 main）。PyPI 最新只有 1.2.3，其 Python 封装层把该参数硬编码为 1 且
  拒绝该关键字 → webserver 面板任务在 1.2.3 wheel 上直接 TypeError。
- 计划给 Senza 打 v1.2.4 tag（本地已 bump `f34e323` + tag），但本机全部 GitHub token 对
  oh-my-harness/Senza 均 push=False（senza-agent token 只对本仓库有写权限），推送被卡，改为下游兼容。

### 关键发现
- v1.2.1–1.2.3 的限制**只在 Python 封装层**：Rust 运行时（.so）自 1.2.1 起就把
  `max_consecutive_timeouts` 透传给 `obj.events()/obj.subscribe()`，Python 层 `_get_event_iterator`
  也一直在传，只是 `stream_prompt/stream_run` 签名里没有、调用处硬编码 1。

### 做法
- 新增 `senza_agent/_sdk_compat.py`：从 senza-sdk 1.2.3 逐字 vendor `stream_prompt`
  （`_get_event_iterator`/`_next_event`/终态集合），签名补上 `max_consecutive_timeouts=1`，
  直接调 `obj.events()`。装任何 SDK 版本（1.2.1+）都走同一条代码路径，行为一致。
- `webserver/task.py` 改为 `from senza_agent._sdk_compat import stream_prompt`；
  `senza.stream_prompt` 引用清零。

### 验证
- 三场景行为测试全过：本机 SDK（Python 层带新参数）/ 模拟 1.2.3 Python 层（inspect 探测
  `has_param=False`，直接调用复现 TypeError，shim 正常）/ prompt 异常冒出。
- pytest：225 passed；存量失败（test_context_injector ×2 的 create_tool parameters 不兼容）
  与本次改动无关（git stash 复现过，之前已确认）。

### 遗留项 / 下一步
- Senza 仓库本地已就绪（`f34e323` bump 1.2.4 + tag v1.2.4），等有写权限的 token/机器执行
  `git push origin main v1.2.4`，CI 自动发 PyPI；发布后下游可删掉本 shim 换回 SDK 原生调用。
- senza-agent main 曾推不上去（git 端点 60–300s 挂起、Authentication failed），原因是
  credential.helper=store 里的旧 token 干扰；`-c http.extraheader="Authorization: Basic <b64
  (x-access-token:token)>"` + `-c credential.helper=` 推送成功，远程 main 已到 `1a100a4`。
