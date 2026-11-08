"use strict";

const assert = require("node:assert/strict");
const vm = require("node:vm");
const chunks = [];
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => chunks.push(chunk));
process.stdin.on("end", () => {
    try {
        test(JSON.parse(chunks.join("")));
        process.stdout.write("collection probes: ok\n");
    } catch (error) {
        console.error(error.stack || error);
        process.exitCode = 1;
    }
});

function test(expressions) {
    const secret = "SECRET_ACCOUNT_PRIVATE_URL_CSRF";
    const fail = () => { throw Error("Unexpected mutation or private read"); };
    function harness() {
        const reads = [];
        const element = (tag = "DIV", attrs = {}, privateAttrs = false) => ({
            nodeType: 1, tagName: tag, attrs: {...attrs}, childNodes: [],
            parentElement: null, parentNode: null, shadowRoot: null, isConnected: true,
            get childElementCount() { return this.childNodes.filter(node => node.nodeType === 1).length; },
            getAttribute(name) {
                if (expressions.sources.includes(name)) {
                    reads.push(name);
                    if (privateAttrs) fail();
                }
                return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
            },
            hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); },
            setAttribute: fail, appendChild: fail, remove: fail,
        });
        const text = (value, privateText = false) => ({
            nodeType: 3, parentElement: null, parentNode: null, isConnected: true,
            get nodeValue() {
                reads.push("text");
                if (privateText) fail();
                return value;
            },
            set nodeValue(_) { fail(); },
        });
        const append = (parent, child) => {
            parent.childNodes.push(child);
            child.parentNode = parent;
            child.parentElement = parent.nodeType === 1 ? parent : null;
            return child;
        };
        const html = element("HTML");
        const body = append(html, element("BODY"));
        const root = append(body, element("DIV", {id: "root"}));
        append(root, element("DIV"));
        const window = {};
        window.top = window;
        window.electronNative = {minimize: fail, maximize: fail};
        window.nativeStorage = {getItems: fail};
        window.__APP_CONFIG__ = {productName: "antigravity", appVersion: "2.29.0"};
        Object.defineProperty(window.__APP_CONFIG__, "csrfToken", {get: fail});
        const document = {
            readyState: "complete", documentElement: html,
            getElementById: key => key === "root" ? root : null,
            scripts: [{src: "https://127.0.0.1:7555/main.js"}],
            createTreeWalker: fail, querySelectorAll: fail, createElement: fail,
        };
        for (const object of [document, html, root]) {
            for (const key of ["title", "innerText", "textContent"]) {
                Object.defineProperty(object, key, {get: fail, set: fail});
            }
        }
        const context = vm.createContext({
            window, document, URL,
            location: {origin: "https://127.0.0.1:7555", pathname: "/", href: `https://127.0.0.1:7555/?secret=${secret}`},
            MutationObserver: fail, setTimeout: fail, setInterval: fail, clearInterval: fail,
            fetch: fail, WebSocket: fail,
        });
        const marker = (id, tag = "BUTTON", attrs = {}, privateAttrs = false) =>
            append(root, element(tag, {"data-testid": id, ...attrs}, privateAttrs));
        const shadow = host => {
            const fragment = {nodeType: 11, host, childNodes: [], parentElement: null, parentNode: null};
            host.shadowRoot = fragment;
            return fragment;
        };
        return {window, document, context, root, html, body, reads, element, text, append, marker, shadow};
    }
    const run = (h, mode = "stats") => {
        const value = vm.runInContext(expressions[mode], h.context, {timeout: 1000});
        const plain = JSON.parse(JSON.stringify(value));
        assert.equal(JSON.stringify(plain).includes(secret), false, "private data must never leave a probe");
        return plain;
    };
    const candidates = result => result.candidates.map(item => [item.source, item.text, item.count]);

    const stats = harness();
    const known = stats.marker("new-conversation-button", "BUTTON", {title: secret}, true);
    stats.append(known, stats.text(secret, true));
    const unknown = stats.append(stats.root, stats.element("BUTTON", {title: secret}, true));
    stats.append(unknown, stats.text(secret, true));
    const inputOwner = stats.marker("new-conversation-button", "DIV");
    stats.append(inputOwner, stats.element("INPUT", {placeholder: secret}, true));
    const defaultResult = run(stats);
    assert.equal(defaultResult.status, "ready");
    assert.equal(defaultResult.confirmed, true);
    assert.equal(defaultResult.text_included, false);
    assert.deepEqual(defaultResult.candidates, []);
    assert.equal(defaultResult.sources.text, 1);
    assert.equal(defaultResult.sources.title, 1);
    assert.equal(defaultResult.sources.placeholder, 1);
    assert.deepEqual(stats.reads, [], "default mode must not read any UI values");

    const wide = harness();
    const unvisited = wide.marker("new-conversation-button", "BUTTON", {title: secret}, true);
    wide.append(unvisited, wide.text("New Conversation", true));
    for (let index = 0; index < 10; index++) wide.append(wide.root, wide.element("DIV"));
    const limited = run(wide, "limited");
    assert.equal(limited.status, "error");
    assert.equal(limited.reason, "collection-limit");
    assert.deepEqual(wide.reads, [], "over-budget scans stop before reading or returning partial content");

    const included = harness();
    const first = included.marker("new-conversation-button", "BUTTON", {title: "New Conversation"});
    included.append(first, included.text(" \nNew\tConversation \n"));
    included.append(first, included.text("New Conversation"));
    const rename = included.marker("conversation-rename-menu-item", "DIV", {role: "menuitem", "aria-label": "Rename"});
    included.append(rename, included.text("Rename"));
    const arbitrary = included.append(included.root, included.element("BUTTON", {title: secret}, true));
    included.append(arbitrary, included.text("New Conversation", true));
    const invalidKnown = included.marker("conversation-rename-menu-item");
    included.append(invalidKnown, included.text(secret));
    const wrongLabel = included.marker("new-conversation-button");
    included.append(wrongLabel, included.text("Rename"));
    const result = run(included, "include");
    assert.equal(result.text_included, true);
    assert.equal(result.withheld.sensitive, 2);
    const actual = candidates(result).sort();
    assert.deepEqual(actual, [
        ["aria-label", "Rename", 1], ["text", "New Conversation", 2],
        ["text", "Rename", 1], ["title", "New Conversation", 1],
    ].sort());
    assert.equal(JSON.stringify(result).includes("\\t"), false, "output contains canonical static literals only");

    const boundaries = [
        ["PRE", {}], ["CODE", {}], ["DIV", {class: "terminal"}],
        ["DIV", {class: "editor-container"}], ["DIV", {class: "agy-user-bubble"}],
        ["DIV", {"data-testid": "conversation-row-private"}],
        ["DIV", {"data-testid": "project-header"}],
        ["DIV", {"data-testid": "user-message-content"}],
        ["DIV", {"data-testid": "input-attachment"}],
        ["DIV", {"data-project-label": secret}],
        ["DIV", {role: "tooltip"}],
        ["DIV", {class: "relative w-full select-none cursor-pointer"}],
    ];
    for (const editable of ["", "true", "TRUE", "plaintext-only"]) {
        boundaries.push(["DIV", {contenteditable: editable}]);
    }
    for (const [tag, attrs] of boundaries) {
        const h = harness();
        const parent = h.append(h.root, h.element(tag, {...attrs, title: secret}, true));
        let leaf = parent;
        for (let depth = 0; depth < 30; depth++) leaf = h.append(leaf, h.element("DIV"));
        const child = h.append(leaf, h.element("BUTTON", {"data-testid": "new-conversation-button", title: secret}, true));
        h.append(child, h.text("New Conversation", true));
        const blocked = run(h, "include");
        assert.deepEqual(blocked.candidates, [], `blocked subtree ${JSON.stringify(attrs)}`);
        assert.deepEqual(h.reads, [], "blocked attributes and text are never read");
    }

    for (const value of ["false", "FALSE"]) {
        const h = harness();
        const parent = h.append(h.root, h.element("DIV", {contenteditable: value}));
        const child = h.append(parent, h.element("BUTTON", {"data-testid": "new-conversation-button"}));
        h.append(child, h.text("New Conversation"));
        assert.deepEqual(candidates(run(h, "include")), [["text", "New Conversation", 1]]);
    }
    const inheritance = harness();
    const editable = inheritance.append(inheritance.root, inheritance.element("DIV", {contenteditable: "true"}));
    const falseChild = inheritance.append(editable, inheritance.element("BUTTON", {
        contenteditable: "false", "data-testid": "new-conversation-button", title: secret,
    }, true));
    inheritance.append(falseChild, inheritance.text("New Conversation", true));
    assert.deepEqual(run(inheritance, "include").candidates, []);
    assert.deepEqual(inheritance.reads, []);

    const shadow = harness();
    const host = shadow.marker("new-conversation-button", "DIV");
    const fragment = shadow.shadow(host);
    shadow.append(fragment, shadow.text("New Conversation"));
    const control = shadow.append(fragment, shadow.element("BUTTON", {"aria-label": "New Conversation"}));
    shadow.append(control, shadow.text("New Conversation"));
    const shadowResult = run(shadow, "include");
    assert.deepEqual(candidates(shadowResult).sort(), [
        ["aria-label", "New Conversation", 1], ["text", "New Conversation", 2],
    ].sort());

    const blockedShadow = harness();
    const blockedHost = blockedShadow.append(blockedShadow.root, blockedShadow.element("DIV", {class: "monaco-editor"}));
    const privateFragment = blockedShadow.shadow(blockedHost);
    const blockedControl = blockedShadow.append(privateFragment, blockedShadow.element("BUTTON", {
        "data-testid": "new-conversation-button", title: secret,
    }, true));
    blockedShadow.append(blockedControl, blockedShadow.text("New Conversation", true));
    assert.deepEqual(run(blockedShadow, "include").candidates, []);
    assert.deepEqual(blockedShadow.reads, []);

    const input = harness();
    const inputLabel = input.marker("new-conversation-button", "DIV");
    const field = input.append(inputLabel, input.element("INPUT", {placeholder: "New Conversation", title: secret}));
    Object.defineProperty(field, "value", {get: fail, set: fail});
    assert.deepEqual(candidates(run(input, "include")), [["placeholder", "New Conversation", 1]]);
    assert.deepEqual(input.reads, ["placeholder"]);

    for (const attrs of [{hidden: ""}, {inert: ""}, {"aria-hidden": "TRUE"}]) {
        const h = harness();
        const hidden = h.marker("new-conversation-button", "BUTTON", {...attrs, title: secret}, true);
        h.append(hidden, h.text("New Conversation", true));
        assert.deepEqual(run(h, "include").candidates, []);
        assert.deepEqual(h.reads, []);
    }

    for (const change of [
        h => { h.window.top = {}; },
        h => { h.context.location.origin = "https://elsewhere.invalid"; },
        h => { h.context.location.pathname = "/artifact/private"; },
        h => { h.context.location.pathname = "/terminal-standalone/private"; },
        h => { h.window.electronNative = undefined; },
        h => { h.window.__APP_CONFIG__.productName = secret; },
    ]) {
        for (const mode of ["stats", "include", "misses"]) {
            const h = harness();
            change(h);
            Object.defineProperty(h.window, "__ag_hanhua_engine__", {get: fail});
            Object.defineProperty(h.document, "documentElement", {get: fail});
            assert.equal(run(h, mode).status, "skipped");
            assert.deepEqual(h.reads, []);
        }
    }
    const loading = harness();
    loading.root.childNodes = [];
    Object.defineProperty(loading.window, "__ag_hanhua_engine__", {get: fail});
    assert.equal(run(loading).status, "pending");
    const identityFailure = harness();
    Object.defineProperty(identityFailure.window, "electronNative", {get: fail});
    assert.equal(run(identityFailure).reason, "identity-unavailable");

    const validEngine = () => ({
        scope: expressions.scope,
        misses: {schema: 1, sources: Object.fromEntries(expressions.sources.map((source, index) => [source, index]))},
        active: fail, disconnect: fail, observe: fail,
    });
    const goodMisses = harness();
    const engine = validEngine();
    Object.defineProperty(engine, "state", {get: fail});
    Object.defineProperty(engine.misses, secret, {get: fail});
    Object.defineProperty(engine.misses.sources, secret, {get: fail});
    Object.freeze(engine.misses.sources);
    Object.freeze(engine.misses);
    Object.freeze(engine);
    goodMisses.window.__ag_hanhua_engine__ = engine;
    Object.defineProperty(goodMisses.document, "documentElement", {get: fail});
    const numeric = run(goodMisses, "misses");
    assert.equal(numeric.status, "ready");
    assert.equal(numeric.mode, "misses");
    assert.deepEqual(numeric.candidates, []);
    assert.deepEqual(numeric.sources, Object.fromEntries(expressions.sources.map((source, index) => [source, index])));
    assert.equal(goodMisses.window.__ag_hanhua_engine__, engine);

    const legacy = new Set([secret]);
    legacy[Symbol.iterator] = fail;
    for (const invalid of [
        () => undefined,
        () => ({scope: expressions.scope, misses: legacy, active: fail}),
        () => ({...validEngine(), scope: "old"}),
        () => ({...validEngine(), misses: {schema: 2, sources: {}}}),
        () => Object.defineProperty(validEngine(), "misses", {get: fail}),
        () => Object.defineProperty(validEngine(), "scope", {get: fail}),
        () => ({...validEngine(), misses: Object.defineProperty({sources: {}}, "schema", {get: fail})}),
        () => ({...validEngine(), misses: Object.defineProperty({schema: 1}, "sources", {get: fail})}),
        () => ({...validEngine(), misses: {schema: 1, sources: Object.create(validEngine().misses.sources)}}),
        () => {
            const value = validEngine();
            Object.defineProperty(value.misses.sources, "text", {get: fail});
            return value;
        },
    ]) {
        const h = harness();
        h.window.__ag_hanhua_engine__ = invalid();
        assert.equal(run(h, "misses").reason, "engine-upgrade-required");
        assert.deepEqual(h.reads, []);
    }
    for (const value of [-1, NaN, Infinity, 0.5, true, "3", Number.MAX_SAFE_INTEGER + 1]) {
        const h = harness();
        h.window.__ag_hanhua_engine__ = validEngine();
        h.window.__ag_hanhua_engine__.misses.sources.text = value;
        assert.equal(run(h, "misses").reason, "engine-upgrade-required");
    }
    const markerGetter = harness();
    Object.defineProperty(markerGetter.window, "__ag_hanhua_engine__", {get: fail});
    assert.equal(run(markerGetter, "misses").reason, "engine-upgrade-required");
}
