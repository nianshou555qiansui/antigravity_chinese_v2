# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

给 Antigravity 2.0 做界面汉化。核心思路是**运行时注入**：以 `--remote-debugging-port` 启动
Antigravity，连 Chrome DevTools 协议（CDP）把一段 MutationObserver 翻译引擎注入渲染页面。
**不修改任何安装文件**（不碰 `Antigravity.exe`、`app.asar`、`product.json`），因此没有还原逻辑，
官方更新也不会破坏安装。

依赖：Python 3 + `websockets`（本机实测 Python 3.14.7 / websockets 16.0）。无构建、无测试框架。

### 为什么不能沿用 1.x 的做法

上游 `github.com/cshitian/antigravity_chinese` 针对 Antigravity 1.x：向
`resources\app\out\vs\...\workbench.html` 注入 `<script>` 并同步 `product.json` checksum。
2.0 把 `resources/app` 打包成了 `app.asar`，界面改由本地 HTTPS 服务渲染
（`https://127.0.0.1:<随机端口>/`），那个注入点在文件系统层面已不存在，且 `inject_html()`
对不存在的文件**静默返回 False**。上游 Issue #4 记录了这个失效，作者无回应。

## 常用命令

```bash
python hanhua_v2.py                      # 自动探测 + 启动 + 注入（qidong.bat 就是调它）
python hanhua_v2.py --no-launch          # 只注入到已带调试端口运行的实例（开发时最常用）
python hanhua_v2.py --watch 0            # 关掉守护，注入完立刻退出
python hanhua_v2.py --install-dir "D:\Antigravity" --port 9333
python caiji.py                          # 抓当前页面未翻译英文 → pending.json
```

改完代码后的自查（没有测试框架，靠这几条）：

```bash
python -c "import ast,io;[ast.parse(io.open(f,encoding='utf-8').read()) for f in ['hanhua_v2.py','caiji.py']]"
python -c "import json,io;[json.load(io.open(f,encoding='utf-8')) for f in ['dicts/common.json','dicts/ui_v2.json']]"
# 引擎 JS 必须单独校验：它是 Python 字符串拼出来的，语法错在注入时才会暴露
python -c "import importlib.util,io;s=importlib.util.spec_from_file_location('h','hanhua_v2.py');h=importlib.util.module_from_spec(s);s.loader.exec_module(h);io.open('/tmp/e.js','w',encoding='utf-8').write(h.build_engine_js(h.load_dictionary()))"
node --check /tmp/e.js
python -c "b=open('qidong.bat','rb').read();assert not [x for x in b if x>127],'bat 混入非 ASCII'"
```

## 架构

```
hanhua_v2.py     主脚本：加载字典 → 生成引擎 JS → 启动 Antigravity → CDP 注入 → 守护
  ├ load_dictionary()   合并 dicts/*.json，对 key 做 normalize_text()
  ├ build_engine_js()   把字典嵌进一段 IIFE 模板字符串（整个翻译引擎都在这里面）
  ├ wait_for_debug_port() 轮询 /json 找主界面页
  └ cdp_inject()        等就绪 → 注册 document-start 脚本 → 注入 → 守护补注
caiji.py         界面文本采集器（CDP 抓页面英文 → pending.json）
qidong.bat       一键启动器（taskkill → 调 hanhua_v2.py）
dicts/*.json     字典，格式 {"英文原文": "中文译文"}
```

**注意 `build_engine_js()` 的返回值是字符串**：整个翻译引擎（约 300 行 JS）以 Python 三引号
字符串形式存在于 `hanhua_v2.py` 里，靠 `DICT_PLACEHOLDER` 占位替换注入字典。改 JS 时
Python 的字符串转义层会介入，见下文「转义陷阱」。

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

### 翻译引擎

**匹配优先级（顺序不能动）**：

1. **整节点精确匹配** —— 优先级最高。句子里提到 Antigravity / MCP / Google AI 等产品名也照翻，
   `PROTECTED` 只管子串替换
2. **大小写归一匹配** —— 限 30 字符以内，防误匹配长句
3. **子串替换 `smartReplace()`** —— 仅 `phraseEntries`（key ≥ 30 字符的完整句子）参与，带词边界检查
4. **正则规则 `REGEX_RULES`** —— 带变量的动态文案（时间、模型名、邮箱、按键提示）

**监听策略**：`MutationObserver` 同时听 `childList` + `characterData` + `attributes`。
`characterData` **必须听**——React 重渲染直接改写文本节点，不听就会被改回英文。
另有每 3 秒一次的全量重扫兜底（下拉菜单等临时弹出内容靠它）。

**性能**：`lastSeen` WeakMap 记住每个文本节点上次处理过的值，没变就跳过。这是关键优化——
没有它，每 3 秒的全量重扫都要对每个节点做一遍昂贵匹配。

实测占用（1125 条词条、667 个 DOM 元素）：JS 堆净增 586 KB（≈ Antigravity 总内存 640 MB 的 0.09%）；
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
`ui_v2.json` 放 2.0 界面文案（约 1080 条）。**两个文件不得有重复 key**——
同一条翻译存在两处，改一处另一处就会漂移。`common.json` 是通用词的唯一归属地。

新增翻译只需在 `dicts/` 下加 JSON，不用改代码。

**三条铁律**：

1. **只写完整的 UI 字符串，绝不写半截短语。** 加 `safety barriers` 这种碎片，
   `smartReplace` 会把整句变成「Disables all 安全屏障 for 最大迭代 velocity.」这类中英混杂。
   ≥ 30 字符的 key 会参与子串替换，碎片一旦超过这个长度危害最大。缺哪句就补哪句的完整原文。
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

## 转义陷阱（这个项目最容易踩的坑）

翻译引擎是 Python 字符串里的 JS，**反斜杠要过两层**。

- Python 三引号串里写 `\s` 会触发 `SyntaxWarning` 且语义可疑；写 `\\s` 才会在 JS 里得到 `\s`。
- 写 CDP 调试脚本时更要小心：转义层一丢反斜杠，`/\s+/g` 就变成 `/s+/g`——
  把文本里每个 `s` 换成空格。曾因此得到 "Choo e"、"ecurity"、"Be t of N" 这种**假残缺**，
  据此下了完全错误的结论，还往字典里加了一批永远匹配不上的坏键
（2026-08 审计时已按「小写开头且是更长 key 子串」的标准清理掉 27 条，偶有残留照此清）。
- **对策**：调试脚本里一律用无反斜杠的等价写法 `[ ]`、`[0-9]`、`[.]`、`[A-Za-z]`。
  引擎内的 `cnDuration()`/`REGEX_RULES` 就是按这个规矩写的，别「顺手改回」`\d`。
- Git Bash 的 heredoc 会吞掉 `\\`。在 heredoc 里写 Python 正则时用 `chr(92)` 拼反斜杠。

## 排查经验

- **「注入成功但界面全英文」= 注入太早。** 看 `[连接] 页面:` 那行，**title 为空**就是铁证。
  验证：注入几秒后查 `window.__ag_hanhua_engine__`，是 `false` 说明被冲掉了。
- **「翻译了又变回英文」≠ 没匹配上**，通常是 React 重渲染覆盖，查 `characterData` 是否在监听。
- **整句在字典里却不翻**，先查是不是被 `isProtected` 或禁区规则整段拦掉了。
- **tooltip 只在悬停时才进 DOM**，静态扫描抓不到它的文案，要补 tooltip 翻译得在悬停时采集。
- **tooltip 原文不必悬停采集**：相关页面打开后组件 JS 已加载，在页面里 `fetch`
  `performance.getEntriesByType('resource')` 列出的 .js 资源再搜字符串字面量即可拿到全文，
  连模板拼接的动态文案（如 `` `${key} Sends immediately` ``）都能看到源码。
  比悬停采集可靠，还能顺带发现 Mac 专属变体；新增正则规则前先这样确认拼接方式。
  注意别只搜 tooltip 文本本身——渲染处附近（`data-tooltip-id`、`content:"..."`）才是数据源。
- **别用合成事件批量触发 hover 采集** —— 试过两次都把应用挂死（2 分钟超时）。改用被动采集器
  （挂个定时器收集 tooltip 容器）+ 人工悬停。
- **`qidong.bat` 必须是纯 ASCII**：cmd 按 GBK 解析 .bat，写中文会变乱码并被当成命令执行。
  同理别用 `timeout /t`（stdin 被重定向时直接报错退出），延时统一用 `ping -n N 127.0.0.1`。
- **在 Git Bash 里测 Windows 命令要加 `MSYS_NO_PATHCONV=1`**，否则 `taskkill /f` 的 `/f`
  会被转成路径 `F:/` 而报「无效参数」。用户双击 .bat 时不受影响。
- **不要轻信自己写的扫描脚本。** 这个项目里多次出现「扫描说没问题、用户说有问题」，
  每次都是扫描器自身有 bug。以用户看到的界面为准。

## 已知取舍

`smartReplace` 命中时写回的是 `norm()` 后的文本，会压掉原文本节点的首尾空白，
理论上能让 `"Hello "` + `<b>x</b>` 渲染成 `"Hellox"`。实际界面未观察到；
修它要重构归一化流程，风险大于收益。**若日后见到词粘连，先怀疑这里。**
