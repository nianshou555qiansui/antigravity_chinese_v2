"use strict";

const assert = require("node:assert/strict");
const vm = require("node:vm");

if (require.main === module) {
    const chunks = [];
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => chunks.push(chunk));
    process.stdin.on("end", () => {
        try {
            const payload = JSON.parse(chunks.join(""));
            run(payload);
            runGuardTests(payload);
            process.stdout.write(`ok: ${Object.keys(payload.dictionary).length} entries\n`);
        } catch (error) {
            console.error(error.stack || error);
            process.exitCode = 1;
        }
    });
}

function makeHarness(hasBody = true) {
    const observers = [];
    const timers = [];
    const intervals = new Map();
    const events = new Map();

    class NodeStub {
        constructor(type) {
            this.nodeType = type;
            this.childNodes = [];
            this.parentNode = null;
            this.assignedSlot = null;
        }

        get parentElement() {
            return this.parentNode?.nodeType === 1 ? this.parentNode : null;
        }

        get isConnected() {
            let parent = this;
            const seen = new Set();
            while (parent) {
                if (parent === document) return true;
                if (seen.has(parent)) return false;
                seen.add(parent);
                parent = parent.parentNode || parent.host;
            }
            return false;
        }

        appendChild(child) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = this;
            this.childNodes.push(child);
            notify(this, { type: "childList", target: this, addedNodes: [child] });
            return child;
        }

        removeChild(child) {
            const index = this.childNodes.indexOf(child);
            assert.notEqual(index, -1, "removeChild requires an existing child");
            this.childNodes.splice(index, 1);
            child.parentNode = null;
            notify(this, { type: "childList", target: this, addedNodes: [] });
            return child;
        }

        getRootNode() {
            let root = this;
            while (root.parentNode) root = root.parentNode;
            return root;
        }
    }

    class TextStub extends NodeStub {
        constructor(text) {
            super(3);
            this.value = text;
        }

        get nodeValue() { return this.value; }
        set nodeValue(value) {
            this.value = value;
            notify(this, { type: "characterData", target: this });
        }
    }

    class FragmentStub extends NodeStub {
        constructor(host = null) {
            super(11);
            this.host = host;
        }
    }

    class ElementStub extends NodeStub {
        constructor(tag = "div", attrs = {}) {
            super(1);
            this.tagName = tag.toUpperCase();
            this.attrs = new Map(Object.entries(attrs));
            this.className = attrs.class || "";
            this.shadowRoot = null;
        }

        getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
        hasAttribute(name) { return this.attrs.has(name); }
        setAttribute(name, value) {
            this.attrs.set(name, value);
            if (name === "class") this.className = value;
            notify(this, { type: "attributes", target: this, attributeName: name });
        }

        removeAttribute(name) {
            this.attrs.delete(name);
            if (name === "class") this.className = "";
            notify(this, { type: "attributes", target: this, attributeName: name });
        }

        get classList() {
            return { contains: name => this.className.split(/[\t\n\f\r ]+/).includes(name) };
        }

        attachShadow() {
            const root = new FragmentStub(this);
            this.shadowRoot = root;
            return root;
        }

        querySelectorAll(selector) {
            const names = selector.split(",").map(item => item.slice(1, -1));
            const matches = [];
            const visit = node => {
                for (const child of node.childNodes) {
                    if (child.nodeType === 1) {
                        if (names.some(name => child.hasAttribute(name))) matches.push(child);
                        visit(child);
                    }
                }
            };
            visit(this);
            return matches;
        }
    }

    class ObserverStub {
        constructor(callback) {
            this.callback = callback;
            this.roots = new Set();
            this.options = new Map();
            observers.push(this);
        }

        observe(root, options) { this.roots.add(root); this.options.set(root, options); }
        disconnect() { this.roots.clear(); this.options.clear(); }
    }

    function notify(target, record) {
        for (const observer of observers) {
            let current = target;
            while (current) {
                if (observer.roots.has(current)) {
                    const options = observer.options.get(current);
                    if (record.type !== "attributes" || !options.attributeFilter
                        || options.attributeFilter.includes(record.attributeName)) observer.callback([record]);
                    break;
                }
                current = current.parentNode;
            }
        }
    }

    const document = {
        nodeType: 9,
        parentNode: null,
        childNodes: [],
        addEventListener(type, callback) {
            if (!events.has(type)) events.set(type, []);
            events.get(type).push(callback);
        },
        dispatch(type) {
            for (const callback of events.get(type) || []) callback();
            events.delete(type);
        },
    };
    document.documentElement = new ElementStub("html");
    document.documentElement.parentNode = document;
    document.childNodes.push(document.documentElement);
    let body = null;
    Object.defineProperty(document, "body", {
        get() { return body; },
        set(value) {
            if (body) document.documentElement.removeChild(body);
            body = value;
            if (value) document.documentElement.appendChild(value);
        },
    });
    if (hasBody) document.body = new ElementStub("body");
    const window = {};
    const context = vm.createContext({
        window,
        document,
        Node: { ELEMENT_NODE: 1, TEXT_NODE: 3, DOCUMENT_NODE: 9, DOCUMENT_FRAGMENT_NODE: 11 },
        Element: ElementStub,
        MutationObserver: ObserverStub,
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        setInterval: (fn, ms) => { const id = timers.length + intervals.size + 1; intervals.set(id, { fn, ms }); return id; },
        clearInterval: id => intervals.delete(id),
    });
    return {
        window, document, context, observers, timers, intervals, ElementStub, TextStub, FragmentStub,
        inject(source) { return vm.runInContext(source, context, { timeout: 20000 }); },
        tick(ms) {
            for (const timer of timers.splice(0).filter(item => item.ms <= ms)) timer.fn();
            for (const interval of [...intervals.values()]) if (interval.ms <= ms) interval.fn();
        },
    };
}

function run({ engine, dictionary, normSamples }) {
    const h = makeHarness();
    const entries = Object.entries(dictionary);
    const exact = [];
    for (const [key, expected] of entries) {
        const el = new h.ElementStub("button", { "aria-label": key });
        const text = el.appendChild(new h.TextStub(key));
        h.document.body.appendChild(el);
        exact.push({ el, text, expected, key });
    }
    for (const [raw, expected, normalized] of normSamples) {
        const el = new h.ElementStub("div", { "data-title": raw });
        const text = el.appendChild(new h.TextStub(raw));
        h.document.body.appendChild(el);
        exact.push({ el, text, expected, key: raw, attr: "data-title", attrExpected: raw,
            textExpected: expected === normalized ? raw : expected });
    }
    const dynamicCases = [];
    for (const count of [1, 2]) {
        const plural = count === 1 ? "" : "s";
        dynamicCases.push(
            [`${count} Side Question${plural}`, `${count} \u4e2a\u4fa7\u8fb9\u95ee\u9898`],
            [`View ${count} side question${plural}`, `\u67e5\u770b ${count} \u4e2a\u4fa7\u8fb9\u95ee\u9898`],
            [`Question ${count} of 17`, `\u7b2c ${count} \u4e2a\u95ee\u9898\uff0c\u5171 17 \u4e2a`],
            [`Show ${count} earlier step${plural}`, `\u663e\u793a\u4e4b\u524d\u7684 ${count} \u4e2a\u6b65\u9aa4`],
            [`Show ${count} more subagent${plural}`, `\u518d\u663e\u793a ${count} \u4e2a\u5b50\u667a\u80fd\u4f53`],
            [`${count} call${plural} \u00b7 1234.50 MB`, `${count} \u6b21\u901a\u8bdd \u00b7 1234.50 MB`],
            [`Deleted ${count} recording${plural} (1234.50 MB)`, `\u5df2\u5220\u9664 ${count} \u6761\u5f55\u97f3\uff081234.50 MB\uff09`],
            [`This deletes ${count} call recording${plural} (1234.50 MB) from your machine. You can't undo this.`,
                `\u8fd9\u5c06\u4ece\u4f60\u7684\u8bbe\u5907\u5220\u9664 ${count} \u6761\u901a\u8bdd\u5f55\u97f3\uff081234.50 MB\uff09\uff0c\u6b64\u64cd\u4f5c\u65e0\u6cd5\u64a4\u9500\u3002`],
        );
    }
    for (const query of ["", "File / Gemini+v2", "My-App_42", "Create a new project from one or more folders.", "My  App\u2019s query", "\u4e2d\u6587 Create a new project from one or more folders."])
        dynamicCases.push([`No apps found for "${query}".`, `\u672a\u627e\u5230\u4e0e\u201c${query}\u201d\u5339\u914d\u7684\u5e94\u7528\u3002`]);
    for (const [raw, expected] of dynamicCases) {
        const el = new h.ElementStub("button", { title: raw });
        const text = el.appendChild(new h.TextStub(raw));
        h.document.body.appendChild(el);
        exact.push({ el, text, expected, key: raw, attr: "title" });
    }

    const sample = key => {
        assert.ok(key in dictionary, `missing fixture: ${key}`);
        return dictionary[key];
    };
    const addText = (container, raw, tag = "span", attrs = {}, owner = h) => {
        const el = container.appendChild(new owner.ElementStub(tag, attrs));
        const text = el.appendChild(new owner.TextStub(raw));
        return { el, text };
    };
    const protectedFixtures = [];
    for (const [tag, attrs] of [
        ["input", { placeholder: "File" }],
        ["pre", {}],
        ["code", {}],
        ["div", { class: "terminal" }],
        ["div", { class: "relative w-full select-none cursor-pointer" }],
        ...["true", "TRUE", "", "plaintext-only"].map(value => ["div", { contenteditable: value }]),
    ]) {
        const fixture = addText(h.document.body, "File", tag, attrs);
        protectedFixtures.push(fixture);
    }
    const inheritedEditable = h.document.body.appendChild(new h.ElementStub("div", { contenteditable: "true" }));
    protectedFixtures.push(addText(inheritedEditable, "File"));
    const falseEditable = addText(h.document.body, "File", "div", { contenteditable: "false" });
    const falseUpper = addText(h.document.body, "File", "div", { contenteditable: "FALSE" });
    const invalidEditable = addText(h.document.body, "File", "div", { contenteditable: "inherit" });
    const falseOverride = addText(inheritedEditable, "File", "div", { contenteditable: "false" });
    const permissiveClass = addText(h.document.body, "File", "div", { class: "terminal-settings-row select-none cursor-pointer" });
    const editor = h.document.body.appendChild(new h.ElementStub("div", { class: "editor-container" }));
    let deep = editor;
    for (let i = 0; i < 20; i++) deep = deep.appendChild(new h.ElementStub("div"));
    const deepText = addText(deep, "File").text;
    const productText = addText(h.document.body, "Gemini File; Antigravity File; Chief of Staff File").text;
    const longPhrase = entries.find(([key]) => key.length >= 30 && !/antigravity|jetski|gemini|google ai|best of n|google chrome|marketplace|mcp|citc|chief of staff/i.test(key))[0];
    const protectedPhrase = `Gemini: ${longPhrase}`;
    const protectedPhraseText = addText(h.document.body, protectedPhrase).text;
    const userInput = addText(h.document.body, "File", "textarea").text;

    const oldHost = h.document.body.appendChild(new h.ElementStub("div"));
    const oldRoot = oldHost.attachShadow({ mode: "open" });
    const oldShadow = addText(oldRoot, "File", "button", { title: "Save" });
    const shadowInput = oldRoot.appendChild(new h.ElementStub("input", { placeholder: "File" }));
    const blockedHost = h.document.body.appendChild(new h.ElementStub("div", { class: "editor-container" }));
    const blockedRoot = blockedHost.attachShadow({ mode: "open" });
    const blockedDirect = blockedRoot.appendChild(new h.TextStub("File"));
    let shadowDeep = blockedRoot;
    for (let i = 0; i < 24; i++) shadowDeep = shadowDeep.appendChild(new h.ElementStub("div"));
    const deepShadowText = shadowDeep.appendChild(new h.TextStub("File"));
    const blockedAttr = blockedRoot.appendChild(new h.ElementStub("div", { "aria-label": "File" }));
    const fragment = new h.FragmentStub();
    const fragmentChild = addText(fragment, "File", "button", { title: "Save" });
    h.document.body.appendChild(fragment);

    h.inject(engine);
    for (const { el, text, expected, key, attr = "aria-label", textExpected = expected, attrExpected = expected } of exact) {
        assert.equal(text.nodeValue, textExpected, `exact text ${JSON.stringify(key)}`);
        assert.equal(el.getAttribute(attr), attrExpected, `exact attr ${JSON.stringify(key)}`);
    }
    for (const { text } of protectedFixtures) assert.equal(text.nodeValue, "File", "protected text");
    assert.equal(deepText.nodeValue, "File", "deep editor");
    assert.equal(userInput.nodeValue, "File", "user input");
    assert.equal(productText.nodeValue, "Gemini File; Antigravity File; Chief of Staff File", "product name in composite text");
    assert.equal(protectedPhraseText.nodeValue, protectedPhrase, "product name disables phrase replacement");
    for (const [name, fixture] of Object.entries({ falseEditable, falseUpper, invalidEditable, permissiveClass })) {
        assert.equal(fixture.text.nodeValue, sample("File"), `noneditable UI ${name}`);
    }
    assert.equal(falseOverride.text.nodeValue, "File", "conservative protection inside editable ancestor");
    assert.equal(protectedFixtures[0].el.getAttribute("placeholder"), sample("File"), "input metadata");
    assert.equal(oldShadow.text.nodeValue, sample("File"), "existing shadow text");
    assert.equal(oldShadow.el.getAttribute("title"), sample("Save"), "existing shadow attr");
    assert.equal(shadowInput.getAttribute("placeholder"), sample("File"), "shadow input metadata");
    assert.equal(blockedDirect.nodeValue, "File", "blocked shadow host direct text");
    assert.equal(deepShadowText.nodeValue, "File", "shadow fragment does not consume blocked-zone depth");
    assert.equal(blockedAttr.getAttribute("aria-label"), "File", "blocked shadow metadata remains private");
    assert.equal(fragmentChild.text.nodeValue, sample("File"), "document fragment text");
    assert.equal(fragmentChild.el.getAttribute("title"), sample("Save"), "document fragment attr");
    assert.ok(h.observers[0].roots.has(oldRoot), "existing shadow root observed");

    oldShadow.text.nodeValue = "Save";
    assert.equal(oldShadow.text.nodeValue, sample("Save"), "shadow characterData observer");
    oldShadow.el.setAttribute("title", "File");
    assert.equal(oldShadow.el.getAttribute("title"), sample("File"), "shadow attr observer");
    const newHost = h.document.body.appendChild(new h.ElementStub("div"));
    const newRoot = newHost.attachShadow({ mode: "open" });
    assert.ok(h.observers[0].roots.has(newRoot), "new shadow root observed");
    const newShadow = addText(newRoot, "File", "button", { title: "Save" });
    assert.equal(newShadow.text.nodeValue, sample("File"), "new shadow root text");
    assert.equal(newShadow.el.getAttribute("title"), sample("Save"), "new shadow root attr");
    const hook = h.ElementStub.prototype.attachShadow;
    const oldEngine = h.window.__ag_hanhua_engine__;
    const oldObserver = h.observers[0];
    h.inject(engine);
    assert.equal(h.ElementStub.prototype.attachShadow, hook, "only one shadow hook");
    for (const { text, expected, key, attr } of exact) {
        if (attr === "title") assert.equal(text.nodeValue, expected, `dynamic variable after reinjection ${key}`);
    }
    assert.equal(oldObserver.roots.size, 0, "old observer disconnected");
    assert.equal(oldEngine.state.stopped, true, "old engine snapshot stopped on replacement");
    assert.notEqual(h.window.__ag_hanhua_engine__, oldEngine, "engine replaced");
    h.tick(5000);
    assert.equal(oldObserver.roots.size, 0, "old timer must not reconnect");
    assert.equal(h.intervals.size, 1, "one live engine interval");
    const afterReinject = addText(newRoot, "Save").text;
    assert.equal(afterReinject.nodeValue, sample("Save"), "shadow observer after reinjection");
    const newestRoot = h.document.body.appendChild(new h.ElementStub("div")).attachShadow({ mode: "open" });
    const newestText = addText(newestRoot, "File").text;
    assert.equal(newestText.nodeValue, sample("File"), "shadow hook uses newest engine");
    assert.equal(oldObserver.roots.size, 0, "shadow hook must not revive old observer");

    const delayed = makeHarness(false);
    delayed.inject(engine);
    assert.equal(delayed.observers[0].roots.size, 0, "no body at document-start");
    delayed.inject(engine);
    delayed.document.body = new delayed.ElementStub("body");
    const lateText = addText(delayed.document.body, "File", "span", {}, delayed).text;
    delayed.document.dispatch("DOMContentLoaded");
    assert.equal(lateText.nodeValue, sample("File"), "late body translated");
    assert.equal(delayed.observers[0].roots.size, 0, "stopped document-start engine stays disconnected");
    assert.ok(delayed.observers[1].roots.has(delayed.document.body), "late body observed");
    const missedEvent = makeHarness(false);
    missedEvent.inject(engine);
    missedEvent.document.body = new missedEvent.ElementStub("body");
    const timerText = addText(missedEvent.document.body, "File", "span", {}, missedEvent).text;
    missedEvent.tick(3000);
    assert.equal(timerText.nodeValue, sample("File"), "late body timer fallback");
}

module.exports = { makeHarness };

function hubHarness() {
    const h = makeHarness();
    h.window.top = h.window;
    h.window.electronNative = { minimize() {}, maximize() {} };
    h.window.nativeStorage = { getItems() {} };
    h.window.__APP_CONFIG__ = { productName: "antigravity", appVersion: "2.29.0" };
    h.document.readyState = "complete";
    h.root = { childElementCount: 1 };
    h.document.getElementById = name => name === "root" ? h.root : null;
    h.document.scripts = [{ src: "https://127.0.0.1:7555/main.js?version=1" }];
    h.context.URL = URL;
    h.context.location = { origin: "https://127.0.0.1:7555", pathname: "/", href: "https://127.0.0.1:7555/" };
    const container = h.document.body.appendChild(new h.ElementStub());
    h.text = container.appendChild(new h.TextStub("File"));
    return h;
}

function runGuardTests({ guarded, forced, probe, diagnosis, dictionary }) {
    const standalonePaths = [
        "/artifact", "/artifact/abc", "/artifact-preview", "/artifactXXX",
        "/terminal-standalone/", "/terminal-standalone-test",
        "/settings-standalone", "/settings-standalone2", "/static/artifacts/a",
    ];
    for (const pathname of ["/", "/c/example", "/history", "/sidecars", "/customizations/plugins", "/view/example"]) {
        const h = hubHarness();
        h.context.location.pathname = pathname;
        assert.equal(h.inject(probe).status, "missing", "small probe does not install an engine");
        assert.equal(h.observers.length, 0);
        assert.equal(h.inject(guarded).status, "injected", `main route ${pathname}`);
        assert.equal(h.window.__ag_hanhua_engine__.state.started, true);
        assert.equal(h.window.__ag_hanhua_engine__.state.stopped, false);
        assert.equal(h.window.__ag_hanhua_engine__.state.entries, Object.keys(dictionary).length);
        assert.equal(h.inject(diagnosis).engine, "running", "diagnosis reads real engine snapshot");
        assert.equal(h.inject(probe).status, "alive", "healthy probe needs no dictionary payload");
        assert.equal(h.text.nodeValue, dictionary.File);
        assert.equal(h.inject(guarded).status, "alive", "already running without duplicate injection");
        assert.equal(h.observers.length, 1);
        assert.equal(h.inject(forced).status, "injected", "explicit dictionary refresh");
        assert.equal(h.observers[0].roots.size, 0);
        assert.equal(h.intervals.size, 1);
    }
    const rejected = [
        h => { h.window.top = {}; },
        h => { h.context.location.origin = "https://example.com"; },
        h => { h.window.electronNative = undefined; },
        h => { h.window.electronNative.minimize = "not-a-function"; },
        h => { h.window.nativeStorage = undefined; },
        h => { h.window.__APP_CONFIG__.productName = "another-product"; },
        h => { h.window.__APP_CONFIG__ = undefined; },
        ...standalonePaths.map(
            path => h => { h.context.location.pathname = path; }),
    ];
    for (const change of rejected) {
        const h = hubHarness();
        change(h);
        assert.equal(h.inject(forced).status, "skipped");
        assert.equal(h.text.nodeValue, "File", "rejected pages remain unchanged");
        assert.equal(h.observers.length, 0, "no observer in rejected page");
        assert.equal(h.window.__ag_hanhua_engine__, undefined);
    }
    for (const change of [
        h => { h.document.readyState = "loading"; h.window.__APP_CONFIG__ = undefined; },
        h => { h.root = null; },
        h => { h.root.childElementCount = 0; },
        h => { h.document.scripts = [{ src: "https://example.com/main.js" }]; },
    ]) {
        const h = hubHarness();
        change(h);
        assert.equal(h.inject(forced).status, "pending", "wait without injecting incomplete shells");
        assert.equal(h.observers.length, 0);
        assert.equal(h.text.nodeValue, "File");
    }
    const loading = hubHarness();
    loading.root.childElementCount = 0;
    assert.equal(loading.inject(guarded).status, "pending");
    loading.root.childElementCount = 1;
    assert.equal(loading.inject(guarded).status, "injected");

    for (const change of [
        ...standalonePaths.map(path => h => { h.context.location.pathname = path; }),
        h => { h.context.location.origin = "https://example.com"; },
        h => { h.window.electronNative = undefined; },
    ]) {
        const h = hubHarness();
        assert.equal(h.inject(guarded).status, "injected");
        change(h);
        h.text.nodeValue = "Save";
        assert.equal(h.text.nodeValue, "Save", "same-document identity change stops translation");
        h.tick(5000);
        assert.equal(h.text.nodeValue, "Save", "delayed timers remain stopped");
        assert.equal(h.observers[0].roots.size, 0);
        assert.equal(h.intervals.size, 0);
        assert.equal(h.window.__ag_hanhua_engine__.active(), false);
        assert.equal(h.window.__ag_hanhua_engine__.state.stopped, true);
    }
    const readonly = hubHarness();
    readonly.inject(guarded);
    readonly.context.location.pathname = "/artifact-preview";
    assert.equal(readonly.inject(diagnosis).status, "skipped");
    assert.equal(readonly.window.__ag_hanhua_engine__.state.stopped, false,
        "diagnosis cannot call active() or stop the engine itself");
    assert.equal(readonly.observers[0].roots.size, 1);
    readonly.tick(3000);
    assert.equal(readonly.window.__ag_hanhua_engine__.state.stopped, true,
        "normal engine timer, not diagnosis, enforces changed identity");
}
