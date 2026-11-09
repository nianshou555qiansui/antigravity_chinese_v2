# Antigravity 2.0 汉化

给 [Antigravity](https://antigravity.google/) 2.0 的界面加中文。

当前适配 **2.29.0**：新增 600 条界面文案，覆盖内置浏览器、语音、侧边面板、远程项目、插件登录和自动化任务；词典共 3495 条。已在 Windows 版 2.29.0 验证运行时注入，macOS 本轮未实机验证。

不改安装目录里的任何文件：以调试端口启动 Antigravity，通过 Chrome DevTools 协议把翻译脚本注入页面。想恢复英文，关掉再正常启动就行；官方更新也不会弄坏它。

![中文界面](docs/screenshot.png)

![Antigravity](https://img.shields.io/badge/Antigravity-2.0-blue) ![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey) ![Python](https://img.shields.io/badge/Python-3.x-green)

## 用法

前置：Windows 或 macOS、Antigravity 2.0、Python 3.10+ 和 websockets（推荐 Python 3.11+，离线测试要求 3.11+）。

```bash
pip install websockets
```

Windows 双击 `qidong.bat`，macOS 双击 `qidong_mac.command` 或在终端跑 `./qidong_mac.sh`：自动发现并复用已有的 Antigravity 调试端口；只有应用未运行时才启动。逐个验证并翻译该实例的所有主界面窗口，共用 30 秒观察期，为重载和新开的主窗口补注。看到 `[2/2] Done!` 就完成了；失败时启动器会保留错误退出码。

**默认不会关闭或重启应用。** 已运行但没有可用调试端口时，会报错并保持应用不变。保存工作后可运行 `python hanhua_v2.py --force-restart`，输入 `RESTART` 确认后才关闭并重启；这仍可能丢失未保存内容。仅在明确接受风险的非交互场景使用 `--force-restart --yes`。

macOS 从 GitHub 下 ZIP 解压的话，双击 `.command` 可能被系统拦（文件带隔离属性），改在终端里跑 `./qidong_mac.sh` 就行；git clone 下来的不受影响。

也可以命令行：

```bash
python hanhua_v2.py                                # 自动发现并复用；未运行时启动并注入
python hanhua_v2.py --install-dir "D:\Antigravity" # 找不到可执行文件时手动指定（macOS 可指向 .app）
python hanhua_v2.py --port 9334                    # 只连接此端口；首次启动也使用此端口
python hanhua_v2.py --no-launch                    # 只注入已带调试端口的实例
python hanhua_v2.py --watch 60                     # 所有窗口共用的观察秒数，默认 30
python hanhua_v2.py --force-restart                # 保存工作后，交互确认重启
python hanhua_v2.py --status                       # 只读查看版本、端口、窗口、引擎和词典状态
python hanhua_v2.py --doctor                       # 详细只读诊断及处理建议，不自动修复
```

省略 `--port` 时仅检查 Antigravity 进程拥有的回环监听端口，并校验调试服务的产品身份；首次启动仍默认使用 9333。发现多个有效调试端口时，需用 `--port` 明确选择，不会随意选一个实例。

主窗口还须通过原生桥、产品配置和页面挂载结构检查，不靠窗口标题或页面顺序判断。加载未完成时等待，不向文件预览、独立终端/设置窗口或内置浏览器网页注入。结束时输出成功、跳过、未完成及补注数量；仍有窗口失败时返回非零退出码，不影响已经成功的窗口。`--watch 0` 关闭后续观察，但仍会等待初始窗口就绪（最多约 60 秒，另加在途探测耗时）。

`--watch` 是观察期，不是命令的硬超时。窗口探测最多并发 4 个，请求与连接清理有各自超时；多个窗口无响应时，最终结束时间可能超过观察秒数。

注入只存在于内存里，每次启动 Antigravity 都要重跑。观察期结束后再开新窗口或重载页面，请运行 `python hanhua_v2.py --no-launch`，或开启下面的安全守护。

### 只读诊断

遇到未翻译、端口连接失败或不确定守护是否有效时，先运行 `python hanhua_v2.py --doctor`；只想看简要快照用 `--status`。两者不会启动、关闭或重启应用，也不注入引擎、不修改词典、不创建守护日志或锁文件。可加 `--port 9333` 明确选择实例；不能与 `--watch`、`--force-restart` 或 `--autostart` 混用。

输出包含 Antigravity 版本、已验证端口、候选窗口数、各窗口引擎快照和本地词典校验结果。窗口只以 `#1` 等本次序号显示，不输出标题、URL、会话内容或账号信息。词典会检查 JSON 格式、空值、类型及归一化重复键；引擎词条数与本地不同会提示重新注入，数量相同不代表词典内容完全一致。

退出码：`0` 为本次检查通过，`1` 为未运行、未就绪、引擎缺失/未知或检查失败，`2` 为参数错误。旧引擎没有只读快照时显示“未知”，不会为了检测而调用有副作用的探活函数；请手动运行 `--no-launch` 加载新版引擎后重查。诊断不证明 Windows 登录自启守护进程正在运行；查看自启项仍用 `--autostart status`。

### 安全守护

也可以开启安全自动注入（仅 Windows；需要应用已有调试端口）：

```bash
python hanhua_v2.py --autostart on      # 开启（建登录自启项，下次登录生效）
python shouhu.py --verbose              # 或先前台跑一次看效果，Ctrl+C 退出
python hanhua_v2.py --autostart status  # 看状态
python hanhua_v2.py --autostart off     # 关闭
```

后台守护每轮重新验证进程、端口和窗口，复用有效的 CDP 连接，仅为引擎缺失的主窗口补注入。健康时每轮后等 5 秒，加载或局部失败时等 2 秒；没有应用、端口、窗口或发现失败时逐步退避到 30 秒。单个窗口断线/超时会独立退避并在下一次身份验证后重连，不影响其他窗口；不关闭、启动或重启 Antigravity。日志只在状态/数量/错误类别变化时记录到 `shouhu.log`。

守护启动时只加载一次词典并生成引擎。改词典后可运行启动器立即刷新当前窗口；要让后台守护以后补注时也用新词典，还需重启守护。尚不支持词典热更新。重注入会断开旧观察器并重建引擎；重复精确翻译、属性回写及含变量模板由回归测试覆盖。

**从旧版守护升级**：正在运行的旧进程不会自动加载新代码。请先 `python hanhua_v2.py --autostart off`，再 `python hanhua_v2.py --autostart on`；下次登录生效，或手动运行 `python shouhu.py --verbose`。仅更新文件而不重启旧守护，旧版自动关闭应用的行为仍可能存在。

## 原理

`hanhua_v2.py` 复用现有实例，或在应用未运行时带调试端口启动。`cdp_discovery.py` 核实进程和本机监听端口，读取 `/json/version`、`/json` 并列出候选页面；`cdp_targets.py` 在同一次求值中验证主界面身份并注入带字典的 MutationObserver 脚本。重载后重新验证，不注册跨导航的自动注入脚本；已注入页面若同文档导航到预览等排除区域，引擎也会停止。

`cdp_connection.py` 在单个事件循环中管理逐窗口连接、请求超时和断线退避。健康窗口只发送小型探测表达式，不反复传整份词典；确认引擎缺失后，才发送再次校验身份的完整注入表达式。命令结束或守护退出时关闭连接，窗口/端口归属改变时淘汰旧会话。

脚本监听 DOM 变化实时替换文案。文本与子树只在初始扫描或对应变更时处理；普通获准属性按属性定向更新，隐私边界属性变化才重扫子树。3 秒兜底仅重扫可翻译属性，不重复递归全页面文本。引擎和采集器共用 `privacy_policy.py`：在读取内容前排除已识别的代码、终端、编辑器、消息、附件和用户标题，保护跨 Shadow DOM 与 slot 继承。普通输入框只允许翻译提示属性，不读写输入值。性能测试是合成 DOM 的本地基线，不代表真实 Antigravity 页面耗时。约 0.6 MB 的内存增量是旧版较小词典的历史测量，当前版本待重新测量。

为什么不用老项目的办法：[cshitian/antigravity_chinese](https://github.com/cshitian/antigravity_chinese) 针对 1.x，往 `workbench.html` 写 `<script>` 并同步 `product.json` 校验值。2.0 把 `resources/app` 打包成了 `app.asar`，界面改由本地服务渲染，那个注入点不存在了，失败还是静默的（上游 Issue #4 提过，一直没人修）。

## 补充翻译

漏翻的地方只需要改 JSON，不用碰代码。

1. 先用 `python caiji.py` 查看来源统计；需要本地候选时显式运行 `python caiji.py --include-ui-text`，仅将已审核的静态 UI 标签写进 `pending.json`，不在控制台打印原文。未知文案请人工核对或按下文离线 bundle 审计，不再全页面抓取
2. 翻译后按 `{"原文": "译文"}` 的格式加进 `dicts/`：`common.json` 放通用词，`ui_v2.json` 放 2.0 基础文案，`ui_v2_225.json` 和 `ui_v2_229.json` 分别放 2.25.25、2.29.0 的增量；同一个 key 别写进两个文件
3. `python hanhua_v2.py --no-launch` 重新注入验证

写字典有三条规矩：

1. 只写完整的 UI 字符串。30 字符以上的词条会参与子串替换，写半截短语会把整句搅成中英混杂。
2. 避免翻译用户内容和产品名。已识别的会话标题、路径、邮箱区域会保护；未知组件边界不保证完全识别。Antigravity、Gemini 这类产品名也不动。
3. 带变量的句子（比如「当前模型：<模型名>」）走 `hanhua_v2.py` 的 `REGEX_RULES`，写死进字典的话官方一换内容就失效。

Agent 统一译作智能体。下拉菜单和悬停提示通常在展开或悬停时才进 DOM；采集器只读取下文的审核标签，不采集任意 tooltip。未知文案请从 bundle 静态审计或人工核实。官方大改界面后，保护区与词条也需重新核对。

### 采集与隐私

```bash
python caiji.py                     # 默认仅统计许可来源，不读取或保存界面原文
python caiji.py --include-ui-text   # 显式导出少量已审核静态标签到本地 pending.json
python caiji.py --misses            # 导出新版引擎未命中的来源次数到 misses.json，无原文
python caiji.py --port 9334         # 多实例时明确选择调试端口；默认自动发现
```

采集器只处理通过身份检查的主窗口，不再选择第一个 HTTP 页面。所有模式均只读，不启动应用或注入引擎。失败或有窗口未完成时返回 1，保留原文件；成功时原子替换报告，参数错误返回 2。

`pending.json` / `misses.json` 改为带 `schema: 1` 的统计对象，包含固定 `sources`、`withheld`、窗口计数及 `candidates`。默认 `candidates` 为空，来源数是静态 UI 位置的观测次数，**不是唯一英文或漏翻词条数**；即使同位置已中文也可能计数。显式候选须同时匹配已审核的 10 个按钮/菜单标记及固定标签值，不能用于抓取任意漏翻句子，仍需人工复核，采集产物均不要提交。

`--misses` 只读取当前引擎的来源计数，不保存文本、选择器、标题、URL 或文本哈希。次数按内容变化观测，重扫不重复累加，同一文案在多个节点可能多次计数；重注入清零，不能据此判定哪些旧词条已废弃。旧版原文 Set 不会导出，请先运行 `python hanhua_v2.py --no-launch` 加载新版引擎。`--misses` 不能与 `--include-ui-text` 同用。

为防误改用户属性，`data-title`、普通元素的标题/无障碍标签及未知 tooltip 不再无条件翻译；输入框仅许可安全上下文中的 `placeholder` / `aria-placeholder`。动态分组和用户消息外包装保守排除，内部固定 Pinned 标题、消息动作或 tooltip 可能因此保留英文。这是隐私优先的取舍。已识别区域会保护，但未来未知组件的普通正文仍可能精确命中字典；不保证任意版本都绝不误翻，遇到时请反馈组件位置而非私人原文。

`caiji.py` 仅统计当前页面中的许可位置，文本候选也受静态白名单限制。想审计整个界面（包括未打开的设置页），先从调试端口的 `/json` 取得主页面 URL，再从该 URL 的 origin 下载 `/main.js`（2.29.0 约 11 MB；界面服务端口与调试端口不同，可能使用自签名 HTTPS）。抽取 UI 字符串与 `dicts/` 做双向 diff，可发现新增或改写的原文；核对上下文并排除库文案及用户数据后再翻译。过滤规则与踩坑见 [CLAUDE.md](CLAUDE.md) 的「bundle 审计法」。

现在也可以对已下载的本地 bundle 使用只读审计器：

```bash
python audit_bundle.py --bundle path/to/main.js --version 2.29.0 --output audit-report.json
```

`audit_bundle.py` 不联网、不连接 CDP、不读取运行中的页面，也不修改字典。报告记录 bundle 与各字典 SHA-256，保留字符串位置和属性/直接文本证据，给出精确命中、`new-candidate`、`possible-rewrite`、`not-found-in-bundle`、动态模板、术语及占位符复核项。相似度默认至少 0.85 且只供人工审核；`not-found` 不等于废弃，工具不会自动添加、删除或发布词条。`--expect-sha256` 可在审计前锁定已确认的 bundle；报告中的 `audit-report.json` 是本机产物，不要提交。

离线回归检查：`python -m unittest discover -s tests -v`（需本机有 Node.js，不会启动或修改 Antigravity）。改完词典或引擎都运行一次。GitHub Actions 的 `Offline regression` 工作流会在 Python 3.11、3.12、3.13 上重复运行测试、语法检查和启动器编码检查，并在独立作业中安装 Chromium 运行无用户数据的浏览器夹具。

### 验证范围

离线测试覆盖引擎身份检查、隐私边界、多窗口调度、采集器、bundle 审计、守护连接、重注入稳定性，以及合成 DOM 的初始扫描/变更批次/空闲属性兜底。浏览器夹具使用无账号和用户内容的本地页面。以下场景仍需在用户确认可用的真实环境中验收，离线测试不替代它们：

2026-11-09 Windows Antigravity 2.29.0：安全复用端口 9333，`--no-launch --watch 0` 向一个已确认主窗口补注，随后 `--doctor` 通过。默认采集和漏翻来源计数均以内存执行，未导出候选文本，字典 SHA-256 前后相同。离线 bundle 审计的 `main.js` SHA-256 为 `8d1196a22cd3eb9779e8faf5c31eb732d3df8d9d037b685733ed4e3d6cdfb5a2`，匹配已记录的 2.29.0 bundle；审计未改字典。报告有 3,995 个新候选及 38 个可能改写，未逐条人工复核或自动收录。

- 多个主窗口、新开/重载窗口均能补注；外部网页、预览页和独立设置页不注入。
- 实际消息、附件、标题、输入、代码及未知 tooltip 在目标版本中保持原样，固定 UI 控件仍正常翻译。
- 只对明确确认版本匹配的本地 `main.js` 运行 `audit_bundle.py`，核对报告哈希/候选并确认词典文件哈希未变。安装包版本与仓库适配版本不一致时，不得把审计结果当作目标版本验收。
- 查看推送后 GitHub Actions 的 `Offline regression` 和 `Browser fixture` 两个作业均通过。

旧版 `pending.json` / `misses.json` 可能仍有私人原文，新代码不会自动清理历史文件；请自行检查。新报告及原子写入临时文件均在 `.gitignore` 中，勿提交。

## 限制

- Windows / macOS（Linux 未测；自动注入仅 Windows）
- 支持所选调试实例内的多个主窗口；外部网页、文件预览及独立终端/设置页不翻译
- 不开自动注入时，每次启动及观察期结束后的新窗口/重载都要手动重跑脚本
- 需要能开 DevTools 调试端口，部分企业安全策略会拦截
- 调试端口在 Antigravity 运行期间一直开放，本机任何进程都能借它读到页面内容（包括代码和会话）。脚本注入完会断开，但端口随进程存活，介意就用完关掉、不带端口重启（界面会回到英文）

## 项目结构

```
hanhua_v2.py         主脚本：加载字典、生成引擎、启动、注入、守护
cdp_discovery.py     进程/监听端口归属校验、实例发现与确认后的定向关闭
cdp_targets.py       主界面身份验证、多窗口注入及共享观察期
cdp_connection.py    CDP 连接复用、逐窗口超时、断线退避与清理
diagnostics.py       只读状态检查、严格词典校验及脱敏诊断
guard_runtime.py     守护状态、空闲轮询及等待退避
caiji.py             只读来源统计、受限静态 UI 候选及未命中次数导出
privacy_policy.py    引擎/采集共享保护区、属性许可和静态候选策略
shouhu.py            可选守护进程（仅 Windows）：登录自启，自动检测启动并注入
qidong.bat           Windows 一键启动器
qidong_mac.sh        macOS 一键启动器
qidong_mac.command   macOS 双击入口（调 qidong_mac.sh）
dicts/               字典（common.json + ui_v2*.json，约 3500 条）
tests/               离线词典、完整引擎、目标识别及多窗口回归测试
tests/browser_fixture.html  无用户数据的异步/Shadow DOM/输入保护夹具
audit_bundle.py      本地 bundle 版本审计器（只读，不连接 CDP）
engine_template.js   独立 JavaScript 引擎模板；Python 源码与打包环境均可加载
```

实现细节和踩坑记录在 [CLAUDE.md](CLAUDE.md)。

## 致谢

衍生自 [cshitian/antigravity_chinese](https://github.com/cshitian/antigravity_chinese)（Antigravity 1.x 汉化）。文本归一化、字典加载逻辑和 MutationObserver 引擎骨架沿用自它，约 110 条词条在它基础上修订；CDP 注入机制和 2.0 的界面字典是新写的。

> 原项目未附带开源许可证。原作者若对沿用有异议，欢迎提 Issue 协商。

## 许可证

[MIT](LICENSE)。

## 讨论

在 [LINUX DO](https://linux.do) 社区发布与讨论，欢迎来帖子里反馈漏翻词条。
