"""Offline privacy boundaries across the policy, engine, and window guard."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cdp_targets
import hanhua_v2
import privacy_policy


NODE_SETUP = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {makeHarness} = require('./tests/engine.test.js');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const privateAttrs = new Set(['title', 'aria-label', 'alt', 'placeholder',
    'aria-placeholder', 'data-tooltip-content', 'aria-description', 'data-title']);
function watchPrivateReads(element, text) {
    const reads = {text: 0, attribute: 0};
    const getAttribute = element.getAttribute.bind(element);
    element.getAttribute = name => {
        if (privateAttrs.has(name)) reads.attribute++;
        return getAttribute(name);
    };
    Object.defineProperty(text, 'nodeValue', {
        configurable: true,
        get() { reads.text++; return this.value; },
        set(value) { this.value = value; },
    });
    return reads;
}
function privateLeaf(h, parent) {
    const leaf = parent.appendChild(new h.ElementStub('button', {
        title: 'Save', 'aria-label': 'File', 'data-title': 'PRIVATE_ACCOUNT_CONTEXT',
    }));
    const text = leaf.appendChild(new h.TextStub('Save'));
    return {leaf, text, reads: watchPrivateReads(leaf, text)};
}
"""


class PrivacyIntegrationTests(unittest.TestCase):
    def run_node(self, source):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node.js is required for offline privacy tests')
        engine = hanhua_v2.build_engine_js({'Save': '[saved]', 'File': '[file]'})
        page = {'url': 'http://127.0.0.1:7555/'}
        payload = {
            'engine': engine,
            'policy': privacy_policy.build_policy_js(),
            'probe': cdp_targets.guarded_expression(page, None),
            'guarded': cdp_targets.guarded_expression(page, engine),
            'scope': cdp_targets.ENGINE_SCOPE,
        }
        result = subprocess.run(
            [node, '-e', NODE_SETUP + source],
            input=json.dumps(payload, ensure_ascii=True),
            text=True,
            encoding='utf-8',
            capture_output=True,
            timeout=30,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_hard_values_are_never_read_by_any_engine_entry(self):
        self.run_node(r"""
const h = makeHarness();
const hard = h.document.body.appendChild(new h.ElementStub('div', {
    'data-testid': 'conversation-row-sidebar',
}));
const fixture = privateLeaf(h, hard);
h.inject(payload.engine);
h.observers[0].callback([{type: 'characterData', target: fixture.text}]);
h.observers[0].callback([{type: 'attributes', target: fixture.leaf, attributeName: 'title'}]);
h.observers[0].callback([{type: 'childList', target: hard, addedNodes: [fixture.leaf]}]);
h.tick(5000);
assert.deepEqual(fixture.reads, {text: 0, attribute: 0});
assert.equal(fixture.text.value, 'Save');
assert.equal(fixture.leaf.attrs.get('title'), 'Save');
const misses = h.window.__ag_hanhua_engine__.misses;
assert.equal(JSON.stringify(misses).includes('PRIVATE_ACCOUNT_CONTEXT'), false);
assert.equal(Object.values(misses.sources).reduce((a, b) => a + b, 0), 0);
""")

    def test_detached_mutation_records_do_not_reclassify_private_nodes(self):
        self.run_node(r"""
const h = makeHarness();
h.inject(payload.engine);
const hard = h.document.body.appendChild(new h.ElementStub('div', {
    'data-testid': 'user-message-content',
}));
const fixture = privateLeaf(h, hard);
hard.removeChild(fixture.leaf);
assert.equal(fixture.leaf.isConnected, false);
const observer = h.observers[0];
observer.callback([
    {type: 'childList', target: hard, addedNodes: [fixture.leaf]},
    {type: 'characterData', target: fixture.text},
    {type: 'attributes', target: fixture.leaf, attributeName: 'aria-label'},
]);
assert.deepEqual(fixture.reads, {text: 0, attribute: 0});
assert.equal(fixture.text.value, 'Save');
assert.equal(fixture.leaf.attrs.get('aria-label'), 'File');
""")

    def test_detached_shadow_hosts_stay_private(self):
        self.run_node(r"""
const h = makeHarness();
h.inject(payload.engine);
const host = h.document.body.appendChild(new h.ElementStub('div', {
    'data-testid': 'input-attachment',
}));
const shadow = host.attachShadow({mode: 'open'});
const fixture = privateLeaf(h, shadow);
h.document.body.removeChild(host);
assert.equal(fixture.text.isConnected, false);
h.observers[0].callback([{type: 'characterData', target: fixture.text}]);
h.observers[0].callback([{type: 'attributes', target: fixture.leaf, attributeName: 'title'}]);
assert.deepEqual(fixture.reads, {text: 0, attribute: 0});
""")

    def test_known_media_and_origin_markers_protect_dynamic_button_attributes(self):
        self.run_node(r"""
for (const testId of ['user-input-step', 'side-question-origin']) {
    const h = makeHarness();
    const container = h.document.body.appendChild(new h.ElementStub('div', {
        'data-testid': testId,
    }));
    const fixture = privateLeaf(h, container);
    const neighbor = h.document.body.appendChild(new h.ElementStub('button', {
        title: 'File', 'data-testid': 'conversation-rename-menu-item',
    }));
    const neighborText = neighbor.appendChild(new h.TextStub('Save'));
    h.inject(payload.engine);
    h.observers[0].callback([{type: 'attributes', target: fixture.leaf, attributeName: 'aria-label'}]);
    h.tick(5000);
    assert.deepEqual(fixture.reads, {text: 0, attribute: 0}, testId);
    assert.equal(fixture.text.value, 'Save');
    assert.equal(neighborText.nodeValue, '[saved]', 'adjacent fixed controls stay translatable');
    assert.equal(neighbor.getAttribute('title'), '[file]');
}
""")

    def test_slots_check_both_light_and_composed_ancestors(self):
        self.run_node(r"""
for (const privateSide of ['light', 'composed']) {
    const h = makeHarness();
    const light = h.document.body.appendChild(new h.ElementStub('div',
        privateSide === 'light' ? {'data-testid': 'user-message-content'} : {}));
    const host = h.document.body.appendChild(new h.ElementStub('div',
        privateSide === 'composed' ? {'data-quotable': 'true'} : {}));
    const slot = host.attachShadow({mode: 'open'}).appendChild(new h.ElementStub('slot'));
    const fixture = privateLeaf(h, light);
    fixture.leaf.assignedSlot = slot;
    const policy = h.inject(payload.policy);
    assert.equal(policy.allowsText(fixture.text), false, privateSide);
    assert.equal(policy.allowsAttribute(fixture.leaf, 'title'), false, privateSide);
    assert.equal(policy.collectionSource(fixture.text), null, privateSide);
    h.inject(payload.engine);
    h.observers[0].callback([{type: 'characterData', target: fixture.text}]);
    h.observers[0].callback([{type: 'attributes', target: fixture.leaf, attributeName: 'title'}]);
    h.tick(5000);
    assert.deepEqual(fixture.reads, {text: 0, attribute: 0}, privateSide);
}

const h = makeHarness();
const host = h.document.body.appendChild(new h.ElementStub('div'));
const slot = host.attachShadow({mode: 'open'}).appendChild(new h.ElementStub('slot'));
const leaf = host.appendChild(new h.ElementStub('button', {title: 'File'}));
const text = leaf.appendChild(new h.TextStub('Save'));
leaf.assignedSlot = slot;
const policy = h.inject(payload.policy);
assert.equal(policy.allowsText(text), true, 'ordinary diamond ancestry is not a cycle');
assert.equal(policy.allowsAttribute(leaf, 'title'), true);
h.inject(payload.engine);
assert.equal(text.nodeValue, '[saved]');
assert.equal(leaf.getAttribute('title'), '[file]');
""")

    def test_ancestor_cycles_fail_closed_without_reading_content(self):
        self.run_node(r"""
const h = makeHarness();
const leaf = h.document.body.appendChild(new h.ElementStub('button', {title: 'File'}));
const text = leaf.appendChild(new h.TextStub('Save'));
const reads = watchPrivateReads(leaf, text);
leaf.assignedSlot = leaf;
const policy = h.inject(payload.policy);
assert.equal(policy.allowsText(text), false);
assert.equal(policy.allowsAttribute(leaf, 'title'), false);
assert.equal(policy.collectionSource(text), null);
h.inject(payload.engine);
h.observers[0].callback([{type: 'characterData', target: text}]);
h.tick(5000);
assert.deepEqual(reads, {text: 0, attribute: 0});
""")

    def test_boundary_mutations_precede_reads_and_cached_values(self):
        self.run_node(r"""
const h = makeHarness();
const parent = h.document.body.appendChild(new h.ElementStub('div'));
const leaf = parent.appendChild(new h.ElementStub('button', {title: 'File'}));
const text = leaf.appendChild(new h.TextStub('Save'));
h.inject(payload.engine);
assert.equal(text.value, '[saved]');
assert.equal(leaf.attrs.get('title'), '[file]');

parent.attrs.set('data-testid', 'user-message-content');
text.value = 'Save';
leaf.attrs.set('title', 'File');
const reads = watchPrivateReads(leaf, text);
h.observers[0].callback([{type: 'attributes', target: parent, attributeName: 'data-testid'}]);
h.observers[0].callback([{type: 'characterData', target: text}]);
h.tick(5000);
assert.deepEqual(reads, {text: 0, attribute: 0});
assert.equal(text.value, 'Save');
assert.equal(leaf.attrs.get('title'), 'File');

parent.attrs.delete('data-testid');
h.observers[0].callback([{type: 'attributes', target: parent, attributeName: 'data-testid'}]);
assert.equal(text.value, '[saved]', 'leaving a hard zone permits the current UI value');
assert.equal(leaf.attrs.get('title'), '[file]');
""")

    def test_main_guard_upgrades_old_engine_without_reading_legacy_misses(self):
        self.run_node(r"""
const h = makeHarness();
let activeCalls = 0;
let disconnected = 0;
let missReads = 0;
const old = {
    scope: 'antigravity-main-v1',
    active() { activeCalls++; return true; },
    disconnect() { disconnected++; },
};
Object.defineProperty(old, 'misses', {get() { missReads++; throw Error('private legacy set'); }});
h.window.__ag_hanhua_engine__ = old;
h.window.top = h.window;
h.window.electronNative = {minimize() {}, maximize() {}};
h.window.nativeStorage = {getItems() {}};
h.window.__APP_CONFIG__ = {productName: 'Antigravity', appVersion: '2.29.0'};
h.document.readyState = 'complete';
h.document.getElementById = name => name === 'root' ? {childElementCount: 1} : null;
h.document.scripts = [{src: 'http://127.0.0.1:7555/main.js'}];
h.context.URL = URL;
h.context.location = {origin: 'http://127.0.0.1:7555', pathname: '/', href: 'http://127.0.0.1:7555/'};
const probe = h.inject(payload.probe);
assert.equal(probe.status, 'missing');
assert.equal(activeCalls, 0, 'legacy engine is not accepted as alive');
assert.equal(missReads, 0);
const result = h.inject(payload.guarded);
assert.equal(result.status, 'injected');
assert.equal(disconnected, 1);
assert.equal(missReads, 0);
assert.equal(h.window.__ag_hanhua_engine__.scope, payload.scope);
assert.equal(h.window.__ag_hanhua_engine__.state.privacy, 1);
assert.equal(h.window.__ag_hanhua_engine__.misses.schema, 1);
""")


if __name__ == '__main__':
    unittest.main()
