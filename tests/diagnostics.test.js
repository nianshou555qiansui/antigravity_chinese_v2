"use strict";

const assert = require("node:assert/strict");
const vm = require("node:vm");
const chunks = [];
process.stdin.setEncoding("utf8");
process.stdin.on("data", value => chunks.push(value));
process.stdin.on("end", () => {
    try {
        test(JSON.parse(chunks.join("")));
        process.stdout.write("diagnostic probe: ok\n");
    } catch (error) {
        console.error(error.stack || error);
        process.exitCode = 1;
    }
});

function test({ expression, scope }) {
    const secret = "SECRET_ACCOUNT_PRIVATE_URL_CSRF";
    const fail = () => { throw Error("Unexpected mutation or getter"); };
    const harness = () => {
        const window = {};
        window.top = window;
        window.electronNative = { minimize: fail, maximize: fail };
        window.nativeStorage = { getItems: fail };
        window.__APP_CONFIG__ = { productName: "antigravity", appVersion: "2.29.0" };
        Object.defineProperty(window.__APP_CONFIG__, "csrfToken", { get: fail });
        const root = { childElementCount: 1 };
        const document = {
            readyState: "complete",
            getElementById: key => key === "root" ? root : null,
            scripts: [{ src: "https://127.0.0.1:7555/main.js" }],
        };
        for (const key of ["title", "innerText", "textContent"]) Object.defineProperty(document, key, { get: fail });
        const context = vm.createContext({
            window, document, URL,
            location: { origin: "https://127.0.0.1:7555", pathname: "/", href: `https://127.0.0.1:7555/?secret=${secret}` },
            MutationObserver: fail, setTimeout: fail, setInterval: fail, clearInterval: fail,
        });
        return { window, document, context, root };
    };
    const run = h => {
        const result = vm.runInContext(expression, h.context, { timeout: 1000 });
        assert.equal(JSON.stringify(result).includes(secret), false, "no private result data");
        return result;
    };
    const validEngine = () => ({
        scope, state: { started: true, stopped: false, entries: 123 },
        active: fail, disconnect: fail, observe: fail,
    });

    const missing = harness();
    assert.equal(run(missing).engine, "missing");
    assert.equal(missing.window.__ag_hanhua_engine__, undefined, "no injection");

    for (const [started, stopped, expected] of [
        [true, false, "running"], [false, false, "not-started"],
        [true, true, "stopped"], [false, true, "stopped"],
    ]) {
        const h = harness();
        const engine = validEngine();
        engine.state = { started, stopped, entries: 123 };
        Object.defineProperty(engine, "misses", { get: fail });
        Object.freeze(engine.state);
        Object.freeze(engine);
        h.window.__ag_hanhua_engine__ = engine;
        const result = run(h);
        assert.equal(result.engine, expected);
        assert.equal(result.entries, 123);
        assert.equal(result.confirmed, true);
        assert.equal(h.window.__ag_hanhua_engine__, engine);
        assert.deepEqual(engine.state, { started, stopped, entries: 123 });
    }

    const invalid = [
        () => ({ scope }),
        () => ({ scope: "old", state: { started: true, stopped: false, entries: 1 } }),
        () => ({ scope, state: { started: true, stopped: false, entries: "123" } }),
        () => ({ scope, state: { started: true, stopped: false, entries: -1 } }),
        () => ({ scope, state: { started: true, stopped: false, entries: NaN } }),
        () => ({ scope, state: { started: 1, stopped: false, entries: 123 } }),
        () => ({ scope, state: Object.create({ started: true, stopped: false, entries: 123 }) }),
        () => Object.defineProperty(validEngine(), "scope", { get: fail }),
        () => Object.defineProperty(validEngine(), "state", { get: fail }),
        () => ({ scope, state: Object.defineProperty({ stopped: false, entries: 123 }, "started", { get: fail }) }),
    ];
    for (const create of invalid) {
        const h = harness();
        h.window.__ag_hanhua_engine__ = create();
        assert.equal(run(h).engine, "unknown", "legacy or accessor state stays unknown");
    }
    const accessor = harness();
    Object.defineProperty(accessor.window, "__ag_hanhua_engine__", { get: fail });
    assert.equal(run(accessor).engine, "unknown");

    for (const change of [
        h => { h.window.top = {}; },
        h => { h.context.location.origin = "https://elsewhere.invalid"; },
        h => { h.context.location.pathname = "/artifact/user-preview"; },
        h => { h.window.electronNative = undefined; },
        h => { h.window.__APP_CONFIG__.productName = "not-antigravity"; },
    ]) {
        const h = harness();
        change(h);
        Object.defineProperty(h.window, "__ag_hanhua_engine__", { get: fail });
        const result = run(h);
        assert.equal(result.status, "skipped");
        assert.equal(result.engine, undefined);
    }
    const loading = harness();
    loading.root.childElementCount = 0;
    assert.equal(run(loading).status, "pending");
    assert.equal(loading.window.__ag_hanhua_engine__, undefined);

    const unavailable = harness();
    Object.defineProperty(unavailable.window, "electronNative", { get: fail });
    const identity = run(unavailable);
    assert.equal(identity.status, "skipped");
    assert.equal(identity.reason, "identity-unavailable");
    assert.equal(identity.engine, undefined);
    assert.equal(unavailable.window.__ag_hanhua_engine__, undefined);
}
