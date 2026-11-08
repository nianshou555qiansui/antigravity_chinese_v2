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
import cdp_discovery
import cdp_targets
import privacy_policy

# ★★★ 配置 ★★★
DEFAULT_PORT = 9333
# Clash / 系统代理会劫持 127.0.0.1 的 CDP HTTP/WebSocket，必须直连
_LOCAL_NO_PROXY = "127.0.0.1,localhost,::1"


def _bypass_proxy_for_local_cdp():
    """本机 CDP 走直连，避免 HTTP(S)_PROXY 把 9333 打到代理。"""
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    for host in _LOCAL_NO_PROXY.split(","):
        if host not in parts:
            parts.append(host)
    merged = ",".join(parts)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged


def _mask_proxy(url):
    """代理地址可能内嵌 user:pass@，日志常被贴到 issue，凭据打码。"""
    if not url:
        return None
    return re.sub(r"//([^:/@\s]+):([^@\s]+)@", r"//\1:***@", url)


def _log_proxy():
    http_p = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if http_p or https_p:
        print(f"[代理] HTTP_PROXY={_mask_proxy(http_p)} HTTPS_PROXY={_mask_proxy(https_p)}")
        print("[代理] 本机 CDP 已加入 NO_PROXY，不走代理")


def candidate_paths():
    """候选安装路径（按优先级）。

    Windows 用户级安装目录必须从 %LOCALAPPDATA% 取，不能硬编码 C:\\Users\\——
    用户配置文件目录不在 C 盘时（企业环境、多盘位机器）硬编码会直接找不到。
    """
    if sys.platform == 'darwin':
        return [
            '/Applications/Antigravity.app/Contents/MacOS/Antigravity',
            os.path.expanduser('~/Applications/Antigravity.app/Contents/MacOS/Antigravity'),
        ]
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
        // Python re 的空白集合包含 U+0085 / U+001C，而 JS 的 \\s 还会吞掉 BOM。
        return s.replace(/[\\u0009-\\u000D\\u001C-\\u0020\\u0085\\u00A0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000]+/g, ' ')
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

    function translateAttributes(element) {
        for (const attr of TRANSLATABLE_ATTRS) {
            if (typeof element.hasAttribute === 'function' && !element.hasAttribute(attr)) continue;
            if (!privacy.allowsAttribute(element, attr)) {
                const previous = attrSeen.get(element);
                if (previous) previous.delete(attr);
                continue;
            }
            const raw = element.getAttribute(attr);
            if (!raw) continue;
            let seen = attrSeen.get(element);
            if (!seen) { seen = new Map(); attrSeen.set(element, seen); }
            if (seen.get(attr) === raw) continue;
            const translated = translateAttrValue(raw);
            const current = translated !== null && translated !== raw ? translated : raw;
            seen.set(attr, current);
            if (current !== raw) element.setAttribute(attr, current);
            else recordMiss(attr, raw);
        }
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
    // 周期性全量重扫：兜住 React 重渲染、下拉菜单弹出等 observer 可能漏掉的情况。
    // 同时兜住 startEngine——body 若迟迟不出现，这里会一直重试直到挂上 observer。
    timerId = setInterval(() => {
        if (!ensureTrusted()) return;
        startEngine();
        if (document.body) translateNode(document.body);
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
"""
    return js_source.replace("PRIVACY_PLACEHOLDER", privacy_policy.build_policy_js()).replace("DICT_PLACEHOLDER", dict_json)


def find_antigravity_exe(install_dir=None):
    """定位 Antigravity 可执行文件，返回路径或 None"""
    if install_dir:
        cand = install_dir
        if os.path.isfile(cand):
            return cand
        for sub in [
            'Contents/MacOS/Antigravity',
            'Antigravity',
            'Antigravity.exe',
        ]:
            exe = os.path.join(cand, sub)
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


def launch_antigravity(exe, port):
    """启动 Antigravity（带调试端口），返回进程句柄"""
    print(f"[启动] {exe} --remote-debugging-port={port}")
    try:
        kwargs = {
            'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL,
        }
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        return subprocess.Popen(
            [exe, f"--remote-debugging-port={port}"],
            **kwargs
        )
    except Exception as e:
        print(f"[错误] 启动失败: {e}")
        return None


def is_antigravity_running():
    """探测失败必须报错，不能误当作没有进程而重复启动。"""
    return bool(cdp_discovery.antigravity_pids())


def _confirm_restart(assume_yes):
    print("[警告] 重启将关闭当前所有 Antigravity 实例，未保存的编辑可能丢失。")
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input("保存工作后输入 RESTART 确认重启（其他输入取消）: ").strip() == "RESTART"
    except (EOFError, KeyboardInterrupt):
        return False


def prepare_instance(args):
    instance = cdp_discovery.find_running_instance(args.port)
    port = args.port or DEFAULT_PORT
    if args.force_restart and instance.pids:
        exe = find_antigravity_exe(args.install_dir)
        if not exe:
            raise cdp_discovery.DiscoveryError("未找到可执行文件，取消重启；请指定 --install-dir。")
        # 非当前已验证的 CDP 端口先检查，避免关掉应用才发现无法重启。
        if not instance.endpoint or instance.endpoint.port != port:
            cdp_discovery.require_free_port(port)
        if not _confirm_restart(args.yes):
            raise cdp_discovery.DiscoveryError("重启已取消，应用未被关闭。")
        cdp_discovery.stop_confirmed_instance(instance.pids)
    elif instance.endpoint:
        print(f"[复用] 已发现 Antigravity 调试端口 {instance.endpoint.port}，不重启应用。")
        return instance.endpoint.port, instance.page
    elif instance.pids:
        raise cdp_discovery.DiscoveryError(
            "Antigravity 已在运行，但未找到符合条件的调试端口。应用保持不变。\n"
            "       请用 --port 指定端口，或保存工作后运行 --force-restart 并确认。")
    elif args.no_launch:
        raise cdp_discovery.DiscoveryError("未找到可复用的 Antigravity 实例（--no-launch 不会启动应用）。")

    exe = find_antigravity_exe(args.install_dir)
    if not exe:
        raise cdp_discovery.DiscoveryError("未找到 Antigravity，请用 --install-dir 指定安装目录或 .app。")
    cdp_discovery.require_free_port(port)
    # 确认期间或发现之后若又有应用启动，不能再开重复实例。
    if cdp_discovery.antigravity_pids():
        raise cdp_discovery.DiscoveryError("检测到新的 Antigravity 实例，请重试以安全复用。")
    if launch_antigravity(exe, port) is None:
        raise cdp_discovery.DiscoveryError("Antigravity 启动失败，请检查安装路径与权限。")
    return port, None


def main():
    parser = argparse.ArgumentParser(description="Antigravity 2.0 运行时汉化工具（CDP 注入）")
    parser.add_argument("--install-dir", help="Antigravity 安装目录或 Antigravity.exe 路径（默认自动探测）")
    parser.add_argument("--port", type=int, help=f"指定调试端口；省略则自动发现，首次启动用 {DEFAULT_PORT}")
    parser.add_argument("--no-launch", action="store_true",
                        help="不启动 Antigravity，只连已有调试端口注入（需已带端口启动）")
    parser.add_argument("--watch", type=int,
                        help="注入后守护多少秒（页面重载会自动补注，0=不守护，默认 30）")
    diagnostic = parser.add_mutually_exclusive_group()
    diagnostic.add_argument("--status", action="store_true", help="只读查看版本、端口、窗口、引擎及词典状态")
    diagnostic.add_argument("--doctor", action="store_true", help="只读诊断并给出处理建议，不启动或修改应用")
    parser.add_argument("--autostart", choices=["on", "off", "status"],
                        help="自动注入开关：登录自启守护进程，只复用已有调试端口")
    parser.add_argument("--force-restart", action="store_true", help="保存工作后，确认关闭已有实例并重启")
    parser.add_argument("--yes", action="store_true", help="与 --force-restart 同用，明确确认可能丢失未保存内容")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    if args.force_restart and (args.no_launch or args.autostart):
        parser.error("--force-restart 不能与 --no-launch / --autostart 同用")
    if args.yes and not args.force_restart:
        parser.error("--yes 只能与 --force-restart 同用")
    if args.watch is not None and args.watch < 0:
        parser.error("--watch 不能为负数")
    if args.status or args.doctor:
        if args.force_restart or args.yes or args.autostart or args.watch is not None or args.install_dir:
            parser.error("--status / --doctor 不能与重启、自启、--watch 或 --install-dir 同用")
        import diagnostics
        _bypass_proxy_for_local_cdp()
        sys.exit(diagnostics.run(port=args.port, doctor=args.doctor))
    if args.watch is None:
        args.watch = 30

    if args.autostart:
        # 委托给 shouhu.py（启动项指向它，开关逻辑也放在它那边）
        import shouhu
        {"on": shouhu.autostart_on, "off": shouhu.autostart_off,
         "status": shouhu.autostart_status}[args.autostart]()
        return

    print("=== Antigravity 2.0 运行时汉化工具（CDP 注入） ===")
    _bypass_proxy_for_local_cdp()
    _log_proxy()

    # 1. 加载字典
    dict_map = load_dictionary()
    print(f"[字典] 共 {len(dict_map)} 条翻译规则")
    if not dict_map:
        print("[错误] 未加载到任何字典，请检查 dicts/ 目录")
        sys.exit(1)

    # 只翻译经逐窗口身份检查的主界面；观察期内持续发现新窗口。
    try:
        port, _ = prepare_instance(args)
        print(f"[连接] 已验证调试端口 {port}，正在识别所有主界面窗口...")
        if args.watch > 0:
            print(f"[观察] 首个窗口成功后继续观察 {args.watch} 秒，为新窗口和重载页面补注。")
        report = cdp_targets.inject_windows(
            port, build_engine_js(dict_map), watch_seconds=args.watch)
    except cdp_discovery.DiscoveryError as exc:
        print(f"[错误] {exc}")
        sys.exit(1)
    reinjections = sum(max(0, count - 1) for count in report.injections.values())
    print(f"[窗口] 成功 {report.successful}；跳过 {report.skipped}；未完成 {report.failed}；补注 {reinjections}")
    for result in report.results:
        if result.status in ("error", "pending"):
            print(f"[窗口未完成] {result.reason}")
    if not report.ok:
        print("[错误] 没有已确认且注入成功的主窗口，或部分窗口未完成；未强行注入。")
        sys.exit(1)
    print("[成功] 所有当前已确认的主界面窗口均已注入；其他网页未处理。")
    print("[提示] 观察期结束后新开的窗口，请重跑启动器或使用安全守护。")


if __name__ == "__main__":
    main()
