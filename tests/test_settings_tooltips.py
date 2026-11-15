"""Reported 2.32.1 settings misses and narrowly audited tooltip admission."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hanhua_v2
import privacy_policy


NODE_SETUP = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {makeHarness} = require('./tests/engine.test.js');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
function add(h, parent, tag, attrs = {}, raw = null) {
    const element = parent.appendChild(new h.ElementStub(tag, attrs));
    const text = raw === null ? null : element.appendChild(new h.TextStub(raw));
    return {element, text};
}
function queuedAnchor(h, parent = h.document.body) {
    const card = add(h, parent, 'div', {class: 'agy-card'}).element;
    const row = add(h, card, 'div', {class: 'w-full'}).element;
    const extra = add(h, row, 'div', {class: 'px-3 pb-2 -mt-1 w-full'}).element;
    const label = add(h, extra, 'span', {class: 'flex items-center gap-1 text-sm text-muted-foreground'}).element;
    add(h, label, 'span', {}, 'Keyboard shortcuts');
    return add(h, label, 'span', {
        class: 'cursor-pointer inline-flex items-center hover:text-foreground',
        'data-tooltip-id': ':r2t7:-2',
    }).element;
}
function defaultAnchor(h, parent = h.document.body) {
    const menu = add(h, parent, 'div', {'data-testid': 'permissions-menu', role: 'menu'}).element;
    const option = add(h, menu, 'div', {'data-testid': 'permissions-preset-option-turbo', role: 'menuitem'}).element;
    const tag = add(h, option, 'span', {'data-testid': 'permissions-default-tag'}).element;
    return add(h, tag, 'div', {
        class: 'bg-secondary opacity-70 flex items-center gap-1 rounded-full px-1.5 text-sm',
        'data-tooltip-id': 'fixture-default-tag',
    }).element;
}
function tooltip(h, parent = h.document.body, raw = null, id = 'fixture-tooltip') {
    return add(h, parent, 'div', {id, role: 'tooltip', class: 'compact-tooltip'}, raw);
}
function spyText(h, text) {
    const descriptor = Object.getOwnPropertyDescriptor(h.TextStub.prototype, 'nodeValue');
    const reads = {count: 0};
    Object.defineProperty(text, 'nodeValue', {
        get() { reads.count++; return descriptor.get.call(this); },
        set(value) { descriptor.set.call(this, value); },
    });
    return reads;
}
"""


class SettingsTooltipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if cls.node is None:
            raise AssertionError('Node.js is required for settings tooltip tests')
        cls.payload = json.dumps({
            'engine': hanhua_v2.build_engine_js(hanhua_v2.load_dictionary()),
            'policy': privacy_policy.build_policy_js(),
        }, ensure_ascii=True)

    def run_node(self, source):
        result = subprocess.run(
            [self.node, '-e', NODE_SETUP + source], input=self.payload,
            text=True, encoding='utf-8', capture_output=True, timeout=30, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_layout_config_labels_and_voice_change_preserve_voice_names(self):
        self.run_node(r"""
const h = makeHarness();
const layouts = [
    ['Knowledge Work', '知识工作', 'Simplified interface for research, writing, and everyday tasks.', '适用于研究、写作和日常任务的简洁界面。'],
    ['Software Engineering', '软件工程', 'Full interface for coding, developer tools, and managing agents.', '包含编程、开发工具和智能体管理功能的完整界面。'],
].map(([raw, expected, description, expectedDescription]) => {
    const option = add(h, h.document.body, 'span', {role: 'radio', 'aria-label': raw});
    const label = add(h, option.element, 'span', {}, raw);
    const detail = add(h, option.element, 'div', {}, description);
    return {option, label, detail, expected, expectedDescription};
});
const change = add(h, h.document.body, 'button', {'data-testid': 'voice-settings-change'}, 'Change');
const name = add(h, h.document.body, 'span', {'data-testid': 'voice-settings-name'}, 'Neno');
const voiceReads = spyText(h, name.text);
h.inject(payload.engine);
for (const {option, label, detail, expected, expectedDescription} of layouts) {
    assert.equal(label.text.value, expected);
    assert.equal(detail.text.value, expectedDescription);
    assert.equal(option.element.getAttribute('aria-label'), expected);
}
assert.equal(change.text.value, '更换');
assert.equal(name.text.value, 'Neno');
name.text.value = 'File';
h.observers[0].callback([{type: 'characterData', target: name.text}]);
h.inject(payload.engine);
h.tick(5000);
assert.equal(name.text.value, 'File', 'voice names cannot match the general dictionary');
assert.equal(voiceReads.count, 0);
""")

    def test_sandbox_hint_templates_at_global_and_chat_positions(self):
        self.run_node(r"""
const cases = [
    ['Default and Auto need', '“默认”和“自动”'],
    ['Default needs', '“默认”'], ['Auto needs', '“自动”'],
];
for (const marker of ['agent-permissions', 'permissions-sandbox-hint']) {
    const h = makeHarness();
    const scope = add(h, h.document.body, 'div', {'data-testid': marker}).element;
    const fixtures = cases.map(([prefix, translated]) => {
        const raw = prefix + " the terminal sandbox, which isn’t available on this host.";
        return {fixture: add(h, scope, 'button', {title: raw}, raw),
            expected: translated + '模式需要终端沙箱，但当前主机无法使用该功能。'};
    });
    const invalid = add(h, scope, 'span', {}, "Default need the terminal sandbox, which isn't available on this host.");
    h.inject(payload.engine);
    for (const {fixture, expected} of fixtures) {
        assert.equal(fixture.text.value, expected);
        assert.equal(fixture.element.getAttribute('title'), expected);
    }
    assert.equal(invalid.text.value, "Default need the terminal sandbox, which isn't available on this host.");
    h.inject(payload.engine);
    for (const {fixture, expected} of fixtures) assert.equal(fixture.text.value, expected);
}
""")

    def test_queued_shortcuts_portal_waits_for_its_description_link(self):
        self.run_node(r"""
for (const [first, second] of [["Enter", "Alt+Enter"], ["\u23ce", "\u2325\u23ce"]]) {
    const h = makeHarness();
    const anchor = queuedAnchor(h);
    const tip = tooltip(h);
    const cases = [
        [first + ' Queues after the turn', first + ' 排队等待当前轮次结束后发送'],
        [second + ' Sends immediately', second + ' 立即发送'],
        [second + ' On empty prompt, sends next in queue', second + ' 输入为空时，发送队列中的下一条'],
    ].map(([raw, expected]) => ({...add(h, tip.element, 'span', {}, raw), expected}));
    const reads = cases.map(fixture => spyText(h, fixture.text));
    h.inject(payload.engine);
    assert.ok(reads.every(read => read.count === 0), 'unlinked tooltip bodies are never read');
    anchor.setAttribute('aria-describedby', 'other-description fixture-tooltip');
    for (const fixture of cases) assert.equal(fixture.text.value, fixture.expected);
    const p = h.inject(payload.policy);
    assert.equal(p.textScope(cases[0].text), 'queued-shortcuts');
    assert.equal(p.collectionSource(cases[0].text), null, 'translation never permits tooltip export');
    assert.equal(p.allowsAttribute(tip.element, 'title'), false);
    h.inject(payload.engine);
    cases[0].text.nodeValue = first + ' Sends immediately';
    assert.equal(cases[0].text.value, first + ' 立即发送');
}
""")

    def test_default_tags_only_translate_the_two_static_descriptions(self):
        self.run_node(r"""
const h = makeHarness();
const anchor = defaultAnchor(h);
anchor.setAttribute('aria-describedby', 'fixture-tooltip');
const tip = tooltip(h, h.document.body, 'Your This Computer setting');
h.inject(payload.engine);
assert.equal(tip.text.value, '你的此计算机设置');
tip.text.nodeValue = 'Your Remote setting';
assert.equal(tip.text.value, '你的远程设置');
const misses = JSON.stringify(h.window.__ag_hanhua_engine__.misses);
for (const raw of ['File', 'Save setting', 'Create a new project from one or more folders. setting']) {
    tip.text.nodeValue = raw;
    h.tick(3000);
    assert.equal(tip.text.value, raw, 'project names and unknown bodies must not use dictionary or substring replacement');
    assert.equal(JSON.stringify(h.window.__ag_hanhua_engine__.misses), misses);
}
const p = h.inject(payload.policy);
assert.equal(p.collectionSource(tip.text), null);
assert.equal(p.acceptsStatic(tip.text, null, 'Your Remote setting'), false);
""")

    def test_unknown_tooltips_are_rejected_before_text_and_attribute_reads(self):
        self.run_node(r"""
const h = makeHarness();
const anchor = add(h, h.document.body, 'button', {
    'data-tooltip-id': 'file-name-tooltip', 'aria-describedby': 'fixture-tooltip',
}).element;
const tip = tooltip(h, h.document.body, 'File');
const reads = spyText(h, tip.text);
let attributeReads = 0;
const leaf = add(h, tip.element, 'button', {title: 'Save'}).element;
const getAttribute = leaf.getAttribute.bind(leaf);
leaf.getAttribute = name => { if (name === 'title') attributeReads++; return getAttribute(name); };
h.inject(payload.engine);
tip.text.value = 'Save';
h.observers[0].callback([{type: 'characterData', target: tip.text}]);
h.observers[0].callback([{type: 'attributes', target: leaf, attributeName: 'title'}]);
h.inject(payload.engine);
h.tick(5000);
assert.equal(reads.count, 0);
assert.equal(attributeReads, 0);
assert.equal(tip.text.value, 'Save');
assert.equal(leaf.attrs.get('title'), 'Save');
assert.equal(Object.values(h.window.__ag_hanhua_engine__.misses.sources).reduce((a, b) => a + b, 0), 0);
""")

    def test_audited_anchors_and_portals_cannot_override_protected_ancestors(self):
        self.run_node(r"""
for (const attrs of [
    {'data-testid': 'user-message-content'}, {'data-testid': 'file-title-name'},
    {'data-testid': 'comment-card-text'}, {class: 'file-viewer-root'},
    {class: 'terminal'}, {contenteditable: 'true'}, {role: 'tooltip', class: 'compact-tooltip'},
]) {
    for (const side of ['anchor', 'portal']) {
        const h = makeHarness();
        const hard = add(h, h.document.body, 'div', attrs).element;
        const anchor = queuedAnchor(h, side === 'anchor' ? hard : h.document.body);
        anchor.setAttribute('aria-describedby', 'fixture-tooltip');
        const tip = tooltip(h, side === 'portal' ? hard : h.document.body, 'Enter Sends immediately');
        const reads = spyText(h, tip.text);
        h.inject(payload.engine);
        tip.text.value = 'Alt+Enter Sends immediately';
        h.observers[0].callback([{type: 'characterData', target: tip.text}]);
        h.inject(payload.engine);
        h.tick(5000);
        assert.equal(reads.count, 0, side + JSON.stringify(attrs));
        assert.equal(tip.text.value, 'Alt+Enter Sends immediately');
    }
}
""")

    def test_reused_portals_recheck_links_markers_and_parent_boundaries(self):
        self.run_node(r"""
const h = makeHarness();
const anchor = defaultAnchor(h);
anchor.setAttribute('aria-describedby', 'fixture-tooltip');
const tip = tooltip(h, h.document.body, 'Your Remote setting');
h.inject(payload.engine);
assert.equal(tip.text.value, '你的远程设置');
const reads = spyText(h, tip.text);
const other = add(h, h.document.body, 'button', {'data-tooltip-id': 'file-tooltip', 'aria-describedby': 'fixture-tooltip'}).element;
tip.text.nodeValue = 'File';
assert.equal(reads.count, 0, 'ambiguous portal ownership fails closed');
anchor.removeAttribute('aria-describedby');
tip.text.nodeValue = 'Save';
assert.equal(reads.count, 0, 'a reused filename portal cannot inherit permission admission');
other.removeAttribute('aria-describedby');
anchor.setAttribute('aria-describedby', 'fixture-tooltip');
tip.text.nodeValue = 'Your This Computer setting';
assert.equal(tip.text.value, '你的此计算机设置');
anchor.parentElement.setAttribute('data-testid', 'unknown-default-tag');
reads.count = 0;
tip.text.nodeValue = 'File';
assert.equal(reads.count, 0, 'marker changes invalidate admission');
anchor.parentElement.setAttribute('data-testid', 'permissions-default-tag');
anchor.parentElement.setAttribute('class', 'terminal');
reads.count = 0;
tip.text.nodeValue = 'Save';
assert.equal(reads.count, 0, 'source ancestry is rechecked before using the text cache');
""")

    def test_shadow_tooltip_links_and_hosts_use_the_same_privacy_gate(self):
        self.run_node(r"""
for (const protectedHost of [false, true]) {
    const h = makeHarness();
    const host = add(h, h.document.body, 'div', protectedHost ? {'data-testid': 'user-message-content'} : {}).element;
    const shadow = host.attachShadow({mode: 'open'});
    const anchor = queuedAnchor(h, shadow);
    const tip = tooltip(h, shadow, 'Alt+Enter On empty prompt, sends next in queue');
    const reads = spyText(h, tip.text);
    h.inject(payload.engine);
    assert.equal(reads.count, 0, 'unlinked shadow tooltip is not read');
    anchor.setAttribute('aria-describedby', 'fixture-tooltip');
    if (protectedHost) assert.equal(reads.count, 0);
    else assert.equal(tip.text.value, 'Alt+Enter 输入为空时，发送队列中的下一条');
    const p = h.inject(payload.policy);
    assert.equal(p.collectionSource(tip.text), null);
}
""")


if __name__ == '__main__':
    unittest.main()
