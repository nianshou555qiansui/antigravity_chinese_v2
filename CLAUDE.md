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
python caiji.py                          # 抓当前页面未翻译英文 → pending.json
python caiji.py --misses                 # 拉引擎未命中日志 → misses.json（需已注入）
```

改完代码后的自查：先跑离线回归（不会启动或修改 Antigravity），再按改动做实机验证。

```bash
python -m unittest discover -s tests -v
python -c "import ast,io;[ast.parse(io.open(f,encoding='utf-8').read()) for f in ['hanhua_v2.py','caiji.py','shouhu.py']]"
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
  ├ build_engine_js()   把字典嵌进一段 IIFE 模板字符串（整个翻译引擎都在这里面）
  ├ wait_for_debug_port() 轮询 /json 找主界面页
  └ cdp_inject()        等就绪 → 注册 document-start 脚本 → 注入 → 守护补注
caiji.py         界面文本采集器（CDP 抓页面英文 → pending.json；--misses 拉取引擎未命中日志 → misses.json）
cdp_discovery.py  进程归属、回环端口及产品校验；显式确认后的定向关闭
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

**注意 `build_engine_js()` 的返回值是字符串**：整个翻译引擎（约 360 行 JS）以 Python 三引号
字符串形式存在于 `hanhua_v2.py` 里，靠 `DICT_PLACEHOLDER` 占位替换注入字典。改 JS 时
Python 的字符串转义层会介入，见下文「转义陷阱」。

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

`wait_for_debug_port()` 每次重试都重新验证进程归属、产品 User-Agent 和本地 WebSocket 地址。
当前优先精确标题 Antigravity，多本地页面有歧义则拒绝；完整主界面特征识别与多窗口支持见 TODO 第 2 项。

### 注入失败的显式上报

`Runtime.evaluate` 的 JS 异常**不在**顶层 `error` 字段里，而在 `result.exceptionDetails`。
`main()` 必须显式查它：不查的话引擎脚本抛错时脚本照样打印「[成功]」，制造
「注入成功但界面全英文」的假象，把排查引向错误方向（见「排查经验」第一条）。

### 注入时机 —— 冷启动能否成功的关键

**端口开放 ≠ 界面就绪。** 冷启动时 CDP 端口比 SPA 挂载早得多，此时页面 `title` 还是空的，
直接注入会被随后的加载/导航**整个冲掉**，现象是脚本打印「[成功]」但界面全是英文。
`cdp_inject()` 因此分三步：

1. **等就绪** —— 轮询 `document.readyState === 'complete'` 且 `body.innerText` 超过 100 字符
2. **注册 document-start 脚本** —— `Page.addScriptToEvaluateOnNewDocument`。
   这个注册**只在 CDP 连接存活期间有效**，连接一断浏览器就清掉它，所以它只是加速手段，不是保险
3. **守护补注** —— 每 1.5 秒查 `window.__ag_hanhua_engine__`，被冲掉立刻补注，默认 30 秒。
   这一步才是真正的保险

`main()` 外层还包了 3 次重试：早期 page target 可能在加载途中被丢弃，WebSocket 会直接断开。

### 守护进程（shouhu.py，可选，仅 Windows）

`--autostart on` 建登录启动项，守护每轮结束后等待 2 秒。自动发现/验证端口后复用 `cdp_inject()`；
没有端口、页面未就绪、探测或注入失败都只等待/记录状态，不启动、关闭或重启 Antigravity。
旧版的自动 taskkill、冷却时间和失败次数安全阀已删除；不要重新引入静默强制重启。

- pythonw 子进程必须带 `CREATE_NO_WINDOW`，避免闪黑窗。
- 状态变化才记日志，探测失败不是“进程不存在”；连接复用和请求超时后续按 TODO 第 3 项处理。
- 旧守护进程不会热更新，升级后需 `--autostart off` 再 `--autostart on`，重新登录或手动启动新守护。
- `shouhu.log` / `shouhu.lock` 不提交；停止守护不代表停止 Antigravity。

### 翻译引擎

**匹配优先级**（2.29.0 调整：完整动态模板必须先于子串替换，避免改写变量）：

1. **整节点精确匹配** —— 优先级最高。句子里提到 Antigravity / MCP / Google AI 等产品名也照翻，
   `PROTECTED` 只管子串替换
2. **大小写归一匹配** —— 限 30 字符以内，防误匹配长句
3. **正则规则 `REGEX_RULES`** —— 带变量的动态文案（时间、模型名、邮箱、按键提示）；先尝试原文以保留变量中的空白和引号，再尝试归一化文案
4. **子串替换 `smartReplace()`** —— 仅 `phraseEntries`（key ≥ 30 字符的完整句子）参与，带词边界检查；含产品名或已有中文的文本不走子串替换，避免重注入时翻译英文变量

**监听策略**：`MutationObserver` 同时听 `childList` + `characterData` + `attributes`。
`characterData` **必须听**——React 重渲染直接改写文本节点，不听就会被改回英文。
另有每 3 秒一次的全量重扫兜底（下拉菜单等临时弹出内容靠它）。

**性能**：`lastSeen` WeakMap 记住每个文本节点上次处理过的值，没变就跳过。这是关键优化——
没有它，每 3 秒的全量重扫都要对每个节点做一遍昂贵匹配。

实测占用（2026-08 在约 1150 条词条、667 个 DOM 元素下测得；字典 2026-10 已增至约 2900 条，
下列数字是最低值，改动字典后应重测）：JS 堆净增 586 KB（≈ Antigravity 总内存 640 MB 的 0.09%）；
全量重扫 0.4~0.7 ms；11 秒采样期内主线程长任务 0 次。
**量堆增量别直接比前后差值**——应用自身堆在 52~55 MB 间波动，几百 KB 会被噪声淹没；
正确做法是在页面里再构造一份等价结构、前后立刻取样取差值。

### 三条容易改错的不变量

**1. `norm()` 必须与 `normalize_text()` 逐条对应。**
字典 key 在加载时被归一化（压缩空白、弯引号 `’‘“”` 转半角）。页面文本不做同样处理就永远匹配不上。
界面里的 `Don't`、`Let's`、`agent's` 用的是 **U+2019 弯撇号**，不是 ASCII `'`。
曾出过 bug：JS 侧写成 `.replace(/[']/g, "'")`，字符类里只有 ASCII 引号，等于拿直引号换直引号——
纯空操作，所有含撇号的句子静默漏翻。

**2. 禁区类名分两类匹配，选错会造成方向相反的两种事故。**

| 类别 | 匹配方式 | 选错的后果 |
|---|---|---|
| `BLOCKED_CLASS_SUBSTR`（`monaco-editor`、`code-block` 等多词类名） | 子串 | —— 足够特异，不会误伤 |
| `BLOCKED_CLASS_TOKEN`（`terminal`、`preview` 等通用单词） | class token 精确 | 用子串会把 `terminal-settings-row` 这类正常设置容器整块判成禁区，整片界面漏翻 |

`isInBlockedZone()` 向上回溯 **25 层**（不是 12）：Tailwind/React 嵌套很深，12 层够不到上层的
禁区容器，用户的代码/会话内容就会漏出来被翻译。这段只对「值变过」的节点跑，代价可忽略。

会话/项目列表项的识别特征必须够精确：`DIV` + `relative` + `w-full` + `select-none` + `cursor-pointer`。
只判 `select-none cursor-pointer` 会误伤菜单栏按钮（File/View/Window），导致它们不翻译。

**3. `attachShadow` 劫持只做一次，回调动态读全局引擎，不能闭包捕获 observer。**
闭包捕获会让每次重复注入都永久持有一份旧引擎（连同整份字典约 0.6 MB），
而守护期会反复补注，泄漏不断叠加。当前实现用 `__ag_hooked__` 标记只劫持一次，
回调里读 `window.__ag_hanhua_engine__.observe`。实测连注 5 次仅增长 561 KB（≈ 一份字典）。
旧引擎 disconnect 时还要置 `stopped` 标记：document-start 路径下 `startEngine` 尚未成功，
迟到的重试定时器会把刚断开的 observer 重新挂回 body，新旧引擎叠加成双重翻译。

另外 `startEngine()` 必须可重试：document-start 注入路径下 `document.body` 还不存在，
只跑一次会永远挂不上 observer。当前靠 `started` 标记 + `DOMContentLoaded` + 3 秒定时器兜底。

**属性翻译**：`TRANSLATABLE_ATTRS` 覆盖 `placeholder`/`title`/`aria-label`/`alt`/`data-title`/
`data-tooltip-content` 等。属性**不查禁区**——它是 UI 元数据而非用户输入，查了会漏翻输入框提示。
属性与文本节点共用 `translateAttrValue()` 的同一套规则（精确 → 大小写归一 → 正则），
否则 `Send feedback as X` 这类动态文案作为 `aria-label` 出现时翻不了。

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

官方更新后的词条漂移检查：引擎把「含英文、未进禁区、但没翻出来」的文本记进
`misses`（内存 Set，上限 2000，重复注入即重置；只记文本节点不记属性——属性
不查禁区，会话标题这类用户内容会混进来）。`python caiji.py --misses` 拉取，
产物 `misses.json` 已进 .gitignore。

**重新注入后 `misses` 会有假阳性**：新引擎带一份空的 `lastSeen`，首次全量扫描会撞上
页面上**上一次注入留下的中文译文**。这些译文若保留拉丁字母（产品名、模型名，如
`Gemini 4 Argon 现已向付费 Pro 套餐订阅者开放。`）就过不了「无英文字母」的短路，
一路走到最后被记成未命中。排查时先滤掉含中文的条目——它们不是缺口。
（`misses` 里含中文 = 已翻过；真正的缺口是纯英文条目。）

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

**三条铁律**：

1. **只写完整的 UI 字符串，绝不写半截短语。** 加 `safety barriers` 这种碎片，
   `smartReplace` 会把整句变成「Disables all 安全屏障 for 最大迭代 velocity.」这类中英混杂。
   ≥ 30 字符的 key 会参与子串替换，碎片一旦超过这个长度危害最大。缺哪句就补哪句的完整原文。
   **唯一例外：React 把一句拆成多个文本节点时，节点本身就是独立渲染单元。**
   这时按**节点原文**收录不算半截短语（例：`Configure the browser subagent. It requires` +
   链接 `Google Chrome` + `to be installed.` 是三个节点）。但必须先在 bundle 里确认
   `A.createElement(..., "片段")` 的写法，不能凭猜测截取；能拼成整句的（`A.Fragment`
   里连续的 `"…","…"`）仍应优先走 `REGEX_RULES` 或整句 key。
2. **不翻用户内容**：会话标题、文件路径、邮箱、产品名（Antigravity / Gemini / Google AI /
   Jetski / Marketplace）、技能名一律保持英文。产品名列在 `PROTECTED` 里，
   而 `PROTECTED` **只放 Antigravity 自身的产品名词**——第三方/用户自装的技能名是每台机器
   不一样的本地配置，写死进去既泄露环境又对别人无用。
3. **带变量的文案走 `REGEX_RULES`，不写死进字典。** 像
   `Select model, current: Gemini 3.7 Flash High` 这种把具体模型名写进 key 的条目，
   官方一换模型就整条失效。

术语约定：Agent → **智能体**。已安装技能的名称与描述按设计保持英文，不纳入字典。

## 隐私红线

`pending.json` 是采集器产物，**已在 `.gitignore` 里，绝不提交**。它是某台机器某一次的界面快照，
`caiji.py` 的过滤（JS 侧 `isUsableEnglish` + Python 侧 `SENSITIVE` 二次过滤，拦下即不落盘不上屏）
是尽力而为不是保证——**实测曾夹带过账号邮箱**（`Send feedback as <邮箱>`），二次过滤就是为它加的。

同理，任何「当前机器上有什么」的信息（已装技能名、用户目录、账号）都不该硬编码进源码。
安装路径必须从 `%LOCALAPPDATA%` 取，不能写死 `C:\Users\...`。
打印环境变量前先脱敏：`HTTP_PROXY` 这类地址可能内嵌 `user:pass@`，用户把日志贴进
issue 就泄露了，`_mask_proxy()` 就是干这个的。

## 转义陷阱（这个项目最容易踩的坑）

翻译引擎是 Python 字符串里的 JS，**反斜杠要过两层**。

- Python 三引号串里写 `\s` 会触发 `SyntaxWarning` 且语义可疑；写 `\\s` 才会在 JS 里得到 `\s`。
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

- **「注入成功但界面全英文」= 注入太早。** 看 `[连接] 页面:` 那行，**title 为空**就是铁证。
  验证：注入几秒后查 `window.__ag_hanhua_engine__`，是 `false` 说明被冲掉了。
- **「翻译了又变回英文」≠ 没匹配上**，通常是 React 重渲染覆盖，查 `characterData` 是否在监听。
- **整句在字典里却不翻**，先查是不是被 `isProtected` 或禁区规则整段拦掉了。
- **tooltip 只在悬停时才进 DOM**，静态扫描抓不到它的文案，要补 tooltip 翻译得在悬停时采集。
- **tooltip 原文不必悬停采集**：相关页面打开后组件 JS 已加载，在页面里 `fetch`
  `performance.getEntriesByType('resource')` 列出的 .js 资源再搜字符串字面量即可拿到全文，
  连模板拼接的动态文案（如 `` `${key} Sends immediately` ``）都能看到源码。
  过滤 URL 时别用 `endsWith('.js')`——带查询串或 `.mjs` 的 chunk 会被漏掉
  （曾因此只扫到 2 个文件），用 `includes('.js')` 一类的宽松匹配。
  比悬停采集可靠，还能顺带发现 Mac 专属变体；新增正则规则前先这样确认拼接方式。
  注意别只搜 tooltip 文本本身——渲染处附近（`data-tooltip-id`、`content:"..."`）才是数据源。
- **别用合成事件批量触发 hover 采集** —— 试过两次都把应用挂死（2 分钟超时）。改用被动采集器
  （挂个定时器收集 tooltip 容器）+ 人工悬停。
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
