"use strict";

const fs = require("node:fs");
const { makeHarness } = require("./engine.test.js");

function buildTree(harness, nodes, dictionaryEntries) {
    const root = new harness.ElementStub("div", { class: "synthetic-benchmark" });
    harness.document.body.appendChild(root);
    const labels = [];
    for (let index = 0; index < nodes; index++) {
        const row = new harness.ElementStub("div");
        const label = `Label ${index % dictionaryEntries}`;
        const button = new harness.ElementStub("button", { "aria-label": label });
        const text = new harness.TextStub(label);
        row.appendChild(button);
        button.appendChild(text);
        root.appendChild(row);
        labels.push({ button, text, index });
    }
    return { root, labels };
}

function countTranslated(root, dictionaryEntries) {
    let count = 0;
    const visit = node => {
        for (const child of node.childNodes || []) {
            if (child.nodeType === 3 && /^标签 \d+$/.test(child.nodeValue)) count++;
            if (child.nodeType === 1 || child.nodeType === 11) visit(child);
        }
    };
    visit(root);
    return count;
}

function runCase(item) {
    const harness = makeHarness();
    const { root, labels } = buildTree(harness, item.nodes, item.dictionary_entries);
    const started = process.hrtime.bigint();
    harness.inject(item.engine);
    const initialMs = Number(process.hrtime.bigint() - started) / 1e6;
    const changed = Math.max(1, Math.floor(labels.length / 10));
    const mutationStarted = process.hrtime.bigint();
    harness.batch(() => {
        for (let offset = 0; offset < changed; offset++) {
            const label = labels[offset];
            label.text.nodeValue = `Label ${(label.index + 1) % item.dictionary_entries}`;
        }
    });
    const mutationMs = Number(process.hrtime.bigint() - mutationStarted) / 1e6;
    const attributeStarted = process.hrtime.bigint();
    harness.batch(() => {
        for (let offset = 0; offset < changed; offset++) {
            const label = labels[offset];
            label.button.setAttribute("aria-label", `Label ${(label.index + 2) % item.dictionary_entries}`);
        }
    });
    const attributeMs = Number(process.hrtime.bigint() - attributeStarted) / 1e6;

    harness.clearTimeouts();
    let idleTextReads = 0;
    for (const item of labels) {
        let value = item.text.nodeValue;
        Object.defineProperty(item.text, "nodeValue", {
            get() { idleTextReads++; return value; },
            set(next) { value = next; },
        });
    }
    const idleStarted = process.hrtime.bigint();
    harness.tick(3000);
    const idleAttributeFallbackMs = Number(process.hrtime.bigint() - idleStarted) / 1e6;
    return {
        name: item.name,
        labels: item.nodes,
        elements: 1 + item.nodes * 2,
        dictionary_entries: item.dictionary_entries,
        changed_nodes: changed,
        initial_ms: Math.round(initialMs * 100) / 100,
        mutation_batch_ms: Math.round(mutationMs * 100) / 100,
        attribute_batch_ms: Math.round(attributeMs * 100) / 100,
        idle_attribute_fallback_ms: Math.round(idleAttributeFallbackMs * 100) / 100,
        idle_text_reads: idleTextReads,
        translated: countTranslated(root, item.dictionary_entries),
    };
}

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
if (!Array.isArray(payload.cases) || payload.cases.length === 0) {
    throw Error("performance payload must contain cases");
}
const report = payload.cases.map(runCase);
process.stdout.write(JSON.stringify({ schema: 1, cases: report }) + "\n");
