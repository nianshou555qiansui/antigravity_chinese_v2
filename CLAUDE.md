# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

给 Antigravity 2.0 做界面汉化。核心思路是**运行时注入**：以 `--remote-debugging-port` 启动
Antigravity，连 Chrome DevTools 协议（CDP）把一段 MutationObserver 翻译引擎注入渲染页面。
**不修改任何安装文件**（不碰 `Antigravity.exe`、`app.asar`、`product.json`），因此没有还原逻辑，
官方更新也不会破坏安装。

依赖：Python 3.10+ + `websockets`（本机实测 Python 3.14.7 / websockets 16.0）。无构建；离线测试使用 Python 3.11+ 标准库 `unittest` + Node.js。
当前适配 Antigravity **2.29.0**（2026-11）。官方版本号从调试端口 `/json/version` 的 User-Agent 读：
`Antigravity/2.29.0`。本轮 Windows 实机注入通过；macOS 未实机验证。

### 为什么不能沿用 1.x 的做法

上游 `github.com/cshitian/antigravity_chinese` 针对 Antigravity 1.x：向
`resources\app\out\vs\...\workbench.html` 注入 `<script>` 并同步 `product.json` checksum。
2.0 把 `resources/app` 打包成了 `app.asar`，界面改由本地 HTTPS 服务渲染
（`https://127.0.0.1:<随机端口>/`），那个注入点在文件系统层面已不存在，且 `inject_html()`
对不存在的文件**静默返回 False**。上游 Issue #4 记录了这个失效，作者无回应。

## 常用命令

```bash
python hanhua_v2.py                      # 自动发现并复用；没有进程时启动（启动器仅委托）
python hanhua_v2.py --no-launch          # 只注入到已带调试端口运行的实例（开发时最常用）
python hanhua_v2.py --watch 0            # 关掉守护，注入完立刻退出
python hanhua_v2.py --force-restart       # 保存工作后输入 RESTART 确认；非交互需显式加 --yes
python hanhua_v2.py --install-dir "D:\Antigravity" --port 9333
python hanhua_v2.py --install-dir "/Applications/Antigravity.app"
python hanhua_v2.py --autostart on|off|status  # 自动注入开关（守护 shouhu.py，仅 Windows）
python shouhu.py --verbose                     # 前台跑守护，实时看日志
python hanhua_v2.py --status             # 只读快照，不注入/启动应用
python hanhua_v2.py --doctor             # 只读诊断，包含词典严格校验及处理建议
python caiji.py                          # 已验证主窗口来源统计 → pending.json（默认零原文）
python caiji.py --include-ui-text        # 显式本地导出审核过的静态标签，不打印原文
python caiji.py --misses                 # 新版引擎未命中来源计数 → misses.json（无原文）
```

改完代码后的自查：先跑离线回归（不会启动或修改 Antigravity），再按改动做实机验证。

```bash
python -m unittest discover -s tests -v
python -c "import ast,io;[ast.parse(io.open(f,encoding='utf-8').read()) for f in ['hanhua_v2.py','cdp_discovery.py','cdp_targets.py','caiji.py','shouhu.py']]"
python -c "import json,io,os;[json.load(io.open('dicts/'+f,encoding='utf-8')) for f in os.listdir('dicts') if f.endswith('.json')]"
# 引擎 JS 必须单独校验：它是 Python 字符串拼出来的，语法错在注入时才会暴露
# 注意别写 '/tmp/e.js'：Windows 原生 Python 看不见 Git Bash 的 /tmp，会 FileNotFoundError
p=$(python -c "import importlib.util,io,os,tempfile;s=importlib.util.spec_from_file_location('h','hanhua_v2.py');h=importlib.util.module_from_spec(s);s.loader.exec_module(h);p=os.path.join(tempfile.gettempdir(),'e.js');io.open(p,'w',encoding='utf-8').write(h.build_engine_js(h.load_dictionary()));print(p)")
node --check "$p"
python -c "b=open('qidong.bat','rb').read();assert not [x for x in b if x>127],'bat 混入非 ASCII'"
```

> 三个入口（`hanhua_v2.main()` / `caiji.main()` / `shouhu.main()`）**都要调用
> `_bypass_proxy_for_local_cdp()`**。websockets 16 的 `proxy` 参数默认 `True`，
> 会读 `HTTP_PROXY` 并在 `NO_PROXY` 不含本机时把 `ws://127.0.0.1` 打进代理（实测
> `ConnectionRefusedError`）。`http.client` 不受影响，所以症状是 `/json` 探得到、
> 注入却连不上。

## 架构

```
hanhua_v2.py     主脚本：加载字典 → 生成引擎 JS → 启动 Antigravity → CDP 注入 → 守护
  ├ load_dictionary()   合并 dicts/*.json，对 key 做 normalize_text()
  ├ build_engine_js()   加载 engine_template.js 并嵌入字典/隐私策略
  └ prepare_instance() 复用实例或安全启动，关闭应用必须显式确认
caiji.py         主窗口来源统计与受限静态标签导出；旧版 raw misses 拒绝读取
privacy_policy.py  引擎/采集器共享 DOM 保护与属性许可，读取前判定
cdp_discovery.py  进程归属、回环端口及产品校验；列出候选 target，显式确认后的定向关闭
cdp_targets.py    同一次求值内验证主界面并注入；逐窗口超时隔离，共享观察期
  ├ guarded_expression() 当前 origin + Hub 特征校验，通过后才能执行引擎
  └ inject_windows()     每轮重发现窗口，补注新窗口/重载页面，汇总当前结果
cdp_connection.py  每目标独立连接、请求 ID/锁、超时、失败退避；新发现后才能重连
diagnostics.py     只读窗口/词典诊断，不调用 engine.active() 或任何注入/启动入口
guard_runtime.py   显式守护状态、单事件循环及可测试的 step()；发现放在线程执行
shouhu.py        可选守护进程（仅 Windows）：发现已有调试端口→补注入，不启动/关闭应用
qidong.bat           Windows 一键启动器（只调 hanhua_v2.py，保留错误退出码）
qidong_mac.sh        macOS 一键启动器（只调 hanhua_v2.py，保留错误退出码）
qidong_mac.command   macOS 双击入口
dicts/*.json         字典，格式 {"英文原文": "中文译文"}
README.md            面向用户的门面文档（用法、原理、限制）；改 CLI 参数或用户可见行为时同步
docs/screenshot.png  界面截图，README 引用
requirements.txt     依赖清单（只有 websockets）
tests/               unittest + Node.js 完整引擎离线回归（无需浏览器或额外 npm 包）
```

`engine_template.js` 是无需构建的独立 IIFE 源文件。`build_engine_js()` 从脚本同目录读取，
PyInstaller 部署则回退到 `_MEIPASS`；通过 `DICT_PLACEHOLDER` 与 `PRIVACY_PLACEHOLDER` 嵌入数据。
改模板不再经过 Python 三引号转义层；仍需运行 Node.js 语法与完整引擎回归。

**平台分支**：`candidate_paths()` / `launch_antigravity()` 和 `cdp_discovery.py` 的进程/端口枚举分 Windows / macOS。
Windows 使用 `tasklist` CSV + `netstat`，macOS 使用 `pgrep` + `lsof`；只检查 Antigravity 拥有的回环监听端口，不扫描其他程序。
当前 macOS 分支有离线 mock 验证，未进行真机验证；更改平台逻辑须补回归。

### 安全启动与实例复用

`prepare_instance()` 优先复用已验证的实例，即使找不到安装路径也可注入。省略 `--port` 时自动发现；
显式指定后不回退其他端口。多个有效端口要求明确选择。发现有效 CDP 但页面未就绪时只等待，不启动新实例。
探测失败必须报错，不能当成“没有进程”。默认有进程但无端口时保持应用不变。

只有 `--force-restart` + 交互输入 `RESTART`（或显式 `--yes`）才允许关闭已确认的 PID；
不能用按映像名关闭全部进程的 `/IM` / `killall`。重启前先检查目标端口、可执行文件和进程快照；
确认期间新增进程、关闭失败或仍有进程存活时，不启动新实例。启动器不再自行杀进程，失败不能打印 Done。

### 主界面身份与多窗口

`candidate_pages()` 仅返回可安全探测的候选：本地 HTTP(S)、CDP 类型 `page`、与目标 ID 一致的
`/devtools/page/<id>` WebSocket，连接地址固定到已验证的监听端点。候选不等于获准翻译；
不能按标题或列表顺序选主窗口，同一调试端口的所有候选都须逐个验证。

`guarded_expression()` 在同一次 `Runtime.evaluate` 中检查身份并执行引擎，避免检查后页面已导航：
- 顶层窗口，当前 origin 与候选一致。
- Hub 原生桥：`electronNative.minimize/maximize`、`nativeStorage.getItems` 都是函数；只检查，不调用 IPC。
- `__APP_CONFIG__.productName` 为 `antigravity`（忽略大小写），`appVersion` 为字符串；不读取/导出整个配置或 CSRF 值。
- `#root` 已挂载子元素、存在同源 `/main.js`，文档不在 `loading` 状态；挂载未完成则等待，不强行注入。
- 路径以 `/artifact`、`/terminal-standalone`、`/settings-standalone` 开头或属于 `/static` 时跳过，
  前三项与官方 standalone 路由的前缀判断一致。合法主界面 SPA 路由如 `/c/*`、`/history`、`/view/*` 不应被误拒。

外部网页、加载壳、独立预览等不得因标题含 Antigravity 就获准翻译。真实指纹依据 2.29.0 Windows
安装包和运行中主窗口核验；新版本改变特征时宁可拒绝并重新审计，不退回宽松匹配。

### 注入失败的显式上报

`Runtime.evaluate` 的 JS 异常**不在**顶层 `error` 字段里，而在 `result.exceptionDetails`。
`_translate_target()` 必须同时查两者。只有显式 `injected/alive` 且 `confirmed: true` 才算成功，
不是“没有异常”就算成功。跳过、未就绪、断线、无效返回值都不能报已注入。
`WindowReport` 按最后一轮结果判断：关闭的窗口不残留失败，恢复的窗口不残留旧错误；
至少一个主窗口成功且没有未完成目标才返回成功。日志只输出数量和原因码，不输出标题、URL 或页面内容。

### 注入时机 —— 冷启动能否成功的关键

**端口开放 ≠ 界面就绪。** 不能用页面标题或 `body.innerText` 字数判断身份/就绪。
`inject_windows()` 每轮重新核实端口归属、枚举候选并运行上述检查，轮间最多等待 1.5 秒。
正常启动对各目标首次成功前强制刷新引擎，以加载新词典；之后只为引擎缺失的页面补注。

所有窗口共用观察截止时间，从首个成功窗口起默认 30 秒；启动未就绪/暂时错误在初始 60 秒内重试，
`--watch 0` 也保留初始等待。目标关闭、重建、导航后都按新一轮结果处理，不复用旧身份。
最多并发 4 个目标请求，每个有 5 秒超时；不得用整批超时反复取消队尾，否则前 4 个挂起页面会饿死后面的主窗口。
在途探测可能让总耗时超过观察秒数。一个 `asyncio.run()` 覆盖完整观察期，连接在各轮之间复用；
健康引擎使用不带词典的小型 guard 探测，只有明确 `missing + confirmed` 才发送完整表达式，再次原子校验身份。

**不注册 `Page.addScriptToEvaluateOnNewDocument`**：已验证的 target 以后也可能导航到用户网页，
不能继承自动注入。重载由下一轮重新验证后补注；同文档变化由引擎 `ensureTrusted()` 在观察器、
定时器和 Shadow DOM 入口复核，失信后永久停止该引擎。观察期结束后的新窗口/重载须重跑或用守护。

### 守护进程（shouhu.py，可选，仅 Windows）

`--autostart on` 建登录启动项。`shouhu.watch()` 只加载/生成一次引擎，委托 `guard_runtime.watch()`
用单个事件循环和连接池运行，`step()` 在线程中发现后先 reconcile，再逐窗口小型探测，只补缺失引擎；
没有端口、页面未就绪、探测或注入失败都只等待/记录状态，不启动、关闭或重启 Antigravity。
旧版的自动 taskkill、冷却时间和失败次数安全阀已删除；不要重新引入静默强制重启。

- pythonw 子进程必须带 `CREATE_NO_WINDOW`，避免闪黑窗。
- `active` 等待 5 秒，加载/局部失败等待 2 秒；`waiting-app/port/window` 和发现 `error` 从 2 秒指数退避至 30 秒，状态恢复重置。
- 连接级失败按目标独立退避，只有 fresh discovery 后才允许重连；超时/断线淘汰本目标会话，不能断掉健康窗口。
- 进程/端点归属、目标地址改变或目标消失须关闭旧会话；发现失败也清空连接池，不能继续用缓存 URL 注入。
- 状态/数量/固定原因码变化才记日志，退避秒数不参与去重；探测失败不是“进程不存在”，异常原文可能有私人数据，不打印。
- 字典不再每轮重新加载；编辑词典后手动启动器可刷新当前窗口，还需重启守护才能更新以后补注所用字典。当前不做守护词典热更新，避免状态复杂化；重注入旧观察器会停止，新旧词典下已翻译文本保持稳定并由回归测试覆盖。
- 旧守护进程不会热更新，升级后需 `--autostart off` 再 `--autostart on`，重新登录或手动启动新守护。
- `shouhu.log` / `shouhu.lock` 不提交；停止守护不代表停止 Antigravity。

### 只读诊断与连接生命周期

`--status` / `--doctor` 在正常字典加载、`prepare_instance()`、引擎构建之前分流。只验证本地词典和
已发现的 CDP；不启动/关闭应用、不注入、不改守护自启项、不创建日志/锁。两者退出码统一为
0 检查通过、1 未就绪或需要处理、2 参数错误；`--doctor` 多显示文件级校验和建议，不自动修复。

`engine.active()` 会执行 `ensureTrusted()`，可能停止观察器，**不能用于只读诊断**。
引擎维护纯数据 `state={started,stopped,entries}`；诊断以 own-property descriptor 读取，
不调用函数/getter、不读取 `misses`。旧引擎无快照则未知，不猜测运行正常。
窗口仅输出本次序号、固定状态、词条数；禁止输出标题、URL、target ID、完整 UA、配置或 CSRF。
词典检查保留 JSON pairs 以识别重复键，错误仅输出分类/文件名，不能回显原文或解析异常。
词条数量不同提示刷新；数量相同不是字典指纹，不代表内容一致。

每目标串行请求并递增 ID，忽略事件与旧请求回包；不使用多个同时 `recv()` 的协程。
取消必须向外传播，watch/CLI 在 finally/async context 关闭所有会话，关闭操作自身也受超时限制。
不要把每轮封进独立 `asyncio.run()`，那会销毁持久连接；也不要跨线程/事件循环复用 WebSocket。

### 翻译引擎

**匹配优先级**（2.29.0 调整：完整动态模板必须先于子串替换，避免改写变量）：

1. **整节点精确匹配** —— 优先级最高。句子里提到 Antigravity / MCP / Google AI 等产品名也照翻，
   `PROTECTED` 只管子串替换
2. **大小写归一匹配** —— 限 30 字符以内，防误匹配长句
3. **正则规则 `REGEX_RULES`** —— 带变量的动态文案（时间、模型名、邮箱、按键提示）；先尝试原文以保留变量中的空白和引号，再尝试归一化文案
4. **子串替换 `smartReplace()`** —— 仅 `phraseEntries`（key ≥ 30 字符的完整句子）参与，带词边界检查；含产品名或已有中文的文本不走子串替换，避免重注入时翻译英文变量

**监听策略**：`MutationObserver` 同时听 `childList` + `characterData` + `attributes`。
`characterData` **必须听**——React 重渲染直接改写文本节点，不听就会被改回英文。
同批次重复根节点和属性名会合并；若同一目标同时有边界和文案属性变化，以最终 DOM 状态递归重判子树；普通获准属性仅处理该属性。
保留 2/5 秒启动兜底及 3 秒属性重扫；不做周期性整页文本递归。

**性能**：`lastSeen` WeakMap 记住每个文本节点上次处理过的值，变更入口先做隐私祖先检查再读取/匹配。
`tests/test_performance_baseline.py` 用合成 DOM 记录初扫、批量文本/属性变更和空闲属性兜底耗时；数字只用于同环境前后比较，不设毫秒 CI 阈值，也不代表实机 UI 性能。

历史实测占用（2026-08 在约 1150 条词条、667 个 DOM 元素下测得）：JS 堆净增 586 KB；
全量重扫 0.4~0.7 ms；11 秒采样期内主线程长任务 0 次。当前词典及扫描策略不同，需在安全实机环境重新测量。
**量堆增量别直接比前后差值**——应用自身堆在 52~55 MB 间波动，几百 KB 会被噪声淹没；
正确做法是在页面里再构造一份等价结构、前后立刻取样取差值。

### 三条容易改错的不变量

**1. `norm()` 必须与 `normalize_text()` 逐条对应。**
字典 key 在加载时被归一化（压缩空白、弯引号 `’‘“”` 转半角）。页面文本不做同样处理就永远匹配不上。
界面里的 `Don't`、`Let's`、`agent's` 用的是 **U+2019 弯撇号**，不是 ASCII `'`。
曾出过 bug：JS 侧写成 `.replace(/[']/g, "'")`，字符类里只有 ASCII 引号，等于拿直引号换直引号——
纯空操作，所有含撇号的句子静默漏翻。

**2. 共享隐私规则先判定，再读取内容；类名区分片段与 token。**

| 类别 | 匹配方式 | 选错的后果 |
|---|---|---|
| `classFragments`（`monaco-editor`、`code-block` 等） | 子串 | —— 足够特异，不会误伤 |
| `classTokens`（`terminal`、`preview` 等） | HTML 空白分隔的 token 精确 | 子串会把 `terminal-settings-row` 等正常设置项误判为禁区 |

`privacy_policy.build_policy_js()` 返回局部策略对象，供引擎与采集表达式嵌入。保护检查必须在
`nodeValue` / 候选属性读取和 `lastSeen` 缓存之前；被保护内容不需要读取或缓存原文。
所有入口（首次、observer、重扫、重注入、Shadow）必须用同一策略，不可单独豁免属性。

祖先检查同时遍历普通 parent、`assignedSlot` 与 `ShadowRoot.host`，检查两条所有权路径，
不是只走 slot 或只走 parent。总预算 256 个节点，超限、循环、异常及 detached 节点拒绝；
不能再“25 层查不到就允许”。边界属性 class/contenteditable/testid/quotable 变化也需监听并重判。

2.29.0 已核验的精确保护标记包含 conversation-row-*、user-input-step、user-message-content/bubble、
data-quotable=true、附件、项目/会话/侧问题标题、section-header、Lexical 根及账号入口。
不要扩大成所有 `data-cascade-id` / `data-quotable-root`：它们含正常 composer 和工具栏。
tooltip portal 混有用户文件名，不能只凭 role=tooltip 就当固定 UI；当前保守拒绝。

会话/项目列表项的识别特征必须够精确：`DIV` + `relative` + `w-full` + `select-none` + `cursor-pointer`。
只判 `select-none cursor-pointer` 会误伤菜单栏按钮（File/View/Window），导致它们不翻译。

**3. `attachShadow` 劫持只做一次，回调动态读全局引擎，不能闭包捕获 observer。**
闭包捕获会让每次重复注入都永久持有一份旧引擎（连同整份字典约 0.6 MB），
而守护期会反复补注，泄漏不断叠加。当前实现用 `__ag_hooked__` 标记只劫持一次，
回调里读 `window.__ag_hanhua_engine__.observe`。实测连注 5 次仅增长 561 KB（≈ 一份字典）。
旧引擎 disconnect 或主界面身份失效时还要置 `stopped` 标记：迟到的重试定时器不能把刚断开的
observer 重新挂回 body。`active()` 必须复核身份，不能仅凭全局标记存在判断引擎可用。

`startEngine()` 保留 body 延迟出现的防御性重试：靠 `started` 标记 + `DOMContentLoaded` +
3 秒定时器兜底。生产入口必须经过 `guarded_expression()`；裸引擎的无 guard 模式只用于离线测试。

**属性翻译**：来源固定为 text、placeholder、title、aria-label、alt、data-tooltip-content、
aria-description、aria-placeholder。`data-title` 是动态分组标题，拒绝；普通 DIV/SPAN 的
title/aria-label 不自动视为固定元数据。按钮/明确控件语义及审核过的静态标记可按许可翻译，
但 hard 祖先永远优先；普通 INPUT/TEXTAREA 仅 placeholder/aria-placeholder 软例外，不读写 value。
文本及获准属性仍共用精确/大小写/模板规则，保留合法动态模板变量。

安全优先会使 section-header 内的固定 Pinned 标题、消息包装内动作、部分 tooltip 保留英文；
不要为恢复覆盖率跳过 hard 祖先。未知组件普通正文为兼容仍精确匹配，不能据此宣称未来任意版本都绝无误翻。
隐私版本 scope 为 `antigravity-main-v2`，不接受旧 v1 的 active 状态，补注会替换旧行为且不读取旧 misses。

## 字典约定

`dicts/*.json` 运行时全部合并成一个 map。`common.json` 放通用 UI 词（23 条），
`ui_v2.json` 放 2.0 界面文案（约 2400 条，2026-10 按 bundle 审计法从 2.23.1 补齐），
`ui_v2_225.json` 放 2.25.25 的增量（471 条），`ui_v2_229.json` 放 2.29.0 的增量（600 条）；总计 3495 条。**所有文件之间不得有重复 key**——
同一条翻译存在两处，改一处另一处就会漂移。`common.json` 是通用词的唯一归属地。
按版本拆文件而不是并进 `ui_v2.json`，是为了让「哪批词条对应哪次官方更新」一眼可查。

**重名不会报错，而是静默覆盖**：`load_dictionary()` 按 `sorted(os.listdir())` 顺序合并，
后加载的文件覆盖先前的（`common.json` → `ui_v2.json` → `ui_v2_225.json` → `ui_v2_229.json`）。
加词条前先跑 `python -m unittest discover -s tests -v` 的重复检查，别指望加载器替你拦。

新增翻译只需在 `dicts/` 下加 JSON，不用改代码。

### 统计与采集隐私

引擎 `misses={schema:1,sources:{固定来源:次数}}` 只留数字，每来源最多 1000000，
不存文本、上下文、选择器或文本哈希。WeakMap 避免同节点同值因重扫重复计数；
内容改变后可再次计数，同一文案多节点不是唯一词条数，重注入清零，已有中文或明显敏感形态不计。
`caiji.py --misses` 用 own-data descriptor 读取固定字段；旧 Set、getter、`active()` 都不得调用/遍历。

采集器使用进程/端口发现及同一次求值内 origin+主界面检查，不再 first HTTP page 或固定 9333。
默认不读 UI 值，仅计审核静态位置的来源，JSON `candidates=[]`；它不是英文/漏翻数量。
显式 `--include-ui-text` 才读审核的 10 个精确按钮/菜单标记，并核对对应固定原文白名单，
输出规范字面量而非 raw（防隐藏空白/上下文夹带）。未知组件即使普通 BUTTON 也不采原文。

报告 schema 1 固定 mode/text_included/windows/sources/withheld/candidates；Python 再重建固定字段，
拒绝非法类型/次数/文本，忽略额外页面字段。上限 1000 候选、120 字符、128 KiB；计数安全整数饱和。
控制台只输出数量、固定错误码和文件名，不输出候选或绝对路径。只有全部成功才原子覆盖，
失败保留旧文件；原子写入临时文件、pending/misses 报告均不提交。旧报告可能含私人原文，不能默认视为安全。

**官方更新后系统性地找漏翻（bundle 审计法）**：逐页跑 `caiji.py` 只覆盖你点到的那几页，
漏掉的页面永远不会暴露。要一次拿全，直接从 SPA 的 JS bundle 里挖——界面全部文案都在里面。

1. 带调试端口启动后，在页面里 `performance.getEntriesByType('resource')` 列出加载的 .js，
   只有两个：`https://127.0.0.1:<端口>/main.js`（约 10 MB，Closure 编译产物，文案全在这里）
   和 `prism_bundle.js`（只是语法高亮）。用 `curl -sk --noproxy '*'` 下载。
2. **比对前先把 bundle 里的弯引号归一成半角。** 字典 key 在加载时已被 `normalize_text()`
   归一，而 bundle 里 `Don't` 用的是 U+2019——不归一，711 条长 key 只匹配上 326 条（46%），
   会误判成「大批词条失效」。用 1:1 字符映射替换，字节偏移不变，可继续按偏移定位。
3. 抽字符串字面量，与字典做**双向 diff**：
   - 「字典有、bundle 没有」= 官方改写了原文（2026-10 那次 470 条，其中 13 条高相似度）。
     用词重叠预筛 + `difflib` 相似度 ≥ 0.85 能自动配对回旧译文，剩下的才人工看。
     相似度阈值别放太低——0.72 档开始大量误配（`Clear conversation` → `New conversation`）。
   - 「bundle 有、字典没有」= 新增文案。
4. **噪声过滤是成败关键。** bundle 里绝大多数字面量不是界面文案，按特征剔除：
   无大写字母（Tailwind/CSS 类名）、含 `[0-9],[0-9]` 或数字占比 >20%（SVG 路径）、
   出现 node/edge/port/graph/layout（ELK 图布局库的选项说明）、
   `must be used within` / `A generic type` / `Invalid quantifier`（React/Redux/TS/正则解析器的
   开发期报错）、全大写 `[A-Z0-9_]+`（枚举常量）、`xxx.yyy` 或 `a/b`（内部设置项与命令 ID）。
5. **最可靠的精度信号是邻近度**：真界面文案必然紧邻字典里已有的词条，库文案自成区域。
   按「离最近字典词条的字节距离」排序，距离个位数的基本全是真文案。
6. **2026-10（2.25.25）实测更准的信号：按 UI 属性名抓。** 界面文案在 bundle 里几乎都以
   `{title:"…", description:"…", label:"…", tooltipText:"…", placeholder:"…"}`
   的对象属性形式出现，库报错/调试字符串不会用这些属性名。正则
   `(?:title|description|label|tooltipText|placeholder|…)\s*:\s*("…")`
   抽出来的 1472 条里 745 条直接命中字典，剩下的质量远高于全量字面量。
   两种方法互补：属性名法精度高、覆盖面窄（不含 `A.createElement("div",null,"文案")`
   这种直接子节点写法），邻近度法补漏。
7. **过滤字符集别收太紧**：`@`（`Ask anything, @ to mention`）、`!`、`&`
   （`Terms of Service & Data Use`）都在真实文案里。第一轮把 `@` 放进排除集，
   整类文案被静默丢弃，`Ask anything, @ to mention` 就是这么漏掉的。
8. **2.25.25 这轮的实际产出**：新增 471 条（`dicts/ui_v2_225.json`），字典总数 2424 → 2895。
   官方改写只配出 4 条高相似度，绝大多数「字典有、bundle 没有」是 1.x 遗留的
   VS Code/Monaco 菜单词条（`Show in Finder`、`Select Line`、`Clear Terminal`），
   不是失效文案，**别去删它们**——它们不会命中，但也无害。
9. **2.29.0（2026-11）**：`main.js` 为 11,187,917 字节，SHA-256 为
   `8d1196a22cd3eb9779e8faf5c31eb732d3df8d9d037b685733ed4e3d6cdfb5a2`。
   安装目录 `resources/bin/language_server.exe` 内嵌 ZIP 中的 `main.js` 与本机界面服务返回文件一致；只读提取即可离线审计，不必重启应用。
   调试端口与界面端口不是同一个，不能固定下载 `https://127.0.0.1:9333/main.js`；从 `/json` 的主页面 URL / resource entries 确定实际地址。
   本轮新增 600 条：属性文案 + 直接文本节点 + 实机未命中补漏；产品名、库内部标识、用户内容不收录。录音计数、侧边问题计数、搜索结果模板走 `REGEX_RULES`。
   `Search conversations with` 等片段经 bundle / DOM 确认是图标两侧的实际文本节点，不能把含 Markdown 图标的整句原始消息直接当 DOM 词条。
   引擎补齐既有 Shadow DOM 的遍历/观察与 host 禁区回溯、可编辑属性枚举保护，并让 JS 空白归一集合与 Python 一致。

现在可用只读 `audit_bundle.py --bundle <本地 main.js> --version <已确认版本> --output audit-report.json`
固化这套流程。它不联网、不连 CDP、不读取运行页面；报告包含 bundle/词典 SHA-256、字符串位置与
属性/直接文本证据，双向 diff 分类为 `new-candidate`、`possible-rewrite`、`not-found-in-bundle`，
另列动态模板、术语、占位符和归一化重复键。相似度默认 `>=0.85` 且只能人工复核；`not-found` 可能是
改写、条件模块或旧词条，不能自动删除。不要把 `--output` 指向词典目录，报告也不能自动回写字典。

CI 运行 `python -B -m unittest discover -s tests -v`、`compileall`、启动器 ASCII 检查及提交空白检查；
独立浏览器作业安装 Playwright Chromium，运行 `tests/test_browser_fixture.py` 的合成页面测试。

**三条铁律**：

1. **只写完整的 UI 字符串，绝不写半截短语。** 加 `safety barriers` 这种碎片，
   `smartReplace` 会把整句变成「Disables all 安全屏障 for 最大迭代 velocity.」这类中英混杂。
   ≥ 30 字符的 key 会参与子串替换，碎片一旦超过这个长度危害最大。缺哪句就补哪句的完整原文。
   **唯一例外：React 把一句拆成多个文本节点时，节点本身就是独立渲染单元。**
   这时按**节点原文**收录不算半截短语（例：`Configure the browser subagent. It requires` +
   链接 `Google Chrome` + `to be installed.` 是三个节点）。但必须先在 bundle 里确认
   `A.createElement(..., "片段")` 的写法，不能凭猜测截取；能拼成整句的（`A.Fragment`
   里连续的 `"…","…"`）仍应优先走 `REGEX_RULES` 或整句 key。
2. **避免翻用户内容**：已识别的会话标题、文件路径和邮箱区域会受保护；未知组件边界并非绝对保证。
   产品名（Antigravity / Gemini / Google AI / Jetski / Marketplace）保持英文，产品名列在 `PROTECTED` 里，
   而 `PROTECTED` **只放 Antigravity 自身的产品名词**——第三方/用户自装的技能名是每台机器
   不一样的本地配置，写死进去既泄露环境又对别人无用。
3. **带变量的文案走 `REGEX_RULES`，不写死进字典。** 像
   `Select model, current: Gemini 3.7 Flash High` 这种把具体模型名写进 key 的条目，
   官方一换模型就整条失效。

术语约定：Agent → **智能体**。已安装技能的名称与描述按设计保持英文，不纳入字典。

## 隐私红线

`pending.json` / `misses.json` 及原子写入临时文件**已在 `.gitignore` 里，绝不提交**。
旧版页面采集曾夹带账号邮箱，已有报告不能因升级就视为安全，新代码也不会自动清理历史文件。
当前 schema 1 默认只保存固定来源计数；显式 `--include-ui-text` 也只接受审核标记与固定标签的双白名单，
不再通过通用敏感词过滤后导出任意页面原文。未知文案须人工核对或离线审计，勿粘贴私人内容。

同理，任何「当前机器上有什么」的信息（已装技能名、用户目录、账号）都不该硬编码进源码。
安装路径必须从 `%LOCALAPPDATA%` 取，不能写死 `C:\Users\...`。
打印环境变量前先脱敏：`HTTP_PROXY` 这类地址可能内嵌 `user:pass@`，用户把日志贴进
issue 就泄露了，`_mask_proxy()` 就是干这个的。

## 转义陷阱（这个项目最容易踩的坑）

`engine_template.js` 是独立 JavaScript 文件，不经过 Python 字符串转义。
但诊断/身份表达式和通过 Python 拼装的 CDP 表达式仍有 Python 与 JavaScript 两层，**反斜杠要过两层**。

- Python 普通三引号串里写 `\s` 会触发 `SyntaxWarning` 且语义可疑；写 `\\s` 才会在 JS 里得到 `\s`。改引擎优先改 `engine_template.js`，勿把它重新内嵌回 Python。
- 写 CDP 调试脚本时更要小心：转义层一丢反斜杠，`/\s+/g` 就变成 `/s+/g`——
  把文本里每个 `s` 换成空格。曾因此得到 "Choo e"、"ecurity"、"Be t of N" 这种**假残缺**，
  据此下了完全错误的结论，还往字典里加了一批永远匹配不上的坏键
（2026-08 审计时已按「小写开头且是更长 key 子串」的标准清理掉 27 条，偶有残留照此清）。
- **对策**：调试脚本里一律用无反斜杠的等价写法 `[ ]`、`[0-9]`、`[.]`、`[A-Za-z]`。
  引擎内的 `cnDuration()`/`REGEX_RULES` 就是按这个规矩写的，别「顺手改回」`\d`。
- Git Bash 的 heredoc 会吞 `\\`——**引号括起的 heredoc（`<<'EOF'`）也一样**，且坑在生成
  那一步：磁盘上的文件已经是单反斜杠，Python 源码里的 `\\r` 退化成真实换行符，把内嵌
  JS 的正则切断（表现为 SyntaxError: missing /）。含反斜杠的 .py 一律用文件写入工具
  生成，别用 heredoc；CDP 调试脚本尽量写成零反斜杠（不用正则、不做换行清洗）。

## 排查经验

- **「注入成功但界面全英文」先查重载与目标识别。** 在已确认的主窗口检查
  `window.__ag_hanhua_engine__?.active?.()`；只看标记存在或标题都不够。观察期已结束时重跑
  `--no-launch`；看到 `hub-mount-loading` 则等挂载稳定，不能移除身份检查来强行注入。
- **「翻译了又变回英文」≠ 没匹配上**，通常是 React 重渲染覆盖，查 `characterData` 是否在监听。
- **整句在字典里却不翻**，先查是不是被 `isProtected` 或禁区规则整段拦掉了。
- **tooltip 只在悬停时才进 DOM**，但采集器会拒绝 `role=tooltip` 区域，且只导出审核过的静态标签；不要用 `caiji.py` 抓任意 tooltip。
- **tooltip 原文不必从运行 DOM 采集**：相关页面打开后组件 JS 已加载，在页面里 `fetch`
  `performance.getEntriesByType('resource')` 列出的 .js 资源再搜字符串字面量即可拿到全文，
  连模板拼接的动态文案（如 `` `${key} Sends immediately` ``）都能看到源码。
  过滤 URL 时别用 `endsWith('.js')`——带查询串或 `.mjs` 的 chunk 会被漏掉
  （曾因此只扫到 2 个文件），用 `includes('.js')` 一类的宽松匹配。
  比悬停采集可靠，还能顺带发现 Mac 专属变体；新增正则规则前先这样确认拼接方式。
  注意别只搜 tooltip 文本本身——渲染处附近（`data-tooltip-id`、`content:"..."`）才是数据源。
- **别用合成事件批量触发 hover** —— 试过两次都把应用挂死（2 分钟超时）。要审计 tooltip，
  从 bundle 静态审计并人工核对候选；若确需验证显示效果，手动悬停查看翻译，不导出运行时原文。
- **`qidong.bat` 必须是纯 ASCII**：cmd 按 GBK 解析 .bat，写中文会变乱码并被当成命令执行。
  同理别用 `timeout /t`（stdin 被重定向时直接报错退出），延时统一用 `ping -n N 127.0.0.1`。
- **在 Git Bash 里测 Windows 命令要加 `MSYS_NO_PATHCONV=1`**，否则 `taskkill /f` 的 `/f`
  会被转成路径 `F:/` 而报「无效参数」。用户双击 .bat 时不受影响。
- **本机开了 HTTP 代理（Clash 等）会让 CDP 连不上**：`urllib` 会把 `http://127.0.0.1:9333`
  的请求打进代理。`main()` 开头的 `_bypass_proxy_for_local_cdp()` 已把本机地址并入
  `NO_PROXY`，新增代码别在它之前对调试端口发请求。
- **Windows 进程枚举必须容忍本地编码。** `cdp_discovery.py` 读取 `tasklist /FO CSV /NH` 的原始 bytes，
  用替换式解码解析 ASCII 可执行文件名/PID，忽略本地化列，避免 GBK 解码异常；命令失败或无有效表须报错，不能当成无进程。
  所有后台子进程带 `CREATE_NO_WINDOW`，不能为了复用端口引入闪黑窗。
- **不要轻信自己写的扫描脚本。** 这个项目里多次出现「扫描说没问题、用户说有问题」，
  每次都是扫描器自身有 bug。以用户看到的界面为准。
- **改完 CLI 参数或用户可见行为，记得同步 `README.md`**——它是这个项目对外的唯一门面，
  用法、原理、限制三段都直接引用脚本行为，漂移了读者会照着过期的说明操作。

## 已知取舍

`smartReplace` 命中时写回的是 `norm()` 后的文本，会压掉原文本节点的首尾空白，
理论上能让 `"Hello "` + `<b>x</b>` 渲染成 `"Hellox"`。实际界面未观察到；
修它要重构归一化流程，风险大于收益。**若日后见到词粘连，先怀疑这里。**
