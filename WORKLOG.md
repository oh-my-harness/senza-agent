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

## 2026-08-28 | v0.1.3：按用户要求移除自动更新，改为手动检查

用户明确"不要自动更新"。变更：
- 删除 setupAutoUpdate（启动+6h 轮询+autoDownload）、preload 的 installUpdate/onUpdateDownloaded、panel 更新横幅——全部回退为无后台行为。
- About senza-agent 改为自定义原生对话框：应用描述（基于 Senza SDK 的开箱即用通用 AI Agent、三端形态、主页链接）+ 三个按钮：检查更新 / 项目主页 / 确定。
- 菜单栏 senza-agent 下新增 Check for Updates…：手动 checkForUpdates（不自动下载）→ 发现新版则询问"下载并安装/以后再说"→ 下载完成再问"立即重启安装/下次启动时"→ quitAndInstall(false,true)。失败弹 warning。_updateBusy 防重入。
- electron-updater 依赖保留（检查/下载/安装仍由它完成），仅去掉自动触发。

验证：node -c 通过；mock electron 测试覆盖四条路径（有新版全流程/已最新/网络错误/About 结构与菜单构建）全部通过；真实 electron 无头启动到 Dashboard 正常。

## 2026-08-28 | v0.1.3 发布完成

CI run 33160675034 success（~2 分钟）；draft 378391523（exe + latest.yml）已公开发布，孤儿 draft 378391524（仅 blockmap）已删除（204）。公开发布时间 2026-08-28T09:47:46Z。URL: https://github.com/oh-my-harness/senza-agent/releases/tag/v0.1.3
注意：latest.yml 仍随 release 发布（electron-builder 固定产物），但 v0.1.3 应用内已无自动更新触发点，该文件仅供手动安装的 electron-updater 工具或未来需要时使用。

## 2026-08-28 | v0.1.4：工作目录显示与运行归档目录修复

### 背景与本次做了什么

用户在桌面版里与内置 Agent 对话时发现两个问题：`get_env_info` 报告的 cwd 是安装目录 `resources\`（而非设置的工作目录），且面板运行归档 `runs/run_*` 落在安装目录里。经核实：前者是 `tool_get_env_info` 直接返回进程 `os.getcwd()`（desktop/main.js 以 `cwd: APP_ROOT` 启动后端导致）；后者是 `qevos_bridge._RUNS_DIR = SENZA_AGENT_DIR/runs`，而 `SENZA_AGENT_DIR` 被固定为安装目录。本次修复：

1. **`tools/standard.py`**：新增 `_effective_working_dir()`，经 `load_config()`（合并 config.json 与 `SENZA_AGENT_WORKING_DIR` 环境变量，与 agent.py 的 `config.working_dir or os.getcwd()` 完全同源）取实际生效目录；`tool_get_env_info` 的 `cwd` 改用它。异常时回落进程 cwd。
2. **`qevos_bridge.py`**：`_RUNS_DIR` 改为 `_resolve_runs_dir()` 动态解析，优先级：显式 `SENZA_AGENT_RUNS_DIR` > `~/.senza-agent/runs`（当 `SENZA_AGENT_DIR` 路径含 `resources` 组件，即打包桌面版）> `<SENZA_AGENT_DIR>/runs`（开发/仓库布局）。`resources` 匹配大小写不敏感（Windows 的 `Resources`）。注意 `_RUNS_DIR` 仍是模块导入时求值——webserver 进程启动后 env 变化不影响已解析值，符合预期。
3. **`cli.py`**：删除 `ensure_env_defaults()` 里 `SENZA_AGENT_RUNS_DIR=./runs` 的 setdefault 与 `DEFAULT_RUNS_DIR` 常量。该变量此前全库无消费方（死配置）；修好桥接后它反而会以 `./runs` 抢占解析优先级、掩盖打包场景，必须删。

### 验证

- 单元：`_resolve_runs_dir` 四场景（仓库布局、`/opt/.../resources`、显式覆盖、自定义 agent dir）+ 空白值忽略，全过。
- E2E（真实启动 `--web` dashboard + `/api/runs-index`）：A 显式覆盖可见 ✓；B 无 env 时仍读 `PROJ/runs`（CLI/开发行为不变）✓；C `SENZA_AGENT_DIR` 指向 resources 时只读 `~/.senza-agent/runs`、不受仓库诱饵目录污染 ✓（首轮 C 失败正是 cli.py setdefault 所致，删除后通过）。
- 工作目录一致性：`SENZA_AGENT_WORKING_DIR=/tmp/senza-wd-e2e` 时 `/api/env` 与 `tool_get_env_info()['output']['cwd']` 一致 ✓。
- `pytest tests/`：326 passed, 3 skipped。

### 踩坑

- cli.py 的 setdefault 是"复活"的死配置：v0.1.3 前无害（无人消费），修复桥接后变成优先级 1 的错误默认。删除死代码要彻底，否则修复时它会反咬。
- Linux `Path` 下 Windows 反斜杠路径是单个 part，测试场景 2b 用 `PureWindowsPath` 验证 parts 拆分才确认 `resources` 检测在真实 Windows 上成立。

### 遗留项

- 桌面端用户升级到 v0.1.4 后，旧安装目录里的 `resources\runs` 归档不会自动迁移（新归档写到 `~\.senza-agent\runs`）。

## 2026-08-28 | v0.1.5：更新下载进度窗口

### 本次做了什么

用户要求"下载更新并安装时提供进度显示"。此前点「下载并安装」后界面无任何反馈（82MB 安装包下载期间静默），用户不知道是否在工作。本次在 `desktop/main.js` 增加：

1. **进度窗口 `showUpdateProgressWindow(version)`**：420×190 无边框深色小窗（复用启动 loading 窗的视觉风格），含标题"正在下载 vX"、蓝色确定性进度条、`pct% (已传/总量 MB, 速度 MB/s)` 文本、以及「取消」按钮。
2. **进度驱动**：electron-updater 原生 `download-progress` 事件（`ProgressInfo{percent,transferred,total,bytesPerSecond}`）→ `executeJavaScript` 更新 DOM；监听器在下载结束/异常/取消后均 `removeListener`，不泄漏。
3. **取消**：窗口按钮经 `ipcRenderer.send('desktop:cancel-update')` → `ipcMain.once` → `CancellationToken.cancel()`；`downloadUpdate(token)` 抛 cancelled 错误被识别（`token.cancelled` 判断）后静默返回，不弹错误框。CancellationToken 从 `electron-updater` 导入（已确认其 JS 导出面包含该类）。
4. **收尾**：下载完成先显示"100% 下载完成，正在准备安装…"再弹重启确认；取消或完成后窗口均关闭。`quitAndInstall(false,true)` 时序不变。

### 验证（/tmp/eb-progress-mock，mock electron + electron-updater）

- happy：进度条 5%→100% 逐步更新、百分比文本正确（MB/s 计算）、cancel IPC 注册、完成后窗口关闭、`quitAndInstall(false,true)` 被调用 ✓
- cancel：下载中途（模拟 60ms 后点击取消按钮走真实 IPC 通道）token 取消、下载中断、窗口关闭、**不弹**重启对话框 ✓
- later：用户选"以后再说"则完全不创建进度窗口 ✓
- 测试侧踩坑：假下载 20ms 就跑完，60ms 才点的取消永远输——把假下载放慢（30ms/步）并在步进间检查 `token.cancelled` 才复现真实竞态下的取消语义。

### 遗留项

- 进度窗口在真实 Windows 上需肉眼确认视觉（无边框窗口无关闭按钮，仅靠取消按钮/自动关闭退出，属预期）。

## 2026-08-31 | senza-sdk 1.3.0 多模态评估：Qevos 识图功能差距对比 + 提 issue #145

### 本次做了什么

评估 senza-sdk 1.3.0（`senza_sdk-1.3.0-cp39-abi3-manylinux_2_28_x86_64.whl`，pin runtime `cb58373`）相对 QevosAgent 识图功能（`tool_load_image` → content_blocks → user 消息注入 → 后端转换）的完成度，并已安装到 guiarcgen 环境。

**1.3.0 已具备（端到端实测通过）**：
- 工具回调返回裸 `Attachment` 或 `Attachment`/`str` 混合 list → 正确转 `DataBlock::Image`
- `senza.image_url(url)`（URL 透传不下载）/ `image_base64(bytes, mime_type)` / `document_url()` / `document_file()`
- 用户侧 `prompt(text, attachments=[...])` / `steer(text, attachments=[...])`
- OpenAI 兼容 wire 格式正确：tool 消息产出 `[{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]`（本地 mock OpenAI SSE 服务验证）
- runtime `ImageSource` 有 `Base64`+`Url` 双变体；adapter OpenAI/Anthropic/DeepSeek 三端均支持

**缺口（共 3 个）**：
1. 主要：工具回调返回 Qevos 风格 dict `{"content":[{"type":"image","media_type","data"}]}` → `parse_tool_result`（Senza `src/core/pytool.rs`）只认 `type:text`，报 `unsupported content block type: image`；且该错误以 tool 消息文本回传给 LLM（实测确认），模型无法自我纠正，工具表现为静默失败。存量 Qevos 风格工具无法平滑迁移。
2. 次要：无 vision 能力探测/降级钩子（Qevos 有错误串启发式识别 + `_strip_vision_blocks` 自愈；senza 无模型 modality 元数据、无 4xx 自动降级）。
3. 提示：Document 块跨 provider 支持不一（GLM/DeepSeek 系多不支持 PDF 输入），缺支持矩阵文档。

**动作**：已在 `oh-my-harness/llm-harness-runtime` 提 issue #145（缺口 1 为主，附复现路径与环境信息）。

### 遇到的问题与结论

- 前一会话遗留的"user_interjection：glm-5.3flash 提示 textonly"问题未定位到出处——搜遍 senza-agent/Senza/QevosAgent 源码、SDK wheel 二进制、会话 meta（记录的是 glm-5.2）均无 "textonly" 字样，疑似指运行界面/宿主层文案或 GLM API 侧报错文案，被用户打断后转向当前对比任务。
- 对 senza-agent 本体的后续改造方向：`tools/standard.py` 的 `tool_load_image`/`tool_load_video` 返回值改为携带 `Attachment`（裸值或 list），`_state.vision_supported` 由错误探测驱动（依赖 SDK 缺口 2 的结构化 error 事件）；`_sdk_compat.stream_prompt` 需加 `attachments` 透传参数（1.3.0 的 `prompt` 签名已支持）。

### 遗留项 / 下一步

- [ ] 等 issue #145 的 dict content-block 支持落地后，改造 senza-agent `tool_load_image` 返回 dict content-block（或先用裸 Attachment 方案提前落地）
- [ ] `_sdk_compat.stream_prompt` 加 attachments 透传
- [ ] vision-unsupported 自愈逻辑（等 SDK 结构化错误事件）

## 2026-08-31 | issue #145 缺口 2/3 修复完成：runtime PR #146（含改动必要性审计）

### 本次做了什么

1. **改动必要性审计**（用户要求"看看改动是不是都是必要的"），对 `/tmp/runtime-pr` 工作树逐项裁剪：
   - **删除**：流中（mid-stream）错误降级分支（原 loop_fn.rs ~555-583）。vision 拒绝是 HTTP 400，发生在响应头阶段（`chat_stream` 返回 handle 之前），流中错误只可能是 StreamIncomplete/Decode/网络断——该分支永远不会命中真实 vision 拒绝，只会在瞬态流错误时误剥历史。
   - **收紧**：连接建立路径的降级触发条件，从"任何错误+历史有图"改为仅 `LlmError::InvalidRequest`（adapter openai/mod.rs:175，400→InvalidRequest）。否则 500/401 等瞬态或认证错误也会触发剥图重试，把故障误伤成历史篡改。
   - **删除**：`vision_removal_notice()` dead-code 访问器（`pub(crate)` 模块内 `pub fn` 宿主不可达）。
   - **保留**：核心 strip 逻辑、VisionDegraded 事件、一次性 guard、4 个集成测试（均为 gap 2/3 必需）。
   - **保留并独立成 commit**：parking_lot 迁移（13 文件，仓库既有约定，rs-parking-lot 规则强制）。
   - 澄清一处历史疑点：async_delivery_integration.rs 的 `buf.push_str(t)` 编译错误是当时 Mutex 类型损坏引发的级联误判——基线（stash 后）clippy 实测干净，`as_str()` 改动属不必要 churn，已在后续测试中自然验证。

2. **验证**：
   - `llm-harness-loop` lib 测试 151 passed（含 vision 5 单测 + loop_fn 4 集成测试）。
   - `cargo clippy --workspace --all-targets` 0 错误 0 警告；`cargo fmt --all` 干净。
   - 全 workspace 测试（`--exclude llm-harness-live-tests`）：17 suite ok；仅 `llm-harness-sandbox::cancellation_storm_stays_bounded` 失败——git stash 基线复现同样失败（全局 ChildReaper + 真实子进程，断言 in_flight<=33，共享机高负载 flaky），与本次改动无关 crate；`--test-threads=2` 时全绿。

3. **提交与 PR**：
   - 分支 `fix/vision-degradation-145` 两个 commit：`1b02d6a` fix(loop)（vision.rs + loop_fn.rs + events.rs，506+）与 `1fc78c8` chore（parking_lot 迁移 13 文件）。
   - **PR #146 已开**：https://github.com/oh-my-harness/llm-harness-runtime/pull/146（Closes #145，缺口 2/3；缺口 1 已由 Senza PR #37 覆盖）。
   - issue #145 已评论 PR 链接：issuecomment-5473700097。

### 踩坑与结论

- **根分区磁盘满**（70G 100%）：`/tmp/runtime-pr/target` 5G 是主因，且 stash 往返时 cargo fingerprint 把空间吃穿导致 `git stash pop` 反复失败。解决：target 迁到 `/data/xuhongming/tmp/runtime-target/target`（15T NFS，75% 使用率），原路径留符号链接——对 cargo 透明。期间清掉 `target/debug/incremental`（2.4G）先解燃眉之急。
- 链接期 `-lsqlite3` 找不到：系统只有 `libsqlite3.so.0`（无 dev 软链），用 `RUSTFLAGS="-L native=/data/xuhongming/.conda/envs/guiarcgen/lib"` 指到 conda 的 libsqlite3.so 解决。
- GitHub 连接间歇性超时（443 连不上），等待 ~2 分钟后自行恢复，重试即可。
- `git stash` 在磁盘满时非常危险：pop 失败后 working tree 可能只剩 untracked 文件。本次靠 `git stash show -p | git apply` 管道恢复全部 16 个文件改动——比重新手写靠谱得多。
- sandbox flaky 定位流程：单测通过→整套重跑通过→基线（stash）复现失败→确认与本改动无关，三层证据链。

### 遗留项 / 下一步
- [ ] PR #146 等 review/merge。
- [ ] senza-agent 侧改造（等 #146 落地或用裸 Attachment 方案提前做）：`tool_load_image` 返回 Attachment/mixed list；`_sdk_compat.stream_prompt` attachments 透传；host 侧 VisionDegraded 事件呈现。
- [ ] "glm-5.3flash textonly" 出处仍未定位（用户侧再问时需拿精确提示原文）。

## 2026-08-31 | Qevos 式识图落地：senza-sdk 1.3.0 升级 + 附件全链路（工具→prompt→HTTP→面板）

### 本次做了什么

决策：**不改 Senza 仓库**（PR #37 仍 OPEN，不在 v1.3.0），用已发布的 senza-sdk 1.3.0 附件 API（`prompt/steer(text, attachments=)`、`image_base64/image_url`）在 senza-agent 侧实现 Qevos 的图像识别功能。

1. **依赖升级**：venv senza-sdk 1.2.1 → 1.3.0（PyPI wheel 实测 `image_base64/image_url/Attachment` 可导入可用）；`requirements.txt` 升为 `senza-sdk>=1.3.0`；`runtime.lock` 由 `05f09d6`（runtime 仓库 SHA，dev_setup 静默 checkout 失败的旧坑）改钉 Senza v1.3.0 tag commit `320744cf…`，dev_setup 从 Senza 源构建 wheel 时不再落空。

2. **工具层**（`tools/standard.py`）：
   - `tool_load_image`：本地/远程图片经原下载+`_normalise_image` 规范化路径后，改为返回 mixed list `[caption?, Attachment(image_base64)]`（1.3.0 `parse_tool_result` 原生支持），LLM 真正拿到像素；错误仍返回 `{"status":"error"}` dict。`_state.vision_supported is False` 守卫保留。
   - `tool_load_video`：抽帧后每帧一个 JPEG Attachment + 引导文字（时长/范围/帧率），代替原来"抽完帧扔掉只报数字"的行为。
   - 实测：1x1 PNG 本地路径 → `["a dot", Attachment(image_base64, image/png)]`；空路径/缺文件/网络不可达 → error dict。

3. **SDK 兼容层**（`_sdk_compat.py`）：vendored `stream_prompt` 增加 `attachments` 参数，`obj.prompt(text, attachments)` 透传（旧 SDK 传 None 也不炸，新 SDK 才传真附件）。

4. **TaskManager**（`webserver/task.py`）：`start_task(text, timeout_ms, attachments=)` → `_run_task` → `stream_prompt(..., attachments=)` 全链透传。

5. **`/api/inject-image`**（`qevos_bridge.py`，原为写死"not supported"的桩）：
   - 新增模块级 `_attachment_from_payload`：data URL（含 mime 解析）/裸 base64/`url` 透传/本地 `path`（读盘 embed）四路解析，坏输入抛 ValueError→400。
   - 路由复刻 `_api_inject`：ask_user 等待中→拒绝；运行中→`harness.steer(text, attachments=[att])`（TypeError→提示升级 1.3.0）；空闲→`start_task(prompt, attachments=[att])`。
   - VisionDegraded 事件：runtime #146 的该事件不在 v1.3.0 的 17 类映射里，1.3.0 下不会出现（`_ => unknown` 兜底），无需处理；vision 拒绝→`_state.vision_supported=False` 的降级链路属 runtime 侧，留待 #146 进入 Senza 发布线。

6. **面板**（`static/panel.html`，7984→8131 行）：
   - cmd 输入框内新增 📎 按钮（CSS 仿 rigor-bulb）、上方浮动附件预览条（缩略图+×删除）。
   - `cmdInput` paste 监听：剪贴板图片（截图）直接进附件，8MB 上限。
   - `sendCmd`：有附件时逐张 POST `/api/inject-image`（首张带 text），走 postJsonWithRetry；无附件路径完全不变。ask_user 等待场景由服务端统一拒绝。

### 验证结果

- 新增 `tests/test_image_attach.py` 27 个用例：payload 解析 10、工具 7、stream_prompt 2、TaskManager 2、endpoint 6（aiohttp TestClient 实测 idle-start/running-steer/400/awaiting-reject/旧SDK-TypeError）——27/27 通过。
- 全量回归：`pytest tests/` → **353 passed, 3 skipped**（基线 326+3，新增 27 全过，无回归）。
- panel.html 4 个 `<script>` 块全部过 `node --check`；新增元素/函数/端点引用均唯一。
- venv 内端到端：`registry.get_standard_tools()` 的 `load_image` 是 `Tool` 对象，`tool_load_image` 直接调用返回 `[str, Attachment]`；1.3.0 `Attachment(image_base64, image/png)` 实例化 OK。
- 已知非问题：跑 Senza 仓库 PR 分支的 `test_tool_attachment_return.py` 失败（`Tool.drive` 属性不存在）——那是 PR #37 测试对接 PR 分支自身运行时的用例，对着发布版 1.3.0 wheel 跑必然缺 `drive()`，与本次改动无关。

### 踩坑与结论

- **edit 工具行号漂移三次**：大 HTML 中 `INS.POST` 锚点算错会把块插进无关函数（两次插坏 sendCmd、一次把附件条插进 tab-pane 又劈开 rigor-bulb 按钮）。教训：编辑后必须立刻 `read` 回看实际范围，靠"响应里显示的行"而非记忆行号；本次全部当场修复并 `node --check` 验证。
- **mock harness 事件队列写法**：`h.events = MagicMock()` 时 `_get_event_iterator` 拿到的 `next(it)` 返回 MagicMock（非 dict），`stream_prompt` 永远等不到终止事件→测试挂 90s 超时。正确写法 `h.events = MagicMock(return_value=iter([...]))`。
- 1.3.0 的 `parse_tool_result` 对 bare Attachment、str、list/tuple 混合、dict（仅 text 块）都接受——mixed list 是官方路径，无需 PR #37。
- `ToolContext` 无法从 Python 构造（builtins 需要 abort token），PyTool 层端到端靠 1.3.0 解析路径约定 + 全量测试间接覆盖。

### 遗留项 / 下一步
- [ ] Senza PR #37 merge 后：tool dict 返回路径（gap 1）可再收敛；steer 的 attachments 在 1.3.0 SDK 已支持，但运行时 steer-with-image 的 UX 文案可细化。
- [ ] runtime #146 进 Senza（下个 tag）后：host 侧处理 `VisionDegraded` 事件（目前 1.3.0 event_stream 映射到 unknown），vision 拒绝自动剥历史重试的 dashboard 呈现。
- [ ] desktop 打包层（win-unpacked 里的旧 panel.html）需随下个安装包构建更新，本次只改了源 `senza_agent/webserver/static/panel.html`。

## 2026-08-31 | SDK 1.3.0 基线确立：清除 1.2.x 兼容层（_sdk_compat 删除）

### 本次做了什么

用户确认基础版本已提升至 1.3.0，要求找出并清理为旧版兼容而存在的代码。审计结果与处置：

1. **删除 `senza_agent/_sdk_compat.py`（-107 行）**。该 shim 是为 SDK 1.2.1–1.2.3 vendored 的
   `stream_prompt`（补 `max_consecutive_timeouts` 参数 + `attachments` 透传）。1.3.0 原生
   `senza.stream_events` 与 vendored 版逐字一致（对比过函数体），原生 `prompt(text, attachments)`
   位置参数直接可用——shim 的两个存在理由都不再成立。

2. **`webserver/task.py` `_run_task` 改为纯原生组合**：先 `senza.stream_events(harness,
   timeout_ms=30000, max_consecutive_timeouts=999999)` 订阅事件流，再起 daemon 线程跑
   `harness.prompt(text, attachments)`；终态 break 后 `join(60s)`，线程内异常（LLM 失败
   RuntimeError）重抛进 except。同时把 broadcast+on_event 收敛为 `_emit` helper，**修复了一个
   既有 bug**：原先 error 路径只 `_broadcast`（仅 WS 客户端可见），不走 `_on_event`
   （= `state_bridge.on_task_event`），LLM 失败时 dashboard 状态桥收不到 error 事件。顺带删除
   死代码 `full_text`（声明后从未 append，`answer_preview` 恒为空串，前端无消费）。

3. **`qevos_bridge.py` `/api/inject-image` steer 分支**：删除 "TypeError → upgrade to >= 1.3.0"
   回退和无用的 `import senza`，合并为单次 `steer(text or ..., attachments=[att])`。

4. **`pyproject.toml`**：`senza-sdk>=1.2.3` → `>=1.3.0`（requirements.txt 上一轮已升，此处漏网）。
   全仓 `1.2.x` 引用清零（仅 WORKLOG 历史记录保留）。

### 验证结果

- `tests/test_image_attach.py` 重写：删 `_sdk_compat.stream_prompt` 2 用例与旧 SDK TypeError
  1 用例；新增原生路径用例——`prompt` 收到 `(text, [att])` 位置断言、prompt 线程异常→
  `_on_event` 收到 error 事件断言。25/25 通过。
- 全量回归：**351 passed, 3 skipped**（基线 353−4 删除 +1 新增 −1 old-SDK = 351，符合预期），
  连跑两次稳定。仅剩 warning 为 `tools/standard.py` 既有 `datetime.utcnow()` 弃用，与本次无关。
- `senza_agent/cli.py`、`webserver/app.py` 入口导入 OK；`pyproject.toml` tomllib 解析 OK。

### 踩坑与结论

- **edit 工具 `SWAP` 范围计算又错两次**（一次产生重复 steer 调用行、一次残留旧循环体 +
  重复 `event_count` 行）——与新写的 `_emit` 相邻的旧行必须显式 DEL，不能指望 SWAP 吞掉。
  每次编辑后立即回读验证，本次全部当场修复。
- 测试里 `asyncio_run`（新建 loop 跑协程）在 pytest-asyncio 已运行的 loop 内必然
  `RuntimeError: Cannot run the event loop while another loop is running`——async 测试直接
  `await` 即可，不需要自建 loop。
- 1.3.0 的 `prompt_async(text, timeout_ms, attachments)` 内部走 `prompt_and_collect`（自订阅
  收集直到 settled），与 webserver 需要的"边流边转发"模型不同，故 task.py 用
  `stream_events` + 线程 `prompt` 组合而非 `prompt_async`。

### 遗留项 / 下一步

- [ ] 前一轮遗留不变：Senza PR #37 merge 后收敛 tool dict 返回路径；runtime #146 进 Senza
  发布线后处理 `VisionDegraded`；desktop 打包层随下个安装包更新。

## 2026-08-31 | 桌面端更新进度窗口乱码 + 无百分比修复

### 现象与根因

用户报告 Windows 桌面端检查更新时，下载进度弹窗中文全部乱码、且看不到百分比。

- **乱码根因**：三处 `loadURL('data:text/html,' + encodeURIComponent(html))` 的 data: URL
  MIME 类型没有 `charset` 参数，页面也没有 `<meta charset>`。Chromium 对无 charset 的
  data: 文档按 OS locale 的传统编码解码（zh-CN Windows 上是 GB18030），UTF-8 字节按
  GBK 解即成乱码（`正在下载` → `姝ｅ湪涓嬭浇` 一类）。
- **无百分比根因（两层）**：
  1. `setUpdateProgress` 走 `_execInProgressWin`（`executeJavaScript(...).catch(() => {})`），
     首次调用发生在 `loadURL` 之后同帧，若 data: 文档尚未解析完，注入被静默吞掉——
     取消按钮的 onclick 绑定同样受影响（本次补了 `did-finish-load` 等待）。
  2. electron-updater 侧：`download-progress` 事件被节流到 1 秒 1 次
     （ProgressCallbackTransform / DataSplitter 均 `nextUpdate = now + 1000`）；且
     0.1.5→0.1.6 差分下载时 COPY 阶段不发进度（ProgressDifferentialDownloadCallbackTransform
     明确 skip COPY），delta 小于 1 秒下载完时可能一次进度都不发。属上限限制，
     已通过"初始 0%（连接中…）"文案兜底显示，下载真正开始后 1 秒内出百分比。

### 修复（desktop/main.js，+19 −4）

1. 三处 data: URL 全部补 `charset=utf-8` + `<meta charset="utf-8">`：
   更新进度窗口（line 429）、启动 loading 窗口（line 383）、agent 启动失败错误窗口
   （line 584，顺带对 err.message 做了 `&`/`<` HTML 转义）。
2. 进度窗口初始 pct 文案由 `准备下载…` 改为 `0%（连接中…）`——差分下载 COPY 阶段
   或 1 秒节流期内用户也能看到明确的进度语义，而非空白。
3. `checkForUpdatesInteractive` 中创建进度窗口后等待 `did-finish-load` 再注入
   cancel onclick / 进度脚本，消除 executeJavaScript 打在未加载文档上的竞态。

### 验证

- `node --check desktop/main.js` 语法 OK。
- data: URL round-trip 测试：`encodeURIComponent` 后按声明 charset 解码，中文完整还原，
  meta charset 存在，URL 长度 183 字符（远低于 Chromium data URL 上限）。
- 从 main.js 提取真实页面模板做 HTML 结构解析：`charset meta: ['utf-8']`，`bar/pct/cancel`
  三个 id 齐全，标题"正在下载 v0.1.6"、初始"0%（连接中…）"、取消按钮文案全部到位。
- 进度 payload 算术验证：`{percent: 42.66, transferred: 33MB, total: 78MB, bps: 5.5MB/s}`
  → `43%（33.0/78.0 MB，5.5 MB/s）`，与 setUpdateProgress 逻辑一致。
- 页面生命周期模拟（初始 → 43% tick → 100% done）状态断言全过。
- 环境无 Electron/jsdom/playwright，以上为静态提取 + DOM shim 级验证；真机表现待
  下个安装包在 zh-CN Windows 上确认。

### 遗留项 / 下一步

- [ ] 本修复需随下个 desktop release 发版（tag 触发 build-windows.yml）才能到达用户。
- [ ] 历史遗留不变：Senza PR #37 merge 后收敛 tool dict 返回路径；runtime #146 进发布线
  后处理 `VisionDegraded`。

## 2026-08-31 | 首次启动环境配置增加 (N/6) 步骤进度

### 需求与实现

用户要求：安装完第一次使用时，配置环境的提示信息后面加 "(1/7)" 式进度。

实现分两层：

1. **desktop/setup_python.ps1**：新增 `Write-Step` / `Write-StepDone` helper（`$script:StepTotal = 6`），
   六个步骤各打一行 `[STEP n/6] <label>` / `[STEP n/6] done`：
   1=Locating Python、2=Creating virtual environment、3=Upgrading pip、
   4=Installing Python packages（耗时长，文案注明 may take a few minutes）、
   5=Installing senza-agent package、6=Finishing up。
   原步骤里与新 marker 重复的三条 Write-Host（Upgrading pip/Installing dependencies/
   Installing senza-agent package）删除，避免日志双份。
2. **desktop/main.js**：`ensurePythonVenv` 的 stdout 处理改为按行解析 `[STEP n/total]`
   marker，映射到 `SETUP_STEP_LABELS` 中文文案后在 loading 窗口显示 `(n/total) 中文…`；
   `done` 行不覆盖正在进行的步骤文案。启动时先显示固定 `(1/6) 检查 Python 环境…`。
   **行缓冲**：`data` 事件可能在行中间截断（本机实测 1 字节分片时 marker 会漏检），
   增加 `partial` 缓冲拼接尾部残行，保证 marker 永不丢失。

### 验证

- `node --check desktop/main.js` OK。
- 步骤序列静态断言：`Write-Step` 1..6 各出现一次、`Write-StepDone` 1..6 齐全、
  重复 Write-Host 已清除。
- stdout 解析器行为测试：按真实 CRLF 输出喂入全六步（含 done 行、pip 噪音行），
  产出消息恰好为 `(1/6) 检查 Python 环境…` … `(6/6) 即将完成…`，PASS。
- 跨 chunk 分片测试：marker 被切成 3 段喂入仍正确识别（缓冲生效），PASS。
- 本机无 pwsh，ps1 侧仅静态验证；真机首次安装时六个 marker 会如实打出。

### 遗留项 / 下一步

- [ ] 本项与乱码修复（73ee40c）都需随下个 desktop release 发版。
- [ ] 历史遗留不变：Senza PR #37、runtime #146。

## 2026-08-31 | runtime PR #146 已合并：影响分析与下一步

### 状态确认

- llm-harness-runtime origin/main HEAD = `64db136` "Merge pull request #146"，分支
  `fix/vision-degradation-145`（`1b02d6a` fix(loop) + `1fc78c8` chore parking_lot）已进主线。
- 合并内容：`AgentEvent::VisionDegraded { reason, stripped_blocks }` 新事件（events.rs）；
  `loop_fn.run_loop` 在 `LlmError::InvalidRequest` 时一次性 strip 图片/文档块并重试
  （`vision::strip_vision`，one-shot guard 防死循环）；ToolResult/User 两类消息的视觉块
  剥离 + 修复提示文案。
- **关键限制：senza-sdk 1.3.0 wheel 的 `EventType` 枚举没有 VisionDegraded**
  （本机 .venv 实测：ABORTED/AGENT_END/ERROR/... 12 个成员）。1.3.0 wheel 构建于
  runtime tag `320744c`（v1.3.0），早于今天的合并。事件在 FFI 边界就被丢弃，
  senza-agent 侧现在什么也收不到。

### 下一步（等 SDK 发版后）

1. Senza 仓库发布包含 `64db136` 的 SDK 版本（如 1.4.0）。
2. senza-agent：`requirements.txt`/`pyproject.toml` 升 `senza-sdk>=1.4.0`。
3. `qevos_bridge.StateBridge._convert_event` 增加 `vision_degraded` 分支：
   转 `{"type": "error"|"notice", "text": "模型不支持图片输入，已剥离 N 张图片后重试"}`，
   状态桥置 warning 提示；事件本身不改变 status（run 会继续）。
4. task.py 的 terminal 事件集合不变（VisionDegraded 非终态，无需处理）。

### senza PR #37 的判断

用户判断正确：**#37 是风格/API 形状问题，不是功能缺口，不建议按原样合并**。
- 合并 #146（缺口 2/3）后，text-only provider 的图片拒收已在 runtime 层自愈
  （剥离+重试+通知），#37 解决的"dict content 块被拒"只剩"风格宽容度"价值：
  tool 返回裸 Attachment / `[Attachment]` 才是 SDK 1.3.0 的官方路径
  （senza-agent 的 load_image/load_video 已走这条路）。
- 且 #37 有实证问题：其 `test_tool_attachment_return.py` 对 1.3.0 wheel 失败
  （缺 `Tool.drive`），分支基于更早的 main。
- 建议：close #37（或不合并），在 issue #145 的"缺口 1"讨论里改为记录
  "dict 块返回错误信息应更友好"（PR #146 已把错误文本回灌问题一并解决——
  InvalidRequest 现在触发剥离重试而非裸错误）。若社区确有 dict 块需求，等有
  真实需求再按 1.3.0+ 基线重写。

## 2026-08-31 | 修复 _StateRef/AgentState 字段失配导致的运行时崩溃

**问题**：用户报告 `load_image` 崩溃 `AttributeError: 'AgentState' object has no attribute
'vision_supported'`，`register_tool` 同理缺 `evolved_tools`。诊断完全正确：
`agent.py:262` 的 `set_state(state)` 把模块级 `_state` 换成了真正的 `AgentState`，但
`tools/standard.py` 的 `_StateRef` 有 10 个字段（evolved_tools、repair_candidates、
repair_failures、repair_history、long_term、concept_memory、runtime_patches、
interrupt_handler、vision_supported、bad_image_urls）在 `behavior/state.py` 的
`AgentState` 上从不存在。初始提交 `30423b3` 就是这样——**这批工具从第一天起就必崩**，
不是识图功能新引入的（351 个测试从没抓到，因为 `test_tools.py` 的 `_isolate_state`
fixture 把 `_state` 换回全新 `_StateRef`，绕开了真实状态对象）。

**修复**（3 文件 +89 −11）：
1. `behavior/state.py`：`AgentState` 补齐全部 10 个缺失字段（dict/list 用
   `field(default_factory=...)`，`vision_supported: bool | None = None`）。
2. `tools/standard.py`：
   - `_StateRef` 改为 `@dataclass`——原来是普通类的类级可变默认值
     （`evolved_tools: dict = {}`），所有实例共享同一个 dict，
     `_isolate_state` 的重置实际清不掉数据（潜在测试串扰 bug，顺手修掉）；
   - 新增 `_STATE_DEFAULTS` 工厂表 + `set_state()` 对缺失字段就地补默认值，
     以后再有字段失配会退化为空容器而不是 AttributeError 崩溃。
3. `tests/test_integration.py`：两个回归测试——① AgentState 必须覆盖
   `_STATE_DEFAULTS` 的每个字段；② `set_state` 对裸对象补字段后
   register_tool 能正常工作。

**验证**：
- 全量 `pytest tests/ -q`：353 passed, 3 skipped（基线 351+3，净增 2 个回归测试）。
- 真实生产路径复现→修复证明：`create_agent(load_config())` 后（此时 `_state` 是
  `AgentState`），修复前 `load_image`/`register_tool` 必现 AttributeError；
  修复后 load_image（文件不存在→error dict）、load_video（缺 opencv→error dict）、
  register/delete/repair/promote 全生命周期、save/load_tools、remember 全部正常，
  错误路径保持 `{"status": "error"}` 约定。
- 插曲：第一次全量跑挂 4 个测试——`field(default_factory=...)` 在非 dataclass 上
  只会存成 `Field` 对象（`'Field' object is not iterable`），`_StateRef` 补
  `@dataclass` 后恢复；另一次 E2E 断言失败是 `_build_tool_recipe` 会对
  python_code 做 `dedent().strip()`，是既有合理行为，非 bug。

**遗留**：`interrupt_handler` 至今没有任何生产代码写入（grep 全仓库只有
standard.py:1398 读），SSH 轮询中断（/stop）实际不会生效——等有真实需求时
在 cli/webserver 层把 `ReplCommandHandler` 接到 state 上。

## 2026-08-31 | 发布 v0.1.7（含状态失配修复）

版本四处同步 0.1.6 → 0.1.7（desktop/package.json、package-lock.json、pyproject.toml、
senza_agent/__init__.py），提交后打 tag v0.1.7 触发 build-windows.yml 出 Windows 安装包
（draft release，需手动 publish）。本版内容：`db655d5` AgentState/_StateRef 字段失配
修复（load_image/load_video/register_tool 等自初始提交起运行时必崩）+ `73ee40c` 更新
进度窗口中文乱码/百分比 + `88ef4f8` 首次启动 (N/6) 步骤进度。

### v0.1.7 发布记录（同日补充）

- tag v0.1.7 push 后 CI run 33367047150 success（~2 分钟出包）。
- electron-builder 又产生两个重复 draft：#379565965（仅 blockmap）、#379565967
  （latest.yml + exe）。处置：下载 blockmap 重新上传到 #379565967，删 #379565965。
- 新坑：CI 建的 draft tag 是 `untagged-…` 占位（打 tag 前已创建），PATCH
  `tag_name: v0.1.7` 修正后才 publish。
- 已发布：releases/latest = v0.1.7（latest.yml + exe 82MB + blockmap 齐全，
  feed sha512 EEO/f9A+…），桌面端自动更新即可拉到。
