"""Shared DOM privacy rules for translation and explicitly requested collection."""

import json


SOURCES = (
    "text", "placeholder", "title", "aria-label", "alt", "data-tooltip-content",
    "aria-description", "aria-placeholder",
)
STATIC_LABELS = {
    "new-conversation-button": ["New Conversation"],
    "history-button": ["Conversation History"],
    "conversation-rename-menu-item": ["Rename"],
    "conversation-pin-menu-item": ["Pin", "Unpin"],
    "conversation-mark-read-menu-item": ["Mark Read", "Mark Unread"],
    "conversation-restore-menu-item": ["Restore"],
    "conversation-delete-menu-item": ["Delete", "Delete Permanently"],
    "conversation-share-menu-item": ["Share"],
    "conversation-stop-menu-item": ["Stop"],
    "conversation-archive-menu-item": ["Archive"],
}


def build_policy_js():
    """Return a local expression; it neither reads page content nor changes globals."""
    return r"""(() => {
        const sources = SOURCES;
        const attributes = sources.filter(name => name !== 'text');
        const boundaryAttributes = ['class', 'role', 'contenteditable', 'hidden', 'aria-hidden', 'inert',
            'data-testid', 'data-quotable', 'data-quotable-inline', 'data-lexical-editor',
            'data-project-name', 'data-project-label', 'data-title'];
        const staticLabels = STATIC_LABELS;
        const hardTags = new Set(['SCRIPT', 'STYLE', 'CODE', 'PRE', 'SVG', 'CANVAS', 'SYMBOL',
            'PATH', 'MATH', 'KBD', 'IFRAME', 'OBJECT', 'EMBED']);
        const softTags = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'OPTION']);
        const classFragments = ['code-view', 'editor-container', 'monaco-editor', 'suggest-widget',
            'output-view', 'debug-console', 'artifact-container', 'code-block', 'diff-view',
            'input-area', 'chat-input'];
        const classTokens = new Set(['terminal', 'xterm', 'preview', 'agy-user-bubble', 'compact-tooltip']);
        const hardTestIds = new Set(['user-input-step', 'user-message-content', 'user-message-bubble', 'input-attachment',
            'user-message-document-chip', 'project-header', 'project-selector-item', 'project-selector-trigger',
            'conversation-group-header', 'section-header', 'breadcrumb-segment', 'side-question-origin',
            'side-question-tab-header-name',
            'side-question-tab-header-title', 'subagent-tab-header-title', 'subagent-node',
            'conversation-navigator-card', 'conversation-navigator-group-card-header',
            'conversation-navigator-group', 'settings-nav-item-Account']);
        const controlRoles = new Set(['button', 'menuitem', 'menuitemcheckbox', 'menuitemradio',
            'tab', 'checkbox', 'radio', 'switch', 'combobox', 'searchbox']);
        const read = (element, name) => element.getAttribute(name);
        const has = (element, name) => typeof element.hasAttribute === 'function'
            ? element.hasAttribute(name) : read(element, name) !== null;
        const tagOf = element => String(element.tagName || '').toUpperCase();
        function hard(element) {
            if (hardTags.has(tagOf(element))) return true;
            const editable = read(element, 'contenteditable');
            if (editable !== null && ['', 'true', 'plaintext-only'].includes(editable.toLowerCase())) return true;
            if (read(element, 'data-lexical-editor') === 'true' || read(element, 'data-quotable') === 'true'
                || read(element, 'data-quotable-inline') === 'true') return true;
            if (has(element, 'data-project-name') || has(element, 'data-project-label')) return true;
            const role = (read(element, 'role') || '').toLowerCase();
            if (['code', 'img', 'tooltip'].includes(role)) return true;
            const testId = read(element, 'data-testid') || '';
            if (hardTestIds.has(testId) || testId.startsWith('conversation-row-')) return true;
            const tokens = (read(element, 'class') || '').split(/[\t\n\f\r ]+/);
            if (tokens.some(token => classTokens.has(token)
                || classFragments.some(fragment => token.includes(fragment)))) return true;
            return tagOf(element) === 'DIV'
                && ['relative', 'w-full', 'select-none', 'cursor-pointer'].every(token => tokens.includes(token));
        }
        function ancestors(node) {
            const elements = [];
            const seen = new Set();
            const active = new Set();
            const pending = [[node, false]];
            while (pending.length) {
                const [current, leaving] = pending.pop();
                if (!current) continue;
                if (leaving) { active.delete(current); continue; }
                if (active.has(current)) return null;
                if (seen.has(current)) continue;
                if (seen.size >= 256) return null;
                seen.add(current);
                active.add(current);
                if (current.nodeType === 1) elements.push(current);
                // Both light-DOM ownership and composed slot/host ancestry must be trusted.
                pending.push([current, true], [current.assignedSlot, false],
                    [current.parentElement || current.parentNode, false], [current.host, false]);
            }
            return elements;
        }
        function chain(node) {
            try {
                if (!node || node.isConnected === false) return null;
                const elements = ancestors(node);
                return elements && !elements.some(hard) ? elements : null;
            } catch (_) { return null; }
        }
        function allowsText(node) {
            const elements = chain(node);
            return !!elements && !elements.some(element => softTags.has(tagOf(element)));
        }
        function allowsAttribute(element, name) {
            if (!attributes.includes(name) || !element || element.nodeType !== 1) return false;
            const elements = chain(element);
            if (!elements) return false;
            const tag = tagOf(element);
            if (softTags.has(tag)) {
                return ['INPUT', 'TEXTAREA'].includes(tag) && ['placeholder', 'aria-placeholder'].includes(name)
                    && !elements.slice(1).some(parent => softTags.has(tagOf(parent)));
            }
            if (elements.some(parent => softTags.has(tagOf(parent)))) return false;
            if (['placeholder', 'aria-placeholder'].includes(name)) return false;
            if (tag === 'IMG' || tag === 'VIDEO' || tag === 'AUDIO') return false;
            const role = (read(element, 'role') || '').toLowerCase();
            return tag === 'BUTTON' || controlRoles.has(role)
                || Object.prototype.hasOwnProperty.call(staticLabels, read(element, 'data-testid') || '');
        }
        function staticOwner(node) {
            const elements = chain(node);
            if (!elements || node.isConnected === false) return null;
            for (const element of elements) {
                if (has(element, 'hidden') || has(element, 'inert')
                    || (read(element, 'aria-hidden') || '').toLowerCase() === 'true') return null;
            }
            // Only audited marker/value pairs may leave the page as text.
            for (const element of elements) {
                const testId = read(element, 'data-testid') || '';
                if (Object.prototype.hasOwnProperty.call(staticLabels, testId)) return testId;
            }
            return null;
        }
        function collectionSource(node, attribute = null) {
            try {
                if (attribute === null) {
                    return node.nodeType === 3 && allowsText(node) && staticOwner(node) ? 'text' : null;
                }
                return allowsAttribute(node, attribute) && staticOwner(node) ? attribute : null;
            } catch (_) { return null; }
        }
        function usableText(raw) {
            if (typeof raw !== 'string') return false;
            const text = raw.trim();
            return text.length >= 2 && text.length <= 120 && /[A-Za-z]{2}/.test(text)
                && !/[\u3400-\u9fff]/.test(text)
                && !/[@{};]|https?:\/\/|[A-Za-z]:[\\/]|(?:^|[ ])(?:~?\/|\\\\)/i.test(text);
        }
        function acceptsStatic(node, attribute, raw) {
            if (!collectionSource(node, attribute) || !usableText(raw)) return false;
            const owner = staticOwner(node);
            return staticLabels[owner].includes(raw.replace(/\s+/g, ' ').trim());
        }
        return {sources, attributes, boundaryAttributes, allowsText, allowsAttribute,
            collectionSource, acceptsStatic, usableText};
    })()""".replace("SOURCES", json.dumps(SOURCES)).replace("STATIC_LABELS", json.dumps(STATIC_LABELS))
