(() => {
    const trustedPage = typeof __agTrustedPage === 'function' ? __agTrustedPage : () => true;
    if (!trustedPage()) return;
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

    const privacy = PRIVACY_PLACEHOLDER;
    // Only bounded source counts survive a miss; never retain raw user context or hashes.
    const misses = {schema: 1, sources: Object.fromEntries(privacy.sources.map(source => [source, 0]))};
    const attrSeen = new WeakMap();
    function recordMiss(source, raw) {
        if (privacy.usableText(raw) && Object.prototype.hasOwnProperty.call(misses.sources, source)) {
            misses.sources[source] = Math.min(1000000, misses.sources[source] + 1);
        }
    }

    // 保护名单：产品名/品牌，禁止子串替换（防止被部分匹配切碎）
    // 只放 Antigravity 自身的产品名词。第三方/用户自装的技能名不要写进来——
    // 那是每台机器不一样的本地配置，写死在这里既泄露环境又对别人无用。
    // 技能名本来就不在字典里，走不到替换分支。
    const PROTECTED = [
        'antigravity', 'jetski', 'gemini', 'google ai', 'best of n',
        'google chrome', 'marketplace', 'mcp', 'citc', 'chief of staff',
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

    const TRANSLATABLE_ATTRS = privacy.attributes;

    // 必须与 Python 侧 normalize_text() 完全一致：字典 key 在加载时被归一化过，
    // 页面文本不做同样归一化就永远匹配不上。
    // 特别是弯引号——界面里的 Don't / Antigravity's 用的是 ’ 而不是 '。
    function norm(s) {
        if (!s) return '';
        // Python re 的空白集合包含 U+0085 / U+001C，而 JS 的 \s 还会吞掉 BOM。
        return s.replace(/[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+/g, ' ')
                .replace(/[’‘]/g, "'")
                .replace(/[“”]/g, '"')
                .replace(/^[ ]+|[ ]+$/g, '');
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
        // 官方把 five hour 改成了 5-hour（2026-08 发现失配），两种写法都得容纳
        [/^You have used some of your (5-hour|five hour) limit, it will fully refresh in (.+)[.]$/,
         (m, a, b) => '你已使用部分五小时限额，将在 ' + cnDuration(b) + ' 后完全刷新。'],
        // 插件页描述是 React 多子节点："...help the Agent" / "in Antigravity " / "work with..."
        // 是三个独立文本节点，整句正则匹配不到，只能拆开：两个片段进字典，这条管中间的产品名
        [/^in (Antigravity)$/, (m, a) => '（在 ' + a + ' 中）'],
        [/^Send feedback as (.+)$/, (m, a) => '以 ' + a + ' 身份发送反馈'],
        // 模型名会随版本变，写进字典等于每次官方换模型就失效，必须走正则
        [/^Select model, current: (.+)$/, (m, a) => '选择模型，当前：' + a],
        // 排队消息的快捷键 tooltip：前缀是平台相关的按键名（Enter / Alt+Enter / Mac 的 ⌥⌅），
        // 且随"立即发送/排队"设置互换后缀，必须按模板匹配而非写死。
        // 第三行 "On empty prompt..." 不需要规则——字典里已有完整句子，smartReplace 覆盖
        [/^(.+?) Sends immediately$/, (m, a) => a + ' 立即发送'],
        [/^(.+?) Queues after the turn$/, (m, a) => a + ' 排队等待当前轮次结束后发送'],
        // 归档/进行中对话的计数标签（"1 archived conversation" / "2 active conversations"）：
        // 数量可变、单复数由官方模板决定，写死进字典必然漏。复合句（"N active and M archived"）
        // 单独一条兜住——单数规则带 $ 锚定，不会误吃复合句。
        [/^([0-9]+) active conversations?$/, (m, a) => a + ' 个进行中对话'],
        [/^([0-9]+) archived conversations?$/, (m, a) => a + ' 个已归档对话'],
        [/^([0-9]+) active conversations? and ([0-9]+) archived conversations?$/,
         (m, a, b) => a + ' 个进行中对话和 ' + b + ' 个已归档对话'],
        // 项目选择器 / 权限预设选择器的 aria-label，后半段是变量
        [/^Select project, current: (.+)$/, (m, a) => '选择项目，当前：' + a],
        [/^Agent permissions preset selector, current: (.+)$/,
         (m, a) => '智能体权限预设选择器，当前：' + a],
        // 新模型上线公告：文案由服务端下发（bundle 里搜不到），模型名随官方更新变，只能按模板匹配
        [/^(.+) is now available to paid Pro plan subscribers[.]$/,
         (m, a) => a + ' 现已向付费 Pro 套餐订阅者开放。'],
        // token 明细折叠按钮：bundle 里是 `Show ${n} breakdown${n===1?'':'s'}` 模板拼接，
        // 写死进字典会随数量变化失效
        [/^Show ([0-9]+) breakdowns?$/, (m, a) => '显示 ' + a + ' 项明细'],
        // 模型分组的小感叹号：分组说明由服务端下发，组内模型列表随官方增删变化
        [/^Models within this group: (.+)$/, (m, a) => '此组中的模型：' + a],
        // 安全预设「继承全局设置」的说明：bundle 里是 `Inherits your Global Permissions
        // when working ${E}.`，E 可能是 "in these conversations" / "in this workspace" 等
        [/^Inherits your Global Permissions when working in these conversations[.]$/,
         () => '在这些对话中工作时继承你的全局权限。'],
        [/^Inherits your Global Permissions when working in this workspace[.]$/,
         () => '在此工作区中工作时继承你的全局权限。'],
        [/^Inherits your Global Permissions when working in this project[.]$/,
         () => '在此项目中工作时继承你的全局权限。'],
        [/^Inherits your Global Permissions when working (.+)[.]$/,
         (m, a) => '工作时继承你的全局权限（范围：' + a + '）。'],
        // 2.29.0 的计数/录音/搜索提示来自模板；保留变量，不记录本机值。
        [/^([0-9]+) Side Questions?$/, (m, a) => a + ' 个侧边问题'],
        [/^View ([0-9]+) side questions?$/, (m, a) => '查看 ' + a + ' 个侧边问题'],
        [/^Question ([0-9]+) of ([0-9]+)$/, (m, a, b) => '第 ' + a + ' 个问题，共 ' + b + ' 个'],
        [/^Show ([0-9]+) earlier steps?$/, (m, a) => '显示之前的 ' + a + ' 个步骤'],
        [/^Show ([0-9]+) more subagents?$/, (m, a) => '再显示 ' + a + ' 个子智能体'],
        [/^No apps found for "(.*)"[.]$/, (m, a) => '未找到与“' + a + '”匹配的应用。'],
        [/^([0-9]+) calls? · ([0-9]+[.][0-9]+) MB$/,
         (m, a, b) => a + ' 次通话 · ' + b + ' MB'],
        [/^Deleted ([0-9]+) recordings? [(]([0-9]+[.][0-9]+) MB[)]$/,
         (m, a, b) => '已删除 ' + a + ' 条录音（' + b + ' MB）'],
        [/^This deletes ([0-9]+) call recordings? [(]([0-9]+[.][0-9]+) MB[)] from your machine[.] You can't undo this[.]$/,
         (m, a, b) => '这将从你的设备删除 ' + a + ' 条通话录音（' + b + ' MB），此操作无法撤销。'],
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
        const rawRegex = applyRegexRules(raw);
        if (rawRegex !== raw) return rawRegex;
        const byRegex = applyRegexRules(t);
        return byRegex !== t ? byRegex : null;
    }

    function translateAttribute(element, attr) {
        if (typeof element.hasAttribute === 'function' && !element.hasAttribute(attr)) return;
        if (!privacy.allowsAttribute(element, attr)) {
            const previous = attrSeen.get(element);
            if (previous) previous.delete(attr);
            return;
        }
        const raw = element.getAttribute(attr);
        if (!raw) return;
        let seen = attrSeen.get(element);
        if (!seen) { seen = new Map(); attrSeen.set(element, seen); }
        if (seen.get(attr) === raw) return;
        const translated = translateAttrValue(raw);
        const current = translated !== null && translated !== raw ? translated : raw;
        seen.set(attr, current);
        if (current !== raw) element.setAttribute(attr, current);
        else recordMiss(attr, raw);
    }

    function translateAttributes(element) {
        for (const attr of TRANSLATABLE_ATTRS) translateAttribute(element, attr);
    }

    function translateNode(node) {
        try {
            if (!node) return;

            if (node.nodeType === Node.ELEMENT_NODE || node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                if (node.nodeType === Node.ELEMENT_NODE) {
                    translateAttributes(node);
                    if (!privacy.allowsText(node)) return;

                    if (node.shadowRoot) {
                        observeRoot(node.shadowRoot);
                        translateNode(node.shadowRoot);
                    }
                }

                for (const child of node.childNodes) translateNode(child);

            } else if (node.nodeType === Node.TEXT_NODE) {
                // Privacy decisions precede reads, matching, and WeakMap caching.
                if (!privacy.allowsText(node)) { lastSeen.delete(node); return; }
                let originalVal = node.nodeValue;
                if (!originalVal || originalVal.trim().length < 1) return;
                // 值没变过就跳过（这是性能关键：避免每轮全量重扫都做昂贵匹配）
                if (lastSeen.get(node) === originalVal) return;
                // 纯中文/无英文字母的文本无需处理
                if (!/[A-Za-z]/.test(originalVal)) { lastSeen.set(node, originalVal); return; }

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
                } else {
                    // 完整动态模板先匹配，避免子串替换改写搜索词等变量。
                    newVal = applyRegexRules(originalVal);
                    if (newVal === originalVal) newVal = applyRegexRules(valNorm);
                    if (newVal === valNorm && !isProtected(valNorm) && !/[一-鿿]/.test(valNorm)) {
                        // 已有中文的输出不再子串替换，重注入也保留其中的英文变量。
                        newVal = smartReplace(valNorm);
                    }
                }

                if (newVal !== originalVal && newVal !== valNorm) {
                    node.nodeValue = newVal;
                    lastSeen.set(node, newVal);
                } else {
                    lastSeen.set(node, originalVal);
                    recordMiss('text', valNorm);
                }
            }
        } catch (e) {}
    }

    // 观察器：childList（含文本节点）+ characterData + attributes
    // characterData 必须监听：React 重渲染会直接改写文本节点，不听就会被改回英文。
    // 自身写入不会造成循环——lastSeen 记录了我们写入的值，再次看到会直接跳过。
    const observer = new MutationObserver(mutations => {
        if (!ensureTrusted()) return;
        const subtreeRoots = new Set();
        const changedAttributes = new Map();
        const boundaryAttributes = new Set(privacy.boundaryAttributes);
        const translatableAttributes = new Set(TRANSLATABLE_ATTRS);
        for (const m of mutations) {
            if (m.type === 'childList') {
                for (const n of m.addedNodes) subtreeRoots.add(n);
            } else if (m.type === 'characterData') {
                subtreeRoots.add(m.target);
            } else if (m.type === 'attributes' && m.target) {
                if (boundaryAttributes.has(m.attributeName)) {
                    subtreeRoots.add(m.target);
                    changedAttributes.delete(m.target);
                } else if (translatableAttributes.has(m.attributeName) && !subtreeRoots.has(m.target)) {
                    let names = changedAttributes.get(m.target);
                    if (!names) { names = new Set(); changedAttributes.set(m.target, names); }
                    names.add(m.attributeName);
                }
            }
        }
        for (const root of subtreeRoots) translateNode(root);
        for (const [element, names] of changedAttributes) {
            for (const attr of names) translateAttribute(element, attr);
        }
    });

    const obsOpts = {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: [...TRANSLATABLE_ATTRS, ...privacy.boundaryAttributes]
    };
    const observedRoots = new WeakSet();
    function observeRoot(root) {
        if (observedRoots.has(root) || !privacy.allowsText(root)) return;
        observer.observe(root, obsOpts);
        observedRoots.add(root);
    }

    // 兜底：全量重扫可翻译属性（解决应用延迟设置 aria-label/title/placeholder 的漏翻）
    // Periodic and observer paths share the same privacy gate.
    function rescanAttributes() {
        if (!document.body) return;
        const sel = TRANSLATABLE_ATTRS.map(a => '[' + a + ']').join(',');
        for (const el of document.body.querySelectorAll(sel)) {
            if (!el.isConnected) continue;
            translateAttributes(el);
        }
    }

    // 启动：挂 observer + 首次全量翻译；保留 body 延迟出现的防御性重试。
    let started = false;
    // 旧引擎或已离开主界面的引擎必须完全停止，迟到的重试不能重新挂载。
    let stopped = false;
    let timerId = null;
    const state = { started: false, stopped: false, entries: map.size, privacy: 1 };
    function ensureTrusted() {
        if (stopped) return false;
        if (trustedPage()) return true;
        stopped = true;
        state.stopped = true;
        observer.disconnect();
        clearInterval(timerId);
        return false;
    }
    const startEngine = () => {
        if (!ensureTrusted() || started || !document.body) return;
        started = true;
        state.started = true;
        observeRoot(document.body);
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
    setTimeout(() => { if (!ensureTrusted() || !document.body) return; translateNode(document.body); rescanAttributes(); }, 2000);
    setTimeout(() => { if (!ensureTrusted() || !document.body) return; translateNode(document.body); rescanAttributes(); }, 5000);
    // DOM text changes are observed directly; keep only the bounded attribute fallback here.
    timerId = setInterval(() => {
        if (!ensureTrusted()) return;
        startEngine();
        rescanAttributes();
    }, 3000);

    window.__ag_hanhua_engine__ = {
        scope: 'antigravity-main-v2',
        state: state,
        active: () => ensureTrusted() && started,
        observe: (root) => { if (ensureTrusted()) observeRoot(root); },
        misses: misses,
        disconnect: () => {
            stopped = true;
            state.stopped = true;
            try { observer.disconnect(); } catch (e) {}
            try { clearInterval(timerId); } catch (e) {}
        }
    };
})();
