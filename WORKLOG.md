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


## 2026-08-28 | DASHBOARD_ALLOW/DENY 访问控制真正落地

### 背景
面板上 IP 白/黑名单此前只存不用（后端无执行代码）——绑定 0.0.0.0 时局域网
任何人都能打开看板（可读取 API key、启停任务）。本次补上真实执行层。

### 改动
- `senza_agent/webserver/app.py`：新增 `_peer_ip` / `_parse_networks` /
  `_acl_allows` / `_acl_middleware_factory`，`create_app` 挂载 ACL 中间件。
  决策规则：本机回环始终放行；DENY 命中即 403（优先于白名单）；白名单非空
  = 只放行名单内；对端 IP 取不到时放行（fail-open，避免误锁）。允许/拒绝
  名单**每请求从 env 重读**——看板保存后立即生效，无需重启。
- `panel.html`：白/黑名单 hint 注明"改完保存即生效"；保存成功消息追加
  "访问控制已启用：名单外/黑名单主机访问将返回 403"。
- `tests/test_webserver.py`：新增 TestDashboardACL 8 个测试（解析、决策
  矩阵、middleware 经真实 socket 的 200/403、env 热更新无需重建 app）。

### 验证
- ACL 测试 8 passed；全量 326 passed / 3 skipped（基线 318 + 新增 8）。
- panel.html node --check 通过。

## 2026-08-28 | 全部品牌图标统一为 OIP 图（预生成多尺寸 ICO）

### 背景
上次 d9a12a4 把 desktop/icon.png 与 LOGO_256.png 换成了 OIP 图（经像素比对与
/data/xuhongming/OIP.webp 内容一致）。但用户反馈任务栏/安装/卸载图标仍是旧图：
其 Windows 安装包是改图前构建的，且 electron-builder 需在构建机上现做 PNG→ICO
转换。本次把 Windows 侧图标源改为预生成的多尺寸 ICO，减少构建期变量。

### 改动
- 新增 `desktop/build/icon.ico`：由 desktop/icon.png（OIP 图）生成，含
  16/24/32/48/64/128/256 七档尺寸（256 档为 PNG 压缩条目，Vista+ 标准）。
- `desktop/package.json`：`win.icon`、`nsis.installerIcon`、
  `nsis.uninstallerIcon` 均指向 `build/icon.ico`（mac/linux 仍用 icon.png）。
  electron-builder 链路（26.15.3）：rcedit 把 win.icon 写进应用 exe 资源；
  NsisTarget 将其设为 MUI_ICON+MUI_UNICON（安装与卸载界面图标）。

### 验证
- ICO 结构解析：7 个条目齐全，最大 256x256（≥256 满足 electron-builder 校验）。
- ico 最大帧回读与 icon.png 逐像素比对：RGBA 平均差 0.0（同一画面）。
- node 侧模拟 resolveSourceFile + getIcoMaxSize：解析到 build/icon.ico，max 256 OK。
- package.json JSON 校验通过；build/ 不在 .gitignore 内。

### 说明
- 面板页左上角 logo 与 favicon（/LOGO_256.png）源文件早已是 OIP 图；浏览器如
  显示旧图属缓存，Ctrl+F5 即可。
- 用户侧要生效需在 Windows 构建机 git pull 后重新打包安装；旧任务栏图标还需
  清 Windows 图标缓存（ie4uinit -show 或删 IconCache.db 重启 explorer）。

## 2026-08-28 | 品牌图统一换源为 OIP(1).jpg（纯图形版）+ 面板 logo 缓存穿透

### 背景
用户指认统一源图应为 /data/xuhongming/OIP(1).jpg（125x131 JPEG，仅蓝色图形标，
无文字），而非此前用的 OIP.webp（图形+SENZA AGENT 文字）。另反馈桌面版菜单栏
下方左侧仍有未替换图标——即页面顶栏 .logo-icon（LOGO_256.png），桌面端 Electron
HTTP 缓存导致看到旧图。

### 改动
- desktop/icon.png、senza_agent/webserver/static/LOGO_256.png：由 OIP(1).jpg
  重做——白底 256x256，图形标等比缩放至 198x208 居中（LANCZOS）。
- desktop/build/icon.ico：由新 icon.png 重建 16~256 七档（256 档与 icon.png
  逐像素 0.0 差）。
- panel.html：favicon 与顶栏 logo URL 加 ?v=oip1 缓存穿透参数，装新版立即生效。

### 验证
- PNG 头/尺寸校验通过；ico 结构 7 条目、最大帧 256≥256。
- 新图与旧 OIP.webp 版逐像素平均差 34.0/255——确为不同构图（无文字版）。
- panel.html 中仅有的两处 LOGO_256 引用均已带 ?v=oip1。

## 2026-08-28 | NSIS 安装器进度/详情功能修正（installer.nsh）

### 背景
desktop/installer.nsh 有一份未提交改动，意图是让 NSIS 安装向导显示安装
进度与详情文本（详情列表框 + 状态栏叙述行）。静态审查发现其中三个 hook
有一个真 bug、两个无效代码，若直接提交会得到"详情框开着但永远没有输出"
的效果，甚至编译报错。

### 问题分析（对照 node_modules 内 electron-builder 26.15.3 模板与
NSIS 编译器源码逐条核实）

- 真 bug：模板 installSection.nsh 第 5-7 行在非静默模式下执行
  `SetDetailsPrint none`。NSIS 源码（script.cpp 的 TOK_SETDETAILSPRINT /
  Ui.c 的 update_status_text）确认 none=6 会同时关闭状态栏与详情列表框
  两条输出通路，DetailPrint 也走同一闸门——因此原 customInstall 里的
  DetailPrint 被完全吞掉。customInit 里的 `SetDetailsPrint both` 在
  .onInit 阶段执行，早于该行，且 both 本就是默认值，等于无效代码。
- 编译错误风险：customPageAfterChangeDir 被 assistedInstaller.nsh 在
  "目录页之后、InstFiles 页之前"的顶层位置 !insertmacro 展开，裸
  DetailPrint 属于运行时指令，出现在任何函数/区段之外无法编译；该 hook
  本意是插入自定义页面。即使包进函数，执行时 InstFiles 页尚未创建，
  update_status_text 因窗口句柄为空直接返回，消息照样丢失。
- 删除安全性：所有 hook 展开点均带 !ifmacrodef 守卫
  （installer.nsi:45-47/79-81、installSection.nsh:81-83、
  assistedInstaller.nsh:42-44），删掉宏定义不会导致编译失败。
- `ShowInstDetails show` 覆盖 common.nsh 的 nevershow：核实 NSIS 源码，
  ShowInstDetails 只是清位再置位，重复指定不产生任何警告，而
  electron-builder 以 -WX（警告即错误）调 makensis——确认无风险。
  用户 include 由绝对路径引入，仅展开一次；installSection.nsh 第 1 行
  的同名 !include 按 !addincludedir 顺序解析到模板自带的
  include/installer.nsh，与用户文件同名但不同物，无重复定义。

### 改动
- 删除 customInit（无效代码）与 customPageAfterChangeDir（裸运行时指令
  无法通过编译）。
- customInstall 开头加 `SetDetailsPrint both`：该 hook 是主安装区段最后
  一条语句，之后无其他输出需要压制，恢复默认输出模式后 DetailPrint
  能同时写入状态栏与详情列表框；静默安装时模板的 none 不执行，此行
  重申默认值，无副作用。
- customHeader（ShowInstDetails show / ShowUnInstDetails show）与
  customUnInstall（卸载时先输出提示再删 python_venv）核实无误，保留。
- 修正头部设计注释：逐文件提取行仍被模板的 SetDetailsPrint none 隐藏，
  详情框实际显示的是 customInstall 的收尾叙述行。

### 验证
- 无 Windows/makensis 环境，走静态验证：逐行核对最终脚本拼装顺序
  （NsisTarget.computeCommonInstallerScriptHeader + computeFinalScript）、
  各 hook 展开点与守卫、SetDetailsPrint/DetailPrint 的运行时语义
  （NSIS script.cpp/Ui.c 源码级确认）。
- 汇总运行时序：GUI 安装 = 详情框可见、File /r 期间进度条正常推进
  （逐文件行被隐藏）、末尾显示收尾叙述；静默安装无输出（符合预期）；
  卸载 = 显示"Removing Python virtual environment..."后删 venv。
- 仓库仅 desktop/installer.nsh 与 WORKLOG.md 变更；desktop/build/icon.ico
  等此前成果未受影响。

### 说明
- 已知取舍：安装过程详情框只有一行收尾叙述，看不到逐文件提取行——
  模板在 installApplicationFiles 之前没有提供可恢复输出的 hook，
  属 electron-builder 模板限制，非本文件缺陷。
- 本机为 Linux，无法运行 makensis 实际打包；下次在 Windows 构建机
  build:win 时请留意 makensis 输出是否有意外告警（-WX 模式下会报错）。

## 2026-08-28 | NSIS 脚本 makensis -WX 真机编译验证（Linux 交叉验证）

### 做了什么
兑现上一条日志的遗留项：在 Linux 上对 electron-builder 26.15.3 真实拼装的
NSIS 安装器脚本做 makensis 编译验证（而非仅静态分析），验证
desktop/installer.nsh（commit c413135）在 -WX 模式下 0 告警 0 错误。

### 方法
- 无 wine、系统 glibc 2.28。electron-builder 默认 nsis@1.2.1 bundle 的
  linux/x64/makensis（NSIS 3.12）按 GLIBC 2.33/2.34 编译，本机无法直接运行
  （GLIBCXX/GLIBC 版本不足；conda 的 libstdc++ 只解决一半，libc 无法补）。
  Docker/podman 在该 NFS 老内核环境不可用（vfs 层目录 0555 + pivot_root
  必然 EACCES）。
- 改用 toolsets.nsis = "0.0.0" 的 legacy 工具集：nsis-3.0.4.1 +
  nsis-resources-3.4.1（npmmirror 下载，sha256 与 windows.js 内置校验和
  完全一致），其 linux/makensis 按 glibc 2.6.32 构建，本机直接可跑。
- 通过环境变量把工具集指到本地：ELECTRON_BUILDER_NSIS_DIR（含
  linux/makensis + elevate.exe + plugins/）、ELECTRON_BUILDER_7ZIP_PATH、
  electron zip 预热 @electron/get 缓存（npmmirror，sha256 校验通过）。
- 在 NODE preload 里打两个运行时补丁后跑真实 build()（win: nsis, x64,
  signAndEditExecutable:false, electronDist 指向本地 zip）：
  1) WineVmManager.exec → 不跑 wine，直接在 Z: 换算路径写一个 514 字节
     的占位卸载器（pass2 只做 File 内嵌，编译期不解析 PE）；
  2) NsisTarget.executeMakensis → 落盘两遍完整脚本与 makensis 命令行。
  其余流程（配置校验、asar、资源拷贝、7z 打包、压缩、installApplicationFiles
  内嵌）全部为 electron-builder 原生代码。

### 验证结果
- 真实 build 全程退出码 0：makensis pass1（BUILD_UNINSTALLER）与 pass2
  （最终安装器，内嵌 288.7MB 7z 与卸载器）均带 -WX，0 错误 0 告警，
  产出 289,840,786 字节的 SenzaAgent Setup 0.1.0.exe。
- 拼装脚本确认：用户 include 以绝对路径展开
  （!include ".../desktop/installer.nsh"），位于 addplugindir 之后、
  common.nsh/MUI2 之前；customHeader/customInstall/customUnInstall 三个
  hook 经 !ifmacrodef 守卫插入。
- 对照实验（同一脚本重放）：
  1) 原样重放 pass1/pass2 → 均 exit 0；
  2) 把 customInstall 体内 DetailPrint 换成未定义变量 ${...} → 编译失败于
     installApplicationFiles 展开内，证明 customInstall 确实被编入 section
     末尾（即 c413135 修复的生效路径）；
  3) 把 customInstall 改名 → 仍编译通过，证明 !ifmacrodef 守卫行为正确。
  即：上一轮静态分析的结论（hook 位置、ShowInstDetails show 覆盖
  nevershow 在 -WX 下安全）全部得到编译器实证。

### 结论与遗留
- 上一条日志的担心可以放下：installer.nsh 在 -WX 下编译干净，Windows
  构建机 build:win 不应出现本文件引发的告警。
- 本验证中 wine 被替换为占位卸载器，故产物仅证明"脚本可编译"，
  不能运行；真实发布仍以 Windows 构建机为准。
- Windows 构建机上若也遇到 glibc 类问题不存在（win32 用 makensis.exe）；
  但注意 package.json 声明 electron-builder ^25.0.0 而本机实装 26.15.3，
  Windows 端装到哪个版本以实际 npm ci 结果为准（hook 机制两版本一致）。


## 2026-08-28 | 参照 Folumi 打通 GitHub Actions 自动发布 exe 安装包

### 做了什么
参考 oh-my-harness/Folumi 的 release-desktop.yml（tag 触发 →
windows-latest 构建 → softprops/action-gh-release 上传 Releases），
改造本仓库 .github/workflows/build-windows.yml。

### 关键发现与修复
- 旧 workflow 的产物路径 `desktop/dist/SenzaAgent-Setup-*.exe` 与
  electron-builder 默认输出名 `SenzaAgent Setup 0.1.0.exe`（含空格）
  不匹配——artifact 上传和 Release 发布都会失败。这是"从未真正发过包"
  的根因。
- package.json 的 build.nsis 增加
  `"artifactName": "${productName}-Setup-${version}.${ext}"`，
  产出确定为 `SenzaAgent-Setup-0.1.0.exe`（无空格，CI glob 可靠命中）。
- package.json 把 electron-builder 从 `^25.0.0` 钉到 `26.15.3` 并重新生成
  package-lock.json：本地 node_modules 实装、且经过 -WX 编译验证的就是
  26.15.3；旧 lockfile 钉在 25.1.8，CI 上 npm ci 会装到未经验证的模板版本。
  CI 与已验证环境从此一致。
- workflow 对齐 Folumi 的做法：node 22 + npm ci 缓存、tag 与 package.json
  版本一致性校验（版本不匹配直接报错退出）、`--publish never` 禁用
  electron-builder 自带的 GitHub 发布（避免与 softprops 重复发布冲突）、
  release 资产命名 `SenzaAgent-v<ver>-windows-x64-setup.exe`、
  fail_on_unmatched_files + generate_release_notes。
- 触发方式与 Folumi 一致：push `v*` tag 自动构建并发布 Release；
  workflow_dispatch 手动触发只出 artifact 不发布。

### 验证
- 本机用真实 electron-builder 26.15.3 跑了完整 NSIS 管线（wine 桩）：
  -WX 两遍编译 0 警告，产出 289,840,786 字节的
  `SenzaAgent-Setup-0.1.0.exe`，确认 artifactName 生效。
- 直接 CLI 方式（无 wine 桩）如预期失败于卸载器 wine 步骤，属本机环境
  限制；GitHub windows-latest runner 自带 wine，不受影响。
- workflow YAML 解析通过；desktop/dist 产物确认被 .gitignore 覆盖。

### 使用方法
```
git tag v0.1.0 && git push origin v0.1.0
```
Actions 自动构建约 5-10 分钟，产物出现在 GitHub Releases（含自动
release notes）。手动测试：Actions 页面 Run workflow，到 Artifacts 下载。

## 2026-08-28 | 首个自动发布 Release 跑通（v0.1.0）

### 做了什么
- 将本地旧 v0.1.0 tag（指向旧提交 1f30aa9，未发过 Release）重打到 HEAD 后推送，
  触发新 workflow。
- 首次 CI 失败：构建本身成功（exe + blockmap 已产出），崩溃发生在发布元数据
  阶段——updateInfoBuilder 的 computeChannelNames 对 null publishConfig 读
  .channel。根因：package.json 没有 repository 信息 + --publish never 时
  publish 配置为 null。
- 修复：package.json 顶层补 npm 标准 repository 字段（注意：放 build. 内会
  被 schema 拒绝）；发布改用 electron-builder 原生 onTag 策略（草稿 Release，
  自动含 exe + blockmap + latest.yml），移除 softprops 二次发布；手动触发
  仍 --publish never。本机 stub 构建 -WX 0 警告复验通过后提交 7752479。
- 第二次 CI run 成功（run 33155966808）。

### 结果
- Release: https://github.com/oh-my-harness/senza-agent/releases/tag/v0.1.0
  资产：SenzaAgent-Setup-0.1.0.exe（78.0 MB）+ latest.yml。
- 清理：失败 run 留下的只有 blockmap 的孤儿草稿 Release 已删除；正式草稿
  已手动 publish（electron-builder 默认发草稿，发布按钮在 Releases 页）。

### 踩坑与结论
- electron-builder 的 publish 元数据生成即使 --publish never 也会走
  blockmap → updateInfo 路径，缺 repository 信息直接 TypeError。repository
  必须放 package.json 顶层，不是 build. 下。
- 首次失败 run 因产物命名/发布流程变化留下垃圾草稿，处理 tag 重打/失败重跑
  时要检查 Releases 页草稿列表。
- 以后发版流程：改 desktop/package.json version → commit →
  git tag vX.Y.Z && git push origin vX.Y.Z → CI 构建草稿 Release →
  Releases 页点 publish。

## 2026-08-28 | 面板新增"工作目录"设置项（运行参数页，热生效）

### 本次做了什么

用户问"桌面版怎么切换工作目录"，调查结论：working_dir 此前只能手改 `~/.senza-agent/config.json` 的 `working_dir` 字段或环境变量 `SENZA_AGENT_WORKING_DIR`，面板无入口。本次把它接入设置面板：

- **后端** `senza_agent/webserver/qevos_bridge.py`：
  - `_api_env_get` 增加 `SENZA_AGENT_WORKING_DIR` 字段，报告实际生效值（config/env 叠加后，未设置回落 `os.getcwd()`）。
  - `_api_env_post` 保存前校验：`os.path.expanduser` 后必须 `os.path.isdir`，否则 400 拒绝（防止保存后 agent 每次工具调用报错）；传空=清除，回落启动目录。
- **前端** `panel.html`：设置 → 运行参数页新增"工作目录"文本框（label 带 hint：SENZA_AGENT_WORKING_DIR、留空=启动目录、不存在会被拒绝）；打开设置时从 `/api/env` 回填；保存时随 payload 提交。
- **生效机制（零后端状态新增）**：复用现有 settings.json 键值机制——保存 → `save_settings()`（写 settings.json + `os.environ`）→ 空闲时 `rebuild_harness()` → `load_config()` 的 `_apply_env` 读到该变量 → `create_agent` 用新 working_dir 重建。任务运行中保存则推迟到任务结束（`pending_rebuild`）。优先级不变：环境变量 > config.json > cwd。

### 验证结果

- 起真实 dashboard（隔离 HOME，端口 8791）跑通全链路：GET 回填 ✓；POST 不存在目录 → 400 `工作目录不存在或不是目录` ✓；POST 合法目录 → `{"ok":true,"rebuilt":true}` ✓；再 GET 返回新值 ✓；settings.json 持久化 ✓；POST 空串 → 键删除、回落 cwd ✓。
- 单独验证启动链 `load_settings_into_env() → load_config()`：重启后 working_dir 仍是保存值 ✓。

### 使用方法

桌面版/看板：设置 → 运行参数 → 工作目录，填绝对路径（支持 `~`），保存即时生效（任务运行中则任务结束后生效）。留空 = 回落到后端启动目录。

## 2026-08-28 | v0.1.1 发布（含面板"工作目录"设置项）

### 本次做了什么

版本号 0.1.0 → 0.1.1（`desktop/package.json`），打 tag `v0.1.1` 触发 CI（run 33158131224，成功，约 2 分钟）。electron-builder 自动创建 draft Release，手动 publish（id 378370587）并补了 release notes；顺手删掉了仅含 .blockmap 的孤儿 draft（378370588）。

### 验证结果

- Release 页公开可见：`SenzaAgent-Setup-0.1.1.exe`（81,832,495 B）+ `latest.yml`（348 B）。
- 发布流程与 v0.1.0 完全一致，无新坑。

### 使用方法

下载 https://github.com/oh-my-harness/senza-agent/releases/tag/v0.1.1 覆盖安装即可；设置 → 运行参数 → 工作目录 可直接切换 Agent 工作根目录。

## 2026-08-28 | v0.1.2：修设置面板 "web is not defined" + 工作目录可视化选择 + 桌面自动更新

### 本次做了什么

1. **修复 v0.1.1 设置面板报错**「无法读取当前配置（web is not defined）」：前次插入 setWorkingDir 回填行时误删了 `const web = env.web || {}` 声明行，openSettings 抛 ReferenceError 落入 catch，被误报成"服务端旧版本"。已恢复声明行，且此错误信息现在只在真正 404/断连时出现。
2. **工作目录可视化选择**：desktop/preload.js 暴露 `senzaDesktop.pickFolder()`（IPC `desktop:pick-folder` → 主进程 `dialog.showOpenDialog({openDirectory, createDirectory})`）；panel.html 在工作目录输入框旁加「浏览…」按钮，仅在桌面版（检测 `window.senzaDesktop`）显示，纯浏览器隐藏。
3. **桌面版自动更新**（electron-updater 6.8.9）：
   - main.js `setupAutoUpdate()`：打包后启动时 + 每 6h `checkForUpdates()`，GitHub Releases 匿名拉 latest.yml（公开仓库免 token），`autoDownload` 后台下载，`autoInstallOnAppQuit` 兜底。
   - 下载完成 → preload `onUpdateDownloaded` → panel 右下角弹持久提示条（新版本 v + 「下次启动时」/「立即安装」），「立即安装」走 `desktop:install-update` IPC → `quitAndInstall(false, true)`（向导式 + 装完自启）。
   - npm install 首次超时（300s），重跑 `--prefer-offline` 2s 完成；electron-updater ^6.8.9 已入 package.json + lockfile。

### 验证结果

- main.js/preload.js `node -c` 通过；panel.html 内联 JS `new Function()` 解析通过（源文件 + 实际伺服内容均验证）。
- 起 dashboard 实测：served panel 含 `const web`/浏览按钮/更新横幅三处；/api/env 全链路（回填/保存合法目录/400 拒绝不存在目录/清空回落）照旧 ✓。
- 无显示器环境无法点原生目录对话框与更新流程 UI；这两条依赖 Windows 实机/GH runner 验证。

### 踩坑与结论

- edit 工具 SWAP 跨行时把 `const web` 声明当上下文吞掉 → v0.1.1 带病发布。教训：改 settings 回填块时，声明行与赋值行是独立行，SWAP 范围必须逐一核对。
- electron-updater 对 GitHub Releases 的 latest.yml 结构有要求（version/files/sha512），electron-builder 26.15.3 默认产物即兼容，无需额外配置。

### 使用方法

升级到 v0.1.2 后：设置 → 运行参数 → 工作目录 → 「浏览…」可视化选择；新版本发布后桌面版自动在右下角提示，点「立即安装」即重启进入安装向导。

## 2026-08-28 | v0.1.2 发布完成

CI run 33159967448 success（~1 分钟）；draft 378384199（exe + latest.yml）通过 API PATCH draft:false 公开发布，孤儿 draft 378384198（仅 blockmap）已删除（204）。公开发布时间 2026-08-28T09:37:22Z，latest.yml 已报 0.1.2（sha512 + blockmap 具备差量更新条件）。URL: https://github.com/oh-my-harness/senza-agent/releases/tag/v0.1.2
遗留项：目录选择对话框与更新横幅的 UI 级验证需 Windows 实机（本机无 X server，无法驱动 Electron 渲染进程）；卸载不清理 ~/.senza-agent（含 API key、会话数据），维持现状。
