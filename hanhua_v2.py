#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Antigravity 2.0 运行时汉化工具 V1.0 —— CDP 注入版

与旧版（AntigravityHanHua_GongJu.py）的区别：
- 旧版针对 1.x 解包结构（resources\\app\\out\\...\\workbench.html 文件注入 + checksum 同步），
  在 2.0 上失效（2.0 打包为 app.asar，界面改为本地 HTTP 服务，见 GitHub Issue #4）。
- 本版不修改任何安装文件：通过 --remote-debugging-port 启动 Antigravity，
  连 DevTools 协议（CDP）把翻译引擎注入渲染页面。

纯 Python 标准库 + 本机已装 websockets 库，无其它依赖。
"""
import os
import sys
import json
import time
import re
import argparse
import subprocess
import http.client

# ★★★ 配置 ★★★
DEFAULT_PORT = 9333


def candidate_paths():
    """候选安装路径（按优先级）。

    用户级安装目录必须从 %LOCALAPPDATA% 取，不能硬编码 C:\\Users\\——
    用户配置文件目录不在 C 盘时（企业环境、多盘位机器）硬编码会直接找不到。
    """
    local = os.environ.get('LOCALAPPDATA') or os.path.expanduser(r'~\AppData\Local')
    return [
        os.path.join(local, 'Programs', 'antigravity', 'Antigravity.exe'),
        r"D:\Antigravity\Antigravity.exe",
        r"C:\Program Files\Antigravity\Antigravity.exe",
        r"C:\Program Files (x86)\Antigravity\Antigravity.exe",
    ]


def normalize_text(text):
    """压缩空白 + 全角引号归一为半角（与旧版一致）"""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return text


def load_dictionary():
    """合并 dicts/*.json 为一个 map（与旧版一致）"""
    total_map = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dicts_dir = os.path.join(script_dir, 'dicts')
    if os.path.exists(dicts_dir):
        for filename in sorted(os.listdir(dicts_dir)):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(dicts_dir, filename), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        for k, v in data.items():
                            norm_k = normalize_text(k)
                            if norm_k:
                                total_map[norm_k] = v
                except Exception as e:
                    print(f"  [跳过] {filename}: {e}")
    return total_map


def build_engine_js(dict_map):
    """构造注入的翻译引擎 IIFE（复用旧版 MutationObserver 思路，禁区按 2.0 调整）

    2.0 是对话型 AI IDE，保护区重点是：代码/编辑器/输入区/对话消息内容。
    去掉旧版 Monaco 专属类（monaco-editor / suggest-widget / artifact-container 等）。
    """
    # 字典只序列化一次：allEntries 由 JS 从 map 现算即可。
    # 之前额外塞了一份排好序的 entries 数组，等于把整份字典编码两遍，
    # 注入 payload 白白翻倍（92 KB → 46 KB）。紧凑分隔符再省一截。
    dict_json = json.dumps(dict_map, ensure_ascii=False, separators=(',', ':'))

    js_source = """\
(() => {
    // 汉化翻译引擎（CDP 注入版，基于容器回溯隔离）
    // 防重复注入：若已注入过（有全局标记），先断开旧 observer 再重建，避免叠加
    if (window.__ag_hanhua_engine__) {
        try { window.__ag_hanhua_engine__.disconnect(); } catch (e) {}
        delete window.__ag_hanhua_engine__;
    }
    const map = new Map(Object.entries(DICT_PLACEHOLDER));
    const lowerMap = new Map();
    for (const [k, v] of map.entries()) lowerMap.set(k.toLowerCase(), v);

    // 按 key 长度降序（长条目优先，避免短条目先匹配吃掉长条目的子串）
    const allEntries = [...map.entries()].sort((a, b) => b[0].length - a[0].length);
    // 预筛：只有长度 >= 30 的完整句子参与子串替换。
    // 门槛必须高：像 "safety barriers" 这种半截短语若参与子串替换，
    // 会把整句里的碎片换成中文、剩下英文，产生"中英混杂"。短条目只做整节点精确匹配。
    const phraseEntries = allEntries.filter(e => e[0].length >= 30);
    // 记录每个文本节点上一次处理过的值：值没变就跳过，避免重复开销
    const lastSeen = new WeakMap();

    // 保护名单：产品名/品牌，禁止子串替换（防止被部分匹配切碎）
    // 只放 Antigravity 自身的产品名词。第三方/用户自装的技能名不要写进来——
    // 那是每台机器不一样的本地配置，写死在这里既泄露环境又对别人无用。
    // 技能名本来就不在字典里，走不到替换分支。
    const PROTECTED = [
        'antigravity', 'jetski', 'gemini', 'google ai', 'best of n',
        'google chrome', 'marketplace', 'mcp', 'citc',
    ];
    function isProtected(s) {
        const low = s.toLowerCase();
        for (const p of PROTECTED) {
            if (low.includes(p)) return true;
        }
        return false;
    }

    // 智能子串替换：只有 phraseEntries（key 长度 >= 30 的完整句子）参与。
    // 词边界：只在 key 前后不是字母/数字/汉字时才替换，避免 "Manager" 被 "Manage" 误替换。
    function smartReplace(text) {
        let result = text;
        for (const [key, val] of phraseEntries) {
            // 快速排除：不含该 key 就跳过昂贵的边界扫描
            if (result.indexOf(key) === -1) continue;
            // key 无需再查保护词：本函数只在文本不含保护词时被调用，而 key 是文本的
            // 子串——key 含保护词则文本必然也含，进不了这里
            // 词边界检查：用字符串判断替代正则，避免转义问题
            // 只在 key 前后不是字母/数字时替换（避免 "Manager" 被 "Manage" 部分匹配）
            let idx = result.indexOf(key);
            while (idx !== -1) {
                const before = idx > 0 ? result[idx - 1] : '';
                const afterIdx = idx + key.length;
                const after = afterIdx < result.length ? result[afterIdx] : '';
                const isWordBoundary =
                    (!/[一-鿿A-Za-z0-9]/.test(before)) &&
                    (!/[一-鿿A-Za-z0-9]/.test(after));
                if (isWordBoundary) {
                    result = result.slice(0, idx) + val + result.slice(afterIdx);
                    idx = result.indexOf(key, idx + val.length);
                } else {
                    idx = result.indexOf(key, idx + key.length);
                }
            }
        }
        return result;
    }

    // 禁区：代码/编辑器/输入/对话内容容器（防止翻译用户数据）
    // 分两类匹配，因为匹配方式选错会造成两种相反的事故：
    // - 多词类名足够特异，用子串匹配没问题
    // - 'terminal'/'preview' 这种通用单词若也用子串匹配，会把 "terminal-settings-row"
    //   这类正常设置项容器整块判成禁区，导致整片界面漏翻。必须按 class token 匹配。
    const BLOCKED_CLASS_SUBSTR = [
        'code-view', 'editor-container', 'monaco-editor', 'suggest-widget',
        'output-view', 'debug-console', 'artifact-container',
        'code-block', 'diff-view', 'input-area', 'chat-input'
    ];
    const BLOCKED_CLASS_TOKEN = ['terminal', 'xterm', 'preview'];
    const BLOCKED_TAGS = ['SCRIPT', 'STYLE', 'CODE', 'PRE', 'INPUT', 'TEXTAREA',
                          'SVG', 'CANVAS', 'SYMBOL', 'PATH', 'MATH', 'KBD'];

    // 承载可见文案的属性（react-tooltip 用 data-tooltip-content，侧边栏分组用 data-title）
    const TRANSLATABLE_ATTRS = ['placeholder', 'title', 'aria-label', 'alt',
                                'data-title', 'data-tooltip-content',
                                'aria-description', 'aria-placeholder'];

    // 必须与 Python 侧 normalize_text() 完全一致：字典 key 在加载时被归一化过，
    // 页面文本不做同样归一化就永远匹配不上。
    // 特别是弯引号——界面里的 Don't / Antigravity's 用的是 ’ 而不是 '。
    function norm(s) {
        if (!s) return '';
        return s.replace(/\\s+/g, ' ')
                .replace(/[’‘]/g, "'")
                .replace(/[“”]/g, '"')
                .trim();
    }

    function isInBlockedZone(node) {
        let curr = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
        let depth = 0;
        // 深度 25：Tailwind/React 的嵌套很深，12 层不够——禁区容器在更上层时会漏判，
        // 用户的代码/会话内容就会被翻译。这段只对"值变过"的文本节点跑，代价可忽略。
        while (curr && depth < 25) {
            if (curr.nodeType === Node.ELEMENT_NODE) {
                const tag = curr.tagName.toUpperCase();
                if (BLOCKED_TAGS.includes(tag)) return true;
                if (curr.getAttribute('contenteditable') === 'true') return true;
                const className = curr.className || '';
                if (typeof className === 'string') {
                    if (BLOCKED_CLASS_SUBSTR.some(cls => className.includes(cls))) return true;
                    const tokens = className.split(/[ ]+/);
                    if (tokens.some(t => BLOCKED_CLASS_TOKEN.includes(t))) return true;
                    // 会话/项目列表项容器（用户内容标题），保护不翻译
                    // 特征：DIV + relative w-full select-none cursor-pointer（区别于普通按钮）
                    if (tag === 'DIV'
                        && className.includes('relative')
                        && className.includes('w-full')
                        && className.includes('select-none')
                        && className.includes('cursor-pointer')) return true;
                }
                const role = curr.getAttribute('role') || '';
                if (role === 'code' || role === 'img') return true;
            }
            curr = curr.parentElement || (curr.parentNode && curr.parentNode.host);
            depth++;
        }
        return false;
    }

    // 动态文案规则：含变量（时间、数字）的句子无法穷举，用正则处理。
    // 刻意不写反斜杠转义（Python 字符串层会把它吞掉），改用 [0-9] [.] [ ] 这类等价写法。
    function cnDuration(s) {
        return s
            .replace(/([0-9]+) days?/g, '$1 天')
            .replace(/([0-9]+) hours?/g, '$1 小时')
            .replace(/([0-9]+) minutes?/g, '$1 分钟')
            .replace(/, /g, ' ');
    }
    const REGEX_RULES = [
        [/^You have used some of your weekly limit, it will fully refresh in (.+)[.]$/,
         (m, a) => '你已使用部分每周限额，将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^You have used some of your five hour limit, it will fully refresh in (.+)[.]$/,
         (m, a) => '你已使用部分五小时限额，将在 ' + cnDuration(a) + ' 后完全刷新。'],
        [/^Send feedback as (.+)$/, (m, a) => '以 ' + a + ' 身份发送反馈'],
        // 模型名会随版本变，写进字典等于每次官方换模型就失效，必须走正则
        [/^Select model, current: (.+)$/, (m, a) => '选择模型，当前：' + a],
        // 排队消息的快捷键 tooltip：前缀是平台相关的按键名（Enter / Alt+Enter / Mac 的 ⌥⌅），
        // 且随"立即发送/排队"设置互换后缀，必须按模板匹配而非写死。
        // 第三行 "On empty prompt..." 不需要规则——字典里已有完整句子，smartReplace 覆盖
        [/^(.+?) Sends immediately$/, (m, a) => a + ' 立即发送'],
        [/^(.+?) Queues after the turn$/, (m, a) => a + ' 排队等待当前轮次结束后发送'],
    ];
    function applyRegexRules(text) {
        for (const [re, fn] of REGEX_RULES) {
            if (re.test(text)) return text.replace(re, fn);
        }
        return text;
    }

    // 属性值的翻译：精确 → 大小写归一 → 正则规则。
    // 必须和文本节点共用同一套规则，否则 "Send feedback as X" 这类动态文案
    // 作为 aria-label 出现时就翻不了（属性路径以前只查字典，不跑正则）。
    // 返回 null 表示没有可用译文。
    function translateAttrValue(raw) {
        const t = norm(raw);
        if (!t) return null;
        if (map.has(t)) return map.get(t);
        const low = t.toLowerCase();
        if (lowerMap.has(low) && t.length < 30) return lowerMap.get(low);
        const byRegex = applyRegexRules(t);
        return byRegex !== t ? byRegex : null;
    }

    function translateNode(node) {
        try {
            if (!node) return;

            if (node.nodeType === Node.ELEMENT_NODE) {
                const tag = node.tagName.toUpperCase();
                if (BLOCKED_TAGS.includes(tag)) return;

                // 属性（placeholder/title/aria-label 等）是 UI 元数据，不是用户输入内容，
                // 所以不查禁区——保证输入框的提示也能翻译
                for (const attr of TRANSLATABLE_ATTRS) {
                    const v = node.getAttribute(attr);
                    if (v) {
                        const t = translateAttrValue(v);
                        if (t !== null && t !== v) node.setAttribute(attr, t);
                    }
                }

                if (node.shadowRoot) translateNode(node.shadowRoot);
                for (const child of node.childNodes) translateNode(child);

            } else if (node.nodeType === Node.TEXT_NODE) {
                let originalVal = node.nodeValue;
                if (!originalVal || originalVal.trim().length < 1) return;
                // 值没变过就跳过（这是性能关键：避免每轮全量重扫都做昂贵匹配）
                if (lastSeen.get(node) === originalVal) return;
                // 纯中文/无英文字母的文本无需处理
                if (!/[A-Za-z]/.test(originalVal)) { lastSeen.set(node, originalVal); return; }
                if (isInBlockedZone(node)) { lastSeen.set(node, originalVal); return; }

                let newVal = originalVal;
                const valNorm = norm(originalVal);
                const valLower = valNorm.toLowerCase();

                if (map.has(valNorm)) {
                    // 整节点精确匹配：优先级最高。
                    // 句子里提到 Antigravity/MCP/Google AI 也照翻——保护名单只管子串替换。
                    newVal = map.get(valNorm);
                } else if (lowerMap.has(valLower) && valLower.length < 30) {
                    // 大小写归一精确匹配（限短词，防误匹配长句）
                    newVal = lowerMap.get(valLower);
                } else if (!isProtected(valNorm)) {
                    // 智能子串替换：仅用于字典没有整句的复合文本。
                    // 含产品名的文本一律不做子串替换，避免把产品名切碎。
                    newVal = smartReplace(valNorm);
                }

                // 动态文案（带时间/数字变量）走正则规则
                if (newVal === originalVal || newVal === valNorm) {
                    newVal = applyRegexRules(valNorm);
                }

                if (newVal !== originalVal && newVal !== valNorm) {
                    node.nodeValue = newVal;
                    lastSeen.set(node, newVal);
                } else {
                    lastSeen.set(node, originalVal);
                }
            }
        } catch (e) {}
    }

    // 观察器：childList（含文本节点）+ characterData + attributes
    // characterData 必须监听：React 重渲染会直接改写文本节点，不听就会被改回英文。
    // 自身写入不会造成循环——lastSeen 记录了我们写入的值，再次看到会直接跳过。
    const observer = new MutationObserver(mutations => {
        for (const m of mutations) {
            if (m.type === 'childList') {
                for (const n of m.addedNodes) translateNode(n);
            } else if (m.type === 'characterData') {
                translateNode(m.target);
            } else if (m.type === 'attributes' && m.target) {
                translateNode(m.target);
            }
        }
    });

    const obsOpts = {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: TRANSLATABLE_ATTRS
    };

    // 兜底：全量重扫可翻译属性（解决应用延迟设置 aria-label/title/placeholder 的漏翻）
    // 属性不查禁区（是 UI 元数据，不是用户输入内容）
    function rescanAttributes() {
        if (!document.body) return;
        const sel = TRANSLATABLE_ATTRS.map(a => '[' + a + ']').join(',');
        for (const el of document.body.querySelectorAll(sel)) {
            if (!el.isConnected) continue;
            for (const attr of TRANSLATABLE_ATTRS) {
                const v = el.getAttribute(attr);
                if (v) {
                    const t = translateAttrValue(v);
                    if (t !== null && t !== v) el.setAttribute(attr, t);
                }
            }
        }
    }

    // 启动：挂 observer + 首次全量翻译。
    // 必须能重试——document-start 注入（Page.addScriptToEvaluateOnNewDocument）时
    // document.body 还不存在，只跑一次就会永远挂不上 observer。
    let started = false;
    // 旧引擎 disconnect 后必须完全停摆：没有 stopped，迟到的 startEngine 重试
    // 会把刚断开的 observer 重新挂回 body，与补注的新引擎叠加成双重翻译
    let stopped = false;
    const startEngine = () => {
        if (started || stopped || !document.body) return;
        started = true;
        observer.observe(document.body, obsOpts);
        translateNode(document.body);
    };

    // Shadow DOM 穿透：劫持 attachShadow，给新 shadow root 也挂观察器。
    // 只劫持一次，且回调里动态读 window.__ag_hanhua_engine__ 而不是闭包捕获 observer——
    // 闭包捕获会让每次重复注入都永久持有一份旧引擎（连同整份字典，约 0.6 MB），
    // 守护期反复补注就会不断叠加。
    if (!Element.prototype.attachShadow.__ag_hooked__) {
        const origAttachShadow = Element.prototype.attachShadow;
        const hooked = function () {
            const sr = origAttachShadow.apply(this, arguments);
            const eng = window.__ag_hanhua_engine__;
            if (eng && eng.observe) { try { eng.observe(sr); } catch (e) {} }
            return sr;
        };
        hooked.__ag_hooked__ = true;
        Element.prototype.attachShadow = hooked;
    }

    startEngine();
    if (!started) {
        document.addEventListener('DOMContentLoaded', startEngine, { once: true });
    }
    setTimeout(startEngine, 300);
    setTimeout(() => { if (stopped || !document.body) return; translateNode(document.body); rescanAttributes(); }, 2000);
    setTimeout(() => { if (stopped || !document.body) return; translateNode(document.body); rescanAttributes(); }, 5000);
    // 周期性全量重扫：兜住 React 重渲染、下拉菜单弹出等 observer 可能漏掉的情况。
    // 同时兜住 startEngine——body 若迟迟不出现，这里会一直重试直到挂上 observer。
    const timerId = setInterval(() => {
        if (stopped) return;
        startEngine();
        if (document.body) translateNode(document.body);
        rescanAttributes();
    }, 3000);

    window.__ag_hanhua_engine__ = {
        observe: (root) => observer.observe(root, obsOpts),
        disconnect: () => {
            stopped = true;
            try { observer.disconnect(); } catch (e) {}
            try { clearInterval(timerId); } catch (e) {}
        }
    };
})();
"""
    return js_source.replace("DICT_PLACEHOLDER", dict_json)


def find_antigravity_exe(install_dir=None):
    """定位 Antigravity.exe，返回路径或 None"""
    if install_dir:
        cand = install_dir
        if cand.endswith('Antigravity.exe'):
            return cand if os.path.exists(cand) else None
        exe = os.path.join(cand, 'Antigravity.exe')
        if os.path.exists(exe):
            return exe
        return None
    for cand in candidate_paths():
        if os.path.exists(cand):
            return cand
    return None


def http_json(host, port, path):
    """GET http://host:port/path → dict/list，失败抛异常"""
    conn = http.client.HTTPConnection(host, port, timeout=3)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return json.loads(body)
    finally:
        conn.close()


def wait_for_debug_port(port, timeout=60):
    """轮询 CDP 端口直到返回 page，返回 page 对象或 None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pages = http_json('127.0.0.1', port, '/json')
            pages = pages if isinstance(pages, list) else []
            # 跳过加载页/空白页（data: URL 或 title 含 Loading），找真正的 Antigravity 主页面
            page = next((
                p for p in pages
                if p.get('type') == 'page'
                and p.get('url', '').startswith('http')
                and not p.get('title', '').startswith('Loading')
            ), None)
            if page:
                return page
        except Exception:
            pass
        time.sleep(0.5)
    return None


def cdp_inject(page, engine_js, watch_seconds=30):
    """通过 CDP WebSocket 注入翻译引擎。用 websockets 库（本机已装）。

    冷启动（qidong.bat）时页面还在加载，直接注入会被随后的加载/导航冲掉
    （现象：注入报成功但界面全英文，页面 title 为空）。因此分三步：

    1. 等待页面真正就绪（readyState=complete 且 body 有实际文案）
    2. Page.addScriptToEvaluateOnNewDocument —— 本连接存活期间，
       页面刷新/导航会在 document-start 自动重新注入
    3. 守护轮询 window.__ag_hanhua_engine__，被冲掉就立刻补注

    注意：第 2 步的注册只在 CDP 连接存活期间有效，连接一断浏览器就清掉它，
    所以第 3 步的守护窗口才是冷启动能否成功的关键。
    """
    import asyncio
    import websockets

    ws_url = page['webSocketDebuggerUrl']

    async def run():
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            counter = {'id': 0}

            async def call(method, params=None):
                counter['id'] += 1
                mid = counter['id']
                await ws.send(json.dumps({"id": mid, "method": method,
                                          "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    # CDP 会推送事件（无 id），只认自己这条请求的回包
                    if msg.get('id') == mid:
                        return msg

            async def evaluate(expr):
                return await call("Runtime.evaluate", {
                    "expression": expr,
                    "returnByValue": True,
                    "awaitPromise": True,
                })

            def value_of(resp):
                try:
                    return resp['result']['result']['value']
                except Exception:
                    return None

            # 1) 等界面真正挂载出来
            READY_EXPR = ("(() => { try { return document.readyState === 'complete'"
                          " && !!document.body"
                          " && document.body.innerText.trim().length > 100;"
                          " } catch (e) { return false; } })()")
            print("[等待] 等待界面加载完成（SPA 挂载）...")
            deadline = time.time() + 60
            ready = False
            while time.time() < deadline:
                if value_of(await evaluate(READY_EXPR)) is True:
                    ready = True
                    break
                await asyncio.sleep(0.5)
            if not ready:
                print("[警告] 界面 60 秒内未就绪，仍尝试注入")

            # 2) 注册 document-start 脚本，覆盖后续刷新/导航
            await call("Page.enable")
            await call("Page.addScriptToEvaluateOnNewDocument", {"source": engine_js})

            # 3) 当前文档立即注入
            resp = await evaluate(engine_js)
            if resp.get('error') or resp.get('result', {}).get('exceptionDetails'):
                return resp

            # 4) 守护：被页面重载冲掉就补注
            if watch_seconds > 0:
                print(f"[守护] 监控 {watch_seconds} 秒，页面重载会自动补注...")
                reinjects = 0
                end = time.time() + watch_seconds
                while time.time() < end:
                    await asyncio.sleep(1.5)
                    alive = value_of(await evaluate("!!window.__ag_hanhua_engine__"))
                    if alive is not True:
                        r = await evaluate(engine_js)
                        if r.get('error') or r.get('result', {}).get('exceptionDetails'):
                            # 不计数；1.5 秒后的探活发现仍未存活会自动再试
                            print("[守护] 补注未成功，等待下轮重试")
                        else:
                            reinjects += 1
                            print(f"[守护] 检测到引擎丢失，已补注（第 {reinjects} 次）")
                if reinjects:
                    print(f"[守护] 结束，共补注 {reinjects} 次")
                else:
                    print("[守护] 结束，引擎全程存活")
            return resp

    return asyncio.run(run())


def launch_antigravity(exe, port):
    """启动 Antigravity（带调试端口），返回进程句柄"""
    print(f"[启动] {exe} --remote-debugging-port={port}")
    try:
        return subprocess.Popen(
            [exe, f"--remote-debugging-port={port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as e:
        print(f"[错误] 启动失败: {e}")
        return None


def is_antigravity_running():
    """检测是否有 Antigravity 进程在跑（不含调试端口信息）。

    Antigravity 允许多实例并存（实测无单实例锁）：端口上没有实例时直接再启一个，
    会静默开出第二个重复窗口——用户原来的窗口保持英文，看起来就是「脚本跑了
    没效果」。启动前先查进程，把这种情况变成一条可操作的即时错误。

    输出用字节匹配：tasklist 在中文 Windows 上输出 GBK，text=True 按 UTF-8
    解码会崩掉 reader 线程，探测静默失败。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Antigravity.exe"],
            capture_output=True, timeout=5,
        ).stdout or b""
        return b"antigravity.exe" in out.lower()
    except Exception:
        return False  # 探测失败不拦路，维持旧行为


def main():
    parser = argparse.ArgumentParser(description="Antigravity 2.0 运行时汉化工具（CDP 注入）")
    parser.add_argument("--install-dir", help="Antigravity 安装目录或 Antigravity.exe 路径（默认自动探测）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"调试端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--no-launch", action="store_true",
                        help="不启动 Antigravity，只连已有调试端口注入（需已带端口启动）")
    parser.add_argument("--watch", type=int, default=30,
                        help="注入后守护多少秒（页面重载会自动补注，0=不守护，默认 30）")
    args = parser.parse_args()

    print("=== Antigravity 2.0 运行时汉化工具（CDP 注入） ===")

    # 1. 加载字典
    dict_map = load_dictionary()
    print(f"[字典] 共 {len(dict_map)} 条翻译规则")
    if not dict_map:
        print("[错误] 未加载到任何字典，请检查 dicts/ 目录")
        sys.exit(1)

    # 2. 定位 / 启动 Antigravity
    exe = find_antigravity_exe(args.install_dir)
    if not args.no_launch:
        if not exe:
            print("[错误] 未找到 Antigravity.exe。请用 --install-dir 指定安装目录。")
            sys.exit(1)
        existing = wait_for_debug_port(args.port, timeout=3)
        if existing:
            print(f"[提示] 端口 {args.port} 已有页面，直接复用（Antigravity 已在运行？）")
        elif is_antigravity_running():
            print(f"[错误] Antigravity 已在运行，但调试端口 {args.port} 上没有实例。")
            print("       此时直接启动只会多开一个重复窗口（原窗口仍是英文）。")
            print("       请先关闭它再运行本脚本（qidong.bat 会自动关闭旧实例），")
            print("       或用 --port 指定它实际开放的调试端口。")
            sys.exit(1)
        elif launch_antigravity(exe, args.port) is None:
            # 不检查返回值的话，启动失败后还要白等 60 秒才报一个"端口没就绪"的误导性错误
            print("[错误] Antigravity 启动失败，请检查安装路径与权限。")
            sys.exit(1)

    # 3. 等待端口
    print(f"[等待] 等待 CDP 端口 {args.port} 就绪...")
    page = wait_for_debug_port(args.port, timeout=60)
    if not page:
        print("[错误] 超时未等到调试端口。请确认 Antigravity 以 --remote-debugging-port 启动。")
        sys.exit(1)
    print(f"[连接] 页面: {page.get('title')}  url: {page.get('url')}")

    # 4. 生成引擎并注入
    print("[生成] 编译翻译引擎...")
    engine_js = build_engine_js(dict_map)
    print("[注入] 通过 CDP 注入翻译引擎...")

    # 冷启动时最早出现的那个 page target 可能在加载途中被丢弃，
    # 连接会直接断掉。重新探测页面再试一次，避免整个脚本崩掉。
    resp = None
    for attempt in range(3):
        try:
            resp = cdp_inject(page, engine_js, watch_seconds=args.watch)
            break
        except Exception as e:
            print(f"[重试] 连接中断（{type(e).__name__}），重新探测页面... ({attempt + 1}/3)")
            time.sleep(2)
            page = wait_for_debug_port(args.port, timeout=30)
            if not page:
                print("[错误] 页面已消失，Antigravity 可能已退出。")
                sys.exit(1)
    if resp is None:
        print("[错误] 多次重试后仍无法注入。")
        sys.exit(1)
    if resp.get('error'):
        print(f"[错误] 注入失败: {resp['error']}")
        sys.exit(1)
    # Runtime.evaluate 的 JS 异常不在顶层 error 里，在 result.exceptionDetails——
    # 不查它，引擎抛错时也会打印「成功」，制造「注入成功但界面全英文」的假象
    exc = resp.get('result', {}).get('exceptionDetails')
    if exc:
        detail = (exc.get('exception') or {}).get('description') or exc.get('text') or str(exc)
        print(f"[错误] 引擎脚本执行异常: {detail}")
        sys.exit(1)
    print("[成功] 翻译引擎已注入！侧边栏等界面元素将显示中文。")
    print("[提示] 若新增字典条目，退出后重新运行本脚本即可。")


if __name__ == "__main__":
    main()
