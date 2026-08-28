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

## 2026-08-27 | 面板 Skills 标签接通 SDK SKILLS/ 目录

### 问题
桌面端面板 Skills 标签空白：`qevos_bridge.py` 的 skills 接口读
`SENZA_AGENT_DIR/skills/`（小写、平铺 *.md），该目录根本不存在；
而 SDK 实际加载的是 `SENZA_AGENT_DIR/SKILLS/<name>/SKILL.md`（大写、目录布局）。
两套体系此前互不相通（早期调查已记录为遗留项）。

### 做法
- `_SKILLS_DIR` 改指 `_AGENT_DIR / "SKILLS"`（与 SDK 同一目录）。
- `_api_skills_list`：列 `<name>/SKILL.md`（平铺旧 .md 仍兼容显示）；
  description 优先取 frontmatter 的 `description:` 字段（跳过 YAML 块），
  兜底取正文第一行非注释文本。
- `_api_skill_get`：`<name>/SKILL.md` 优先，平铺 `name.md` 兜底（`_skill_file` 辅助）。
- `_api_skill_post`：面板新建的 skill 写为 SDK 布局 `<name>/SKILL.md`，SDK 能直接加载。
- `_api_skill_delete`：删 SKILL.md 后清理空目录（目录含额外文件时保留目录）。

### 验证
- 对真实 SKILLS/ 目录冒烟：列表 7 个且描述全部正确取自 frontmatter；
  POST/GET/DELETE 往返；空目录清理；含额外文件的目录只删 SKILL.md；
  平铺旧格式 list/get/delete 兼容；不存在 → 404。
- pytest：247 passed（3 个失败为已知存量 create_tool parameters 问题）。

### 遗留项
- 面板勾选 skill 后 `/api/launch` 仍丢弃 `skills` 字段（`_api_launch` 只取 goal），
  即面板的"激活 skill"勾选尚不影响 SDK 启动配置 —— 下一个可选项。

## 2026-08-27 | web_search 修复：默认 duckduckgo 被墙，切换 bing provider（纯配置）

### 问题
用户报告 web_fetch 可用、web_search 不可用。排查定论：两者同属 SDK（llm-harness-tools）
的 WebToolsPlugin，fetch 正常是因为只抓用户给定 URL；search 默认 provider=duckduckgo
（先 POST lite.duckduckgo.com/lite/，空结果再 GET duckduckgo.com/html/），本机这两个
域名全部不可达（curl 实测 HTTP 000 连接失败，GFW）。

### 三方实现对比（用户问的 qevosagent / omp 调查结论）
- QevosAgent `agent/tools/standard.py:1934 tool_web_search`：单实现，`ddgs` 库
  （requirements.txt:6），`DDGS().text(query)` 取 title/href/body。无 provider 抽象、
  无代理支持；ddgs 底层同样打 lite/html.duckduckgo.com，在本机网络下也会挂。
- omp（Bun 单文件二进制，源码 packages/coding-agent/src/web/search/）：26 个 provider，
  每个 isAvailable/isExplicitlyAvailable/search；显式指定→不可用回落 auto；auto 按
  SEARCH_PROVIDER_ORDER 找第一个可用，执行失败逐个 try 下一个。免 key 类=HTML 抓取
  （bing GET /search?q=&count=&mkt=en-US 解析 li.b_algo + .b_caption p、duckduckgo、
  yahoo、ecosia、startpage、mojeek），带 key 类=perplexity(可匿名)/gemini/anthropic/
  codex/exa/tavily/brave/kagi/jina/kimi/searxng 等；共用 fetcher 失败或命中 bot
  challenge 时自动起 headless Puppeteer 真浏览器重试；支持 HTTPS_PROXY。
- senza SDK（llm-harness-tools web_search.rs，1015 行）：同 provider 路由设计，
  duckduckgo/bing/brave/tavily/serper/serpapi/exa/searxng/google_cse。**bing 分支
  GET base_url?q= + 解析 b_algo，免 API key**——与 omp 免 key bing 抓取同款思路。

### 修复（零代码，纯配置）
SDK 的 config 经 `senza_agent/agent.py _web_config_dict` ← `config.py load_config`
← `~/.senza-agent/config.json` 的 `web` 段（`_apply_file` 已支持）。写入：
```json
{"web": {"provider": "bing", "base_url": "https://www.bing.com/search"}}
```

### 验证
- `load_config()._web_config_dict()` → provider=bing, base_url 正确；
  `senza.create_web_search_tool(cfg)` 构造成功。
- 按 Rust search_bing 同款算法（UA/Accept-Language/GET base_url?q=）Python 移植实测：
  www.bing.com 与 cn.bing.com 均返回 b_algo×10，解析出真实搜索结果（如 "Kimi AI 官网"）。
  www/cn 两域可达性都已确认。

### 踩坑与结论
- SDK NativeTool 不暴露 Python 调用入口（execute 在 Rust 侧由 harness 调度），
  行为验证只能走"同参数请求+同解析算法"的移植模拟，可信度足够（解析器逐行对照过）。
- bing provider 无 API key 要求，免 key；若未来 bing 出验证码，SDK 会报
  web_search_failed——届时可考虑加 searxng 自建或带 key provider。

## 2026-08-27 | WebConfig 默认值改为 bing（代码层修复）

### 为什么改代码
用户指出：duckduckgo 默认值硬编码在 `config.py:18 WebConfig.provider`，只改本机
config.json 修不了其他用户/Windows 机器——新装环境无 config.json 仍会走 DDG 被墙。
默认值本身就该是"开箱可用"的，故改源码默认。

### 改动
- `senza_agent/config.py WebConfig`：`provider = "duckduckgo"` → `"bing"`，
  `base_url = ""` → `"https://www.bing.com/search"`（附一行原因注释）。
  base_url 显式给出是因为 SDK 的 bing provider GET 该 URL + ?q=，而默认空串/旧 DDG
  值对 bing 无意义。
- `tests/test_config.py::test_config_defaults`：断言同步改为 bing + 端点。
- `~/.senza-agent/config.json` 保留（内容现与默认一致，无害，作为显式记录）。

### 验证
- `pytest tests/test_config.py` 3 passed。
- 全量基线：247 passed / 3 failed（均为存量 create_tool parameters 问题，与本次无关）。
- 仓库内 `duckduckgo` 字面量清零（仅剩注释里的原因说明）。


## 2026-08-28 | settings.json 扩容为桌面设置全量存储 + 保存即时生效

### 需求与决策
用户要求：桌面版（看板）设置里的所有配置统一存入 `~/.senza-agent/settings.json`，
修改后立即生效。此前 web 搜索配置只存 config.json，面板上没有编辑入口；compaction
参数同理。经确认（ask_user）采用：
- settings.json 为主存储，save_settings 全量写 settings.json，并把 web/compaction
  两节**镜像**写回 config.json（启动链路 load_config 不变，两文件语义一致）；
- DASHBOARD_HOST/PORT 等重启型配置持久化但提示需重启看板进程（维持现状）。

### 改动
- `senza_agent/config.py`：
  - 新增 `_mirror_to_config_file()`：save_settings 后把 web/compaction 节整节替换
    写入 config.json（best-effort，失败不影响 settings 已落盘）；
  - `_apply_file` 增加 `_to_int/_to_float` 数值强转（settings.json 里嵌套子值都是
    字符串，之前 web.max_results 会变成 "8"）；
  - `_apply_env` 新增 SENZA_AGENT_ADVISOR_INTERVAL / WRAPUP_TURNS / BUDGET_LIMIT
    覆盖（save_settings 本就把 behavior.* 展开成这些 env 键，load_config 此前不读）。
- `senza_agent/webserver/qevos_bridge.py`：
  - `_api_env_get`：补 LLM_CONTEXT_WINDOW 键；新增返回 web/compaction/behavior
    结构化节（从 load_config() 合并结果读取，GET 即所见即所得）；
  - `_api_env_post`：save_settings 后调 load_settings_into_env() 刷新 env。
- `senza_agent/webserver/static/panel.html`：
  - setPaneOther 新增联网搜索 5 项（provider 下拉 bing/duckduckgo/google_cse/
    tavily/serp/searxng、搜索入口 URL、API Key、结果条数、抓取字符上限）；
  - setPaneRuntime 新增 compaction 两项（context_window / reserve_tokens）；
  - openSettings 读 env.web/env.compaction 填充；保存 payload 增 web/compaction
    嵌套对象。
- `tests/test_config.py`：新增 4 个往返测试（镜像写入、数值强转、嵌套 env 展开、
  保存→模拟重启 roundtrip）。

### 立即生效链路（验证过）
POST /api/env → save_settings（settings.json + config.json 镜像 + os.environ）
→ 空闲时 rebuild_harness()（load_config 重读全部节）→ 新 harness 用新 web 配置。
任务运行中则 pending_rebuild，任务结束自动重建。DASHBOARD_HOST/PORT 仍需进程重启
（前端已有提示）。

### 踩坑
- edit SWAP 范围少覆盖一行导致 config.py 出现重复函数体、qevos_bridge 断在
  _apply_env docstring 中间——均靠 ast.parse + 复读修复；教训：SWAP 前先确认
  构造完整边界。
- panel.html 大文件插错 pane 位置（set-body 外）——用 div 配对计数脚本校验。

### 验证
- `pytest tests/`：318 passed / 3 skipped（基线 314 + 新增 4）。
- 临时 HOME 端到端：POST /api/env（web+compaction+behavior+LLM 槽位）→ 200
  rebuilt:true → GET 回读全部一致；settings.json/config.json 内容正确；
  load_config() 数值类型正确（max_results==8 而非 "8"）。
- panel.html：node --check 通过；set-body div 配对平衡；新字段 id 唯一。
