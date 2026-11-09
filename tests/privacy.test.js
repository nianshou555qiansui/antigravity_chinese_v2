"use strict";

const assert = require("node:assert/strict");
const { makeHarness } = require("./engine.test.js");

const attributes = ["placeholder", "title", "aria-label", "alt", "data-title",
    "data-tooltip-content", "aria-description", "aria-placeholder"];
const longPhrase = "Create a new project from one or more folders.";
const unknown = "Fixture untranslated interface sentence";

function add(h, parent = h.document.body, tag = "div", attrs = {}, text = null) {
    const element = parent.appendChild(new h.ElementStub(tag, attrs));
    const node = text === null ? null : element.appendChild(new h.TextStub(text));
    return { element, node };
}

function policy(h, payload) { return h.inject(payload.policy); }
function report(h) { return h.window.__ag_hanhua_engine__.misses; }
function plain(value) { return JSON.parse(JSON.stringify(value)); }

function hardBoundaries() {
    return [
        ...["script", "style", "code", "pre", "svg", "canvas", "symbol", "path", "math",
            "kbd", "iframe", "object", "embed"].map(tag => [tag, {}]),
        ...["code-view", "editor-container", "monaco-editor", "suggest-widget", "output-view",
            "debug-console", "artifact-container", "code-block", "diff-view", "input-area",
            "chat-input"].map(value => ["div", { class: `prefix-${value}-suffix` }]),
        ...["terminal", "xterm", "preview", "agy-user-bubble", "compact-tooltip"]
            .map(value => ["div", { class: `other\t${value}\nlast` }]),
        ...["user-input-step", "user-message-content", "user-message-bubble", "input-attachment", "user-message-document-chip",
            "project-header", "project-selector-item", "project-selector-trigger", "conversation-group-header",
            "breadcrumb-segment", "side-question-origin", "side-question-tab-header-name", "side-question-tab-header-title",
            "subagent-tab-header-title", "subagent-node", "conversation-navigator-card",
            "conversation-navigator-group-card-header", "conversation-navigator-group", "settings-nav-item-Account",
            "conversation-row-sidebar", "conversation-row-history", "section-header"]
            .map(value => ["div", { "data-testid": value }]),
        ...["code", "img", "tooltip"].map(value => ["div", { role: value }]),
        ...["", "true", "TRUE", "plaintext-only"].map(value => ["div", { contenteditable: value }]),
        ["div", { "data-lexical-editor": "true", contenteditable: "false" }],
        ["article", { "data-quotable": "true", "aria-label": "Agent response" }],
        ["span", { "data-quotable-inline": "true" }],
        ["div", { "data-project-name": "File" }],
        ["div", { "data-project-label": "File" }],
        ["div", { class: "relative\tw-full\nselect-none\rcursor-pointer" }],
    ];
}

function testPolicyBoundaries(payload) {
    const h = makeHarness();
    const p = policy(h, payload);
    assert.deepEqual(plain(p.sources), payload.sources);
    assert.deepEqual(plain(p.attributes), payload.sources.filter(name => name !== "text"));
    assert.ok(!p.attributes.includes("data-title"));
    for (const name of ["class", "role", "contenteditable", "data-testid", "data-lexical-editor",
        "data-quotable", "data-quotable-inline", "data-project-name", "data-project-label"]) {
        assert.ok(p.boundaryAttributes.includes(name), `boundary mutations must be observed: ${name}`);
    }
    for (const [tag, attrs] of hardBoundaries()) {
        const outer = add(h, h.document.body, tag, attrs).element;
        const child = add(h, outer, "button", {}, "File");
        assert.equal(p.allowsText(child.node), false, `hard text ${tag} ${JSON.stringify(attrs)}`);
        for (const attr of attributes) {
            assert.equal(p.allowsAttribute(child.element, attr), false, `hard attribute ${attr}`);
        }
        assert.equal(p.collectionSource(child.node), null);
    }
    for (const attrs of [
        { class: "terminal-settings-row preview-settings select-none cursor-pointer" },
        { class: "relative-more w-full select-none cursor-pointer" },
        { class: "relative w-fullish select-none cursor-pointer" },
        { "data-testid": "user-message-actions" },
        { "data-testid": "conversation-view", "data-cascade-id": "fixture" },
        { "data-testid": "pinned-section-header" },
        { "data-quotable-root": "true" },
        { contenteditable: "false" },
        { contenteditable: "FALSE" },
        { contenteditable: "inherit" },
    ]) {
        const fixture = add(h, h.document.body, "div", attrs, "File");
        assert.equal(p.allowsText(fixture.node), true, `unrelated UI is not hard: ${JSON.stringify(attrs)}`);
    }
    const editable = add(h, h.document.body, "div", { contenteditable: "true" }).element;
    const override = add(h, editable, "div", { contenteditable: "false" }, "File");
    assert.equal(p.allowsText(override.node), false, "false child cannot override a hard editable ancestor");
    const header = add(h, h.document.body, "div", { "data-testid": "section-header" }).element;
    const pinned = add(h, header, "span", { "data-testid": "pinned-section-header" }, "Pinned");
    assert.equal(p.allowsText(pinned.node), false, "fixed descendants cannot override a dynamic title boundary");
    for (const tag of ["div", "span", "img", "video", "audio"]) {
        const fixture = add(h, h.document.body, tag, { title: "File", "aria-label": "File" });
        assert.equal(p.allowsAttribute(fixture.element, "title"), false);
        assert.equal(p.allowsAttribute(fixture.element, "aria-label"), false);
    }
    for (const role of [null, "button", "menuitem", "menuitemcheckbox", "menuitemradio", "tab",
        "checkbox", "radio", "switch", "combobox", "searchbox"]) {
        const fixture = add(h, h.document.body, role ? "div" : "button", role ? { role } : {});
        assert.equal(p.allowsAttribute(fixture.element, "title"), true);
        assert.equal(p.allowsAttribute(fixture.element, "aria-label"), true);
        assert.equal(p.allowsAttribute(fixture.element, "data-title"), false);
        assert.equal(p.allowsAttribute(fixture.element, "value"), false);
        assert.equal(p.allowsAttribute(fixture.element, "placeholder"), false);
    }
}

function testEngineHardBoundaries(payload) {
    const h = makeHarness();
    const fixtures = [];
    for (const [tag, attrs] of hardBoundaries()) {
        const outer = add(h, h.document.body, tag, attrs).element;
        const controls = Object.fromEntries(attributes.map(name => [name, "File"]));
        const button = add(h, outer, "button", controls, "File");
        const phrase = add(h, outer, "span", {}, longPhrase);
        const dynamic = add(h, outer, "span", {}, 'No apps found for "File".');
        const input = add(h, outer, "input", { placeholder: "File", "aria-placeholder": "File" });
        fixtures.push({ button, phrase, dynamic, input });
    }
    h.inject(payload.engine);
    const zero = JSON.stringify(report(h));
    for (const fixture of fixtures) {
        assert.equal(fixture.button.node.nodeValue, "File");
        assert.equal(fixture.phrase.node.nodeValue, longPhrase);
        assert.equal(fixture.dynamic.node.nodeValue, 'No apps found for "File".');
        for (const attr of attributes) assert.equal(fixture.button.element.getAttribute(attr), "File");
        assert.equal(fixture.input.element.getAttribute("placeholder"), "File");
        assert.equal(fixture.input.element.getAttribute("aria-placeholder"), "File");
        fixture.button.node.nodeValue = "Save";
        fixture.button.element.setAttribute("title", "Save");
        fixture.button.element.appendChild(new h.TextStub("File"));
    }
    h.tick(5000);
    for (const fixture of fixtures) {
        assert.equal(fixture.button.node.nodeValue, "Save", "characterData stays protected");
        assert.equal(fixture.button.element.getAttribute("title"), "Save", "attribute mutation stays protected");
        assert.equal(fixture.button.element.childNodes.at(-1).nodeValue, "File", "added child stays protected");
    }
    assert.equal(JSON.stringify(report(h)), zero, "protected strings never become misses");
    h.inject(payload.engine);
    h.tick(5000);
    for (const fixture of fixtures) assert.equal(fixture.button.element.getAttribute("title"), "Save");
    assert.equal(Object.values(report(h).sources).reduce((sum, count) => sum + count, 0), 0);
}

function testInputAndControlAttributes(payload) {
    const h = makeHarness();
    const p = policy(h, payload);
    const ordinary = add(h, h.document.body, "div", Object.fromEntries(attributes.map(name => [name, "File"])), "File");
    const button = add(h, h.document.body, "button", Object.fromEntries(attributes.map(name => [name, "File"])), "File");
    const unknownReads = [];
    const ordinaryRead = ordinary.element.getAttribute.bind(ordinary.element);
    ordinary.element.getAttribute = name => {
        if (attributes.includes(name)) unknownReads.push(name);
        return ordinaryRead(name);
    };
    const inputs = [];
    for (const tag of ["input", "textarea", "select", "option"]) {
        const fixture = add(h, h.document.body, tag, Object.fromEntries(attributes.map(name => [name, "File"])), "File");
        Object.defineProperty(fixture.element, "value", {
            get() { assert.fail("input values are not translation input"); },
            set() { assert.fail("input values must not be rewritten"); },
        });
        for (const attr of attributes) {
            assert.equal(p.allowsAttribute(fixture.element, attr), ["input", "textarea"].includes(tag)
                && ["placeholder", "aria-placeholder"].includes(attr));
        }
        assert.equal(p.allowsText(fixture.node), false);
        inputs.push({ ...fixture, tag });
    }
    const wrapped = add(h, h.document.body, "input").element;
    const nested = add(h, wrapped, "input", { placeholder: "File" });
    assert.equal(p.allowsAttribute(nested.element, "placeholder"), false);
    const action = add(h, h.document.body, "button", { "data-testid": "user-message-actions", title: "File" }, "File");
    const view = add(h, h.document.body, "div", { "data-testid": "conversation-view", "data-cascade-id": "fixture" }, "File");
    h.inject(payload.engine);
    h.tick(5000);
    assert.equal(ordinary.node.nodeValue, payload.dictionary.File);
    assert.deepEqual(unknownReads, [], "unknown metadata is rejected before reading its value");
    for (const attr of attributes) assert.equal(ordinary.element.attrs.get(attr), "File", "unknown metadata stays original");
    for (const attr of attributes) {
        const expected = ["placeholder", "aria-placeholder", "data-title"].includes(attr) ? "File" : payload.dictionary.File;
        assert.equal(button.element.getAttribute(attr), expected, `control metadata ${attr}`);
    }
    for (const { element, node, tag } of inputs) {
        assert.equal(node.nodeValue, "File");
        for (const attr of attributes) {
            const translated = ["input", "textarea"].includes(tag) && ["placeholder", "aria-placeholder"].includes(attr);
            assert.equal(element.getAttribute(attr), translated ? payload.dictionary.File : "File");
        }
    }
    assert.equal(action.node.nodeValue, payload.dictionary.File, "message actions are independent fixed UI");
    assert.equal(action.element.getAttribute("title"), payload.dictionary.File);
    assert.equal(view.node.nodeValue, payload.dictionary.File, "conversation-view is not a user-content boundary");
    assert.equal(nested.element.getAttribute("placeholder"), "File");
}

function testShadowDepthAndCycles(payload) {
    const h = makeHarness();
    const p = policy(h, payload);
    const hard = add(h, h.document.body, "div", { class: "editor-container" }).element;
    let current = hard;
    for (let index = 0; index < 50; index++) current = add(h, current).element;
    const deep = add(h, current, "button", { title: "File" }, "File");
    assert.equal(p.allowsText(deep.node), false, "hard ancestors beyond the old 25-level cap are checked");
    const outerRoot = current.attachShadow({ mode: "open" });
    const host = add(h, outerRoot).element;
    const innerRoot = host.attachShadow({ mode: "open" });
    const direct = innerRoot.appendChild(new h.TextStub("File"));
    const shadow = add(h, innerRoot, "button", { title: "File" }, "File");
    assert.equal(p.allowsText(direct), false);
    assert.equal(p.allowsAttribute(shadow.element, "title"), false);
    let beyondLimit = h.document.body;
    for (let index = 0; index < 260; index++) beyondLimit = add(h, beyondLimit).element;
    const extreme = add(h, beyondLimit, "button", { title: "File" }, "File");
    assert.equal(p.allowsText(extreme.node), false, "over-limit traversal fails closed");
    assert.equal(p.allowsAttribute(extreme.element, "title"), false);
    const cycle = new h.ElementStub("button", { title: "File" });
    Object.defineProperty(cycle, "isConnected", { value: true });
    cycle.parentNode = cycle;
    assert.equal(p.allowsText(cycle), false, "synthetic ancestry cycles fail closed");
    assert.equal(p.allowsAttribute(cycle, "title"), false);
    const safe = add(h).element.attachShadow({ mode: "open" });
    const safeShadow = add(h, safe, "button", { title: "File" }, "File");
    h.inject(payload.engine);
    assert.equal(deep.node.nodeValue, "File");
    assert.equal(direct.nodeValue, "File");
    assert.equal(shadow.element.getAttribute("title"), "File");
    assert.equal(extreme.node.nodeValue, "File");
    assert.equal(safeShadow.node.nodeValue, payload.dictionary.File);
    assert.equal(safeShadow.element.getAttribute("title"), payload.dictionary.File);
    const lateRoot = add(h, hard).element.attachShadow({ mode: "open" });
    const late = add(h, lateRoot, "button", { title: "File" }, "File");
    h.tick(5000);
    assert.equal(late.node.nodeValue, "File", "late shadow children remain protected");
    assert.equal(late.element.getAttribute("title"), "File");
}

function testBoundaryChanges(payload) {
    const h = makeHarness();
    const p = policy(h, payload);
    const fixture = add(h, h.document.body, "button", { title: "File" }, "File");
    h.inject(payload.engine);
    assert.equal(fixture.node.nodeValue, payload.dictionary.File);
    for (const [name, value] of [["contenteditable", "true"], ["class", "terminal"],
        ["data-testid", "conversation-row-sidebar"], ["data-lexical-editor", "true"],
        ["data-quotable", "true"], ["role", "tooltip"]]) {
        fixture.element.setAttribute(name, value);
        fixture.node.nodeValue = "Save";
        fixture.element.setAttribute("title", "Save");
        assert.equal(fixture.node.nodeValue, "Save");
        assert.equal(fixture.element.getAttribute("title"), "Save");
        assert.equal(p.allowsText(fixture.node), false);
        h.tick(3000);
        assert.equal(fixture.node.nodeValue, "Save", "cached admission must not bypass changed boundaries");
        fixture.element.removeAttribute(name);
        h.tick(3000);
        assert.equal(fixture.node.nodeValue, payload.dictionary.Save, "newly safe text is reconsidered");
        assert.equal(fixture.element.getAttribute("title"), payload.dictionary.Save);
    }
    const title = add(h, h.document.body, "div", { "data-testid": "section-header" }).element;
    const control = add(h, title, "button", { title: "File" }, "File");
    assert.equal(control.node.nodeValue, "File");
    h.document.body.appendChild(control.element);
    assert.equal(control.node.nodeValue, payload.dictionary.File, "moving to safe UI recalculates admission");
    title.appendChild(control.element);
    control.node.nodeValue = "Save";
    control.element.setAttribute("title", "Save");
    h.tick(3000);
    assert.equal(control.node.nodeValue, "Save", "moving into user title cannot reuse earlier permission");
    assert.equal(control.element.getAttribute("title"), "Save");
}

function testMutationRouting(payload) {
    const h = makeHarness();
    const fixture = add(h, h.document.body, "button", { title: "File" }, "File");
    const shadowRoot = fixture.element.attachShadow({ mode: "open" });
    const shadow = add(h, shadowRoot, "span", {}, "File");
    h.inject(payload.engine);
    assert.equal(fixture.node.nodeValue, payload.dictionary.File);
    assert.equal(shadow.node.nodeValue, payload.dictionary.File);

    let descendantReads = 0;
    for (const node of [fixture.node, shadow.node]) {
        let value = node.nodeValue;
        Object.defineProperty(node, "nodeValue", {
            get() { descendantReads++; return value; },
            set(next) { value = next; },
        });
    }
    h.batch(() => {
        fixture.element.setAttribute("title", "File");
        fixture.element.setAttribute("title", "Save");
    });
    assert.equal(fixture.element.getAttribute("title"), payload.dictionary.Save);
    assert.equal(descendantReads, 0, "ordinary attribute changes do not traverse light or shadow descendants");

    h.batch(() => {
        fixture.element.setAttribute("class", "terminal");
        fixture.node.nodeValue = "File";
        fixture.element.setAttribute("title", "File");
    });
    assert.equal(fixture.node.nodeValue, "File", "new privacy boundaries are rechecked before text reads");
    h.batch(() => {
        fixture.element.removeAttribute("class");
        fixture.node.nodeValue = "Save";
    });
    assert.equal(fixture.node.nodeValue, payload.dictionary.Save, "removing a boundary rescans newly safe text");
}

function testStaticCollection(payload) {
    const h = makeHarness();
    const p = policy(h, payload);
    for (const [testId, labels] of Object.entries(payload.staticLabels)) {
        for (const label of labels) {
            const owner = add(h, h.document.body, "button", { "data-testid": testId, title: label }, label);
            assert.equal(p.collectionSource(owner.node), "text");
            assert.equal(p.acceptsStatic(owner.node, null, label), true, `audited text ${testId}`);
            assert.equal(p.collectionSource(owner.element, "title"), "title");
            assert.equal(p.acceptsStatic(owner.element, "title", label), true);
            assert.equal(p.acceptsStatic(owner.node, null, `User ${label}`), false, "marker alone is not proof");
            assert.equal(p.acceptsStatic(owner.element, "title", "File"), false);
            const inner = add(h, owner.element, "span", {}, label);
            assert.equal(p.acceptsStatic(inner.node, null, label), true, "fixed label descendants inherit audited context");
        }
    }
    for (const attrs of [{}, { role: "button" }, { role: "menuitem" }, { "data-testid": "unknown-menu-item" }]) {
        const fixture = add(h, h.document.body, "button", attrs, "Rename");
        assert.equal(p.collectionSource(fixture.node), null, "generic controls do not establish static UI origin");
        assert.equal(p.acceptsStatic(fixture.node, null, "Rename"), false);
        assert.equal(p.collectionSource(fixture.element, "aria-label"), null);
    }
    for (const attrs of [{ hidden: "" }, { inert: "" }, { "aria-hidden": "true" }]) {
        const container = add(h, h.document.body, "div", attrs).element;
        const fixture = add(h, container, "button", { "data-testid": "conversation-rename-menu-item" }, "Rename");
        assert.equal(p.collectionSource(fixture.node), null, "hidden fixed labels cannot be collected");
    }
    const hard = add(h, h.document.body, "div", { "data-testid": "user-message-content" }).element;
    const spoof = add(h, hard, "button", { "data-testid": "conversation-rename-menu-item" }, "Rename");
    assert.equal(p.collectionSource(spoof.node), null, "static marker cannot override hard ancestors");
    const detached = add(h, h.document.body, "button", { "data-testid": "conversation-rename-menu-item" }, "Rename");
    h.document.body.removeChild(detached.element);
    assert.equal(p.collectionSource(detached.node), null);
    const dynamic = add(h, h.document.body, "button", { "data-testid": "conversation-rename-menu-item" }, "Rename");
    for (const text of ['No apps found for "private query".', "Send feedback as person@example.invalid",
        "Select project, current: Secret Project", "Rename Secret Project", "File"]) {
        assert.equal(p.acceptsStatic(dynamic.node, null, text), false, "dynamic or mismatched raw values stay on-page");
    }
    for (const raw of [null, {}, [], 1, "", "a", "only@example.invalid", "See https://example.invalid/path",
        "Open C:\\Users\\private", "Read /home/private/project", "Use ~/private/project", "Read \\\\server\\private",
        "let value = {};", "hello;", "English \u4e2d\u6587", "x".repeat(121)]) {
        assert.equal(p.usableText(raw), false, "sensitive and nontext shapes are excluded");
    }
    for (const raw of ["Rename", "New Conversation", unknown]) assert.equal(p.usableText(raw), true);
}

function testMissCounts(payload) {
    const h = makeHarness();
    const expected = Object.fromEntries(payload.sources.map(name => [name, 0]));
    const text = add(h, h.document.body, "span", {}, unknown);
    expected.text++;
    const control = add(h, h.document.body, "button").element;
    for (const attr of payload.sources.filter(name => !["text", "placeholder", "aria-placeholder"].includes(name))) {
        control.setAttribute(attr, `${unknown} ${attr}`);
        expected[attr]++;
    }
    const input = add(h, h.document.body, "input", { placeholder: unknown, "aria-placeholder": unknown }).element;
    expected.placeholder++;
    expected["aria-placeholder"]++;
    const hard = add(h, h.document.body, "div", { "data-testid": "conversation-row-history" }).element;
    add(h, hard, "button", { title: unknown }, unknown);
    add(h, h.document.body, "div", { title: unknown, "data-title": unknown });
    for (const raw of ["fixture@example.invalid", "https://example.invalid/private", "C:\\Users\\private",
        "/home/private/project", "~/private/project", "\\\\server\\private", "private;", "Private \u4e2d\u6587"]) {
        add(h, h.document.body, "button", { title: raw }, raw);
    }
    h.inject(payload.engine);
    assert.equal(h.window.__ag_hanhua_engine__.state.privacy, 1);
    assert.deepEqual(plain(report(h)), { schema: 1, sources: expected });
    const before = JSON.stringify(report(h));
    h.tick(5000);
    h.tick(5000);
    assert.equal(JSON.stringify(report(h)), before, "periodic rescans do not inflate misses");
    text.node.nodeValue = `${unknown} changed`;
    expected.text++;
    text.node.nodeValue = `${unknown} changed`;
    control.setAttribute("title", `${unknown} changed title`);
    expected.title++;
    control.setAttribute("title", `${unknown} changed title`);
    input.setAttribute("placeholder", `${unknown} changed placeholder`);
    expected.placeholder++;
    assert.deepEqual(plain(report(h)), { schema: 1, sources: expected });
    const output = JSON.stringify(report(h));
    for (const secret of [unknown, "example.invalid", "private", "C:\\Users", "changed"]) assert.ok(!output.includes(secret));
    assert.deepEqual(Object.keys(report(h)).sort(), ["schema", "sources"]);
    assert.deepEqual(Object.keys(report(h).sources).sort(), [...payload.sources].sort());
    report(h).sources.text = 999999;
    text.node.nodeValue = `${unknown} saturation one`;
    text.node.nodeValue = `${unknown} saturation two`;
    assert.equal(report(h).sources.text, 1000000, "source counters saturate");
    const prior = report(h);
    h.inject(payload.engine);
    assert.notEqual(report(h), prior, "reinjection starts a new bounded counter snapshot");
    h.tick(5000);
    assert.ok(!JSON.stringify(report(h)).includes(unknown));
    for (const count of Object.values(report(h).sources)) assert.ok(Number.isSafeInteger(count) && count >= 0 && count <= 1000000);
}

const tests = {
    boundaries: testPolicyBoundaries,
    hard: testEngineHardBoundaries,
    attributes: testInputAndControlAttributes,
    shadow: testShadowDepthAndCycles,
    mutation: testBoundaryChanges,
    routing: testMutationRouting,
    collection: testStaticCollection,
    misses: testMissCounts,
};
const chunks = [];
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => chunks.push(chunk));
process.stdin.on("end", () => {
    try {
        const payload = JSON.parse(chunks.join(""));
        const selected = process.argv[2];
        assert.ok(Object.prototype.hasOwnProperty.call(tests, selected), "known test case required");
        tests[selected](payload);
        process.stdout.write(`ok: privacy ${selected}\n`);
    } catch (error) {
        console.error(error.stack || error);
        process.exitCode = 1;
    }
});
