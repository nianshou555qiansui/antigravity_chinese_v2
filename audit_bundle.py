#!/usr/bin/env python3
"""Create a read-only, review-oriented report from a local JavaScript bundle."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import hanhua_v2


SCHEMA_VERSION = 1
TOOL_VERSION = "1.0"
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_LITERALS = 2_000_000
MAX_REPORT_OCCURRENCES = 100_000
MAX_NEARBY_CANDIDATES = 5_000
UI_PROPERTIES = frozenset({
    "title", "description", "label", "tooltipText", "placeholder", "ariaLabel",
    "ariaDescription", "helperText", "caption", "emptyStateText",
})
KNOWN_LIBRARY_ERRORS = (
    "must be used within", "a generic type", "invalid quantifier", "cannot read properties of",
)
TERMS = (("Agent", "智能体"),)
TERM_EXCEPTIONS = ("Agent Market",)
PLACEHOLDER_PATTERNS = (
    ("template", re.compile(r"\$\{[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\}")),
    ("named-brace", re.compile(r"(?<![${])\{[A-Za-z_][\w.]*\}(?!\})")),
    ("printf", re.compile(r"%(?:\d+\$)?[sdif]")),
)
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9']*")
UI_PROPERTY_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*:\s*$")
ALL_CAPS_RE = re.compile(r"[A-Z][A-Z0-9_]{2,}\Z")
FILEISH_RE = re.compile(r"(?:^|[/\\])[^\s/\\]+\.(?:tsx?|jsx?|py|json|ya?ml|md|css|html|rs|go)\Z", re.I)
PRIVATE_DATA_PATTERNS = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b(?:https?://|ftp://|www\.)[^\s]+", re.I),
    re.compile(r"\b[A-Za-z]:[\\/][^\s]+"),
    re.compile(r"(?:^|\s)(?:~?/|\\\\)[^\s]+"),
)


class AuditError(Exception):
    pass


class JsonPairs(list):
    """Distinguish JSON objects from arrays while retaining duplicate keys."""


def _decode_escape(source, index):
    if index >= len(source):
        return "", index
    char = source[index]
    simple = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}
    if char in simple:
        return simple[char], index + 1
    if char in "\r\n":
        if char == "\r" and index + 1 < len(source) and source[index + 1] == "\n":
            return "", index + 2
        return "", index + 1
    if char == "x" and index + 2 < len(source):
        digits = source[index + 1:index + 3]
        if re.fullmatch(r"[0-9a-fA-F]{2}", digits):
            return chr(int(digits, 16)), index + 3
    if char == "u":
        if index + 1 < len(source) and source[index + 1] == "{":
            end = source.find("}", index + 2)
            digits = source[index + 2:end] if end >= 0 else ""
            if digits and len(digits) <= 6 and re.fullmatch(r"[0-9a-fA-F]+", digits):
                value = int(digits, 16)
                if value <= 0x10FFFF:
                    return chr(value), end + 1
        digits = source[index + 1:index + 5]
        if len(digits) == 4 and re.fullmatch(r"[0-9a-fA-F]{4}", digits):
            return chr(int(digits, 16)), index + 5
    return char, index + 1


def _decode_string(raw):
    output = []
    index = 0
    while index < len(raw):
        if raw[index] == "\\":
            value, index = _decode_escape(raw, index + 1)
            output.append(value)
        else:
            output.append(raw[index])
            index += 1
    text = "".join(output)
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return text


def _regex_can_start(previous):
    return previous in {
        "", "(", "[", "{", ":", ";", ",", "=", "!", "?", "&", "|", "+", "-", "*",
        "%", "~", "return", "throw", "case", "delete", "void", "typeof", "instanceof",
        "in", "of", "yield", "await", "else", "do", ")control", "=>",
    }


def _skip_regex(source, index):
    index += 1
    in_class = False
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
        elif char in "\r\n":
            return index, False
        elif char == "[":
            in_class = True
            index += 1
        elif char == "]":
            in_class = False
            index += 1
        elif char == "/" and not in_class:
            index += 1
            while index < len(source) and source[index].isalpha():
                index += 1
            return index, True
        else:
            index += 1
    return index, False


def _skip_template(source, index):
    """Skip a template literal, including nested interpolation expressions."""
    index += 1
    previous = ""
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
        elif char == "`":
            return index + 1, True
        elif char == "$" and index + 1 < len(source) and source[index + 1] == "{":
            index, previous, closed = _skip_expression(source, index + 2)
            if not closed:
                return index, False
        else:
            index += 1
    return index, False


def _skip_expression(source, index):
    depth = 1
    previous = "{"
    paren_context = []
    bracket_depth = 0
    control_paren_keywords = {"if", "while", "for", "with", "switch", "catch"}
    while index < len(source) and depth:
        char = source[index]
        if char in "'\"":
            index, previous, closed = _skip_quoted(source, index, char)
            if not closed:
                return index, previous, False
        elif char == "`":
            index, closed = _skip_template(source, index)
            if not closed:
                return index, "", False
            previous = "value"
        elif source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end + 1
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise AuditError("unterminated JavaScript block comment")
            index = end + 2
        elif char == "/" and _regex_can_start(previous):
            index, closed = _skip_regex(source, index)
            if not closed:
                raise AuditError("unterminated JavaScript regular expression")
            previous = "value"
        elif char == "[":
            bracket_depth += 1
            previous = char
            index += 1
        elif char == "]":
            bracket_depth = max(0, bracket_depth - 1)
            previous = char
            index += 1
        elif char == "{" and bracket_depth == 0:
            depth += 1
            previous = char
            index += 1
        elif char == "}" and bracket_depth == 0:
            depth -= 1
            previous = "value"
            index += 1
        else:
            if char == "=" and index + 1 < len(source) and source[index + 1] == ">":
                previous = "=>"
                index += 2
            elif char.isalpha() or char in "_$":
                end = index + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                previous = source[index:end]
                index = end
            elif char == "(":
                paren_context.append(previous in control_paren_keywords)
                previous = char
                index += 1
            elif char == ")":
                is_control = paren_context.pop() if paren_context else False
                previous = ")control" if is_control else char
                index += 1
            elif char == "[":
                bracket_depth += 1
                previous = char
                index += 1
            elif char == "]":
                bracket_depth = max(0, bracket_depth - 1)
                previous = char
                index += 1
            elif not char.isspace():
                previous = char
                index += 1
            else:
                index += 1
    return index, previous, depth == 0


def _skip_quoted(source, index, quote):
    index += 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
        elif source[index] == quote:
            return index + 1, "value", True
        elif source[index] in "\r\n" and quote != "`":
            return index, "", False
        else:
            index += 1
    return index, "", False


def _template_parts(raw):
    parts = []
    index = 0
    start = 0
    interpolated = False
    while index < len(raw):
        if raw[index] == "\\":
            index += 2
        elif raw[index] == "$" and index + 1 < len(raw) and raw[index + 1] == "{":
            interpolated = True
            static = _decode_string(raw[start:index])
            if static:
                parts.append(static)
            index, _, closed = _skip_expression(raw, index + 2)
            if not closed:
                raise AuditError("unterminated JavaScript template interpolation")
            start = index
        else:
            index += 1
    tail = _decode_string(raw[start:])
    if tail:
        parts.append(tail)
    return parts, interpolated


def _location(line_starts, offset):
    line_index = bisect_right(line_starts, offset) - 1
    return line_index + 1, offset - line_starts[line_index] + 1


def _literal_context(source, start):
    prefix = source[max(0, start - 120):start]
    match = UI_PROPERTY_RE.search(prefix)
    if match and match.group(1) in UI_PROPERTIES:
        return "ui-property:" + match.group(1)
    if re.search(r"createElement\s*\([^,]+,\s*null\s*,\s*$", prefix):
        return "direct-text"
    if re.search(r"children\s*:\s*$", prefix):
        return "children-property"
    return "string-literal"


def _scan_bundle(source):
    occurrences = []
    line_starts = [0]
    line_starts.extend(index + 1 for index, char in enumerate(source) if char == "\n")
    index = 0
    previous = ""
    paren_context = []
    control_paren_keywords = {"if", "while", "for", "with", "switch", "catch"}
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
        elif source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end + 1
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise AuditError("unterminated JavaScript block comment")
            index = end + 2
        elif char in "'\"":
            start = index
            end, _, closed = _skip_quoted(source, index, char)
            if not closed:
                raise AuditError("unterminated JavaScript string literal")
            raw = source[index + 1:end - 1]
            value = _decode_string(raw)
            line, column = _location(line_starts, start)
            occurrences.append({
                "text": value, "char_offset": start, "line": line,
                "column": column, "evidence": _literal_context(source, start),
                "kind": "string",
            })
            if len(occurrences) > MAX_LITERALS:
                raise AuditError("bundle contains too many string literals")
            index = end
            previous = "value"
        elif char == "`":
            start = index
            end, closed = _skip_template(source, index)
            if not closed:
                raise AuditError("unterminated JavaScript template literal")
            raw = source[index + 1:end - 1]
            parts, interpolated = _template_parts(raw)
            line, column = _location(line_starts, start)
            if interpolated:
                value = _decode_string(raw)
                occurrences.append({
                    "text": value, "parts": parts, "char_offset": start,
                    "line": line, "column": column,
                    "evidence": "dynamic-template", "kind": "template",
                })
            else:
                occurrences.append({
                    "text": _decode_string(raw), "char_offset": start,
                    "line": line, "column": column,
                    "evidence": "template-literal", "kind": "string",
                })
            if len(occurrences) > MAX_LITERALS:
                raise AuditError("bundle contains too many string literals")
            index = end
            previous = "value"
        elif char == "/" and _regex_can_start(previous):
            index, closed = _skip_regex(source, index)
            if not closed:
                raise AuditError("unterminated JavaScript regular expression")
            previous = "value"
        else:
            if char == "=" and index + 1 < len(source) and source[index + 1] == ">":
                previous = "=>"
                index += 2
            elif char.isalpha() or char in "_$":
                end = index + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                previous = source[index:end]
                index = end
            elif char == "(":
                # A regex may begin after the closing parenthesis of a control
                # statement (`if (ok) /.../`), but not after ordinary grouping
                # or a call (`(value) / divisor`). Keep that small bit of
                # context instead of treating every ')' as regex-friendly.
                paren_context.append(previous in control_paren_keywords)
                previous = char
                index += 1
            elif char == ")":
                is_control = paren_context.pop() if paren_context else False
                previous = ")control" if is_control else char
                index += 1
            else:
                previous = char
                index += 1
    return occurrences


def _placeholder_tokens(text):
    found = []
    for kind, pattern in PLACEHOLDER_PATTERNS:
        found.extend(f"{kind}:{match.group(0)}" for match in pattern.finditer(text))
    return Counter(found)


def _dictionary(dict_dir):
    files = []
    entries = []
    raw_seen = {}
    normalized_seen = {}
    problems = []
    for path in sorted(Path(dict_dir).glob("*.json"), key=lambda item: item.name):
        try:
            data = path.read_bytes()
            parsed = json.loads(data.decode("utf-8"), object_pairs_hook=JsonPairs)
        except (OSError, UnicodeError, json.JSONDecodeError):
            problems.append({"file": path.name, "kind": "invalid-json"})
            continue
        files.append({"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        if not isinstance(parsed, JsonPairs):
            problems.append({"file": path.name, "kind": "dictionary-not-object"})
            continue
        local_seen = set()
        for key, value in parsed:
            if not isinstance(key, str) or not isinstance(value, str) or not key.strip() or not value.strip():
                problems.append({"file": path.name, "kind": "invalid-entry"})
                continue
            normalized = hanhua_v2.normalize_text(key)
            if key in local_seen:
                problems.append({"file": path.name, "kind": "duplicate-json-key", "key": key})
            local_seen.add(key)
            if key in raw_seen:
                problems.append({"file": path.name, "kind": "duplicate-source-key", "key": key,
                                 "also_in": raw_seen[key]})
            else:
                raw_seen[key] = path.name
            if normalized in normalized_seen:
                prior = normalized_seen[normalized]
                problems.append({"file": path.name, "kind": "duplicate-normalized-key", "key": key,
                                 "normalized": normalized, "also_in": prior["file"],
                                 "also_key": prior["key"]})
            else:
                normalized_seen[normalized] = {"file": path.name, "key": key}
            entries.append({"file": path.name, "key": key, "value": value, "normalized": normalized})
    if not files:
        raise AuditError("no readable dictionary JSON files found")
    return entries, files, problems


def _candidate_filter(text, evidence):
    value = text.strip()
    if len(value) < 3 or len(value) > 300:
        return "outside-length-range"
    if not re.search(r"[A-Za-z]{2}", value) or re.search(r"[\u3400-\u9fff]", value):
        return "not-English-ui-text"
    if any(pattern.search(value) for pattern in PRIVATE_DATA_PATTERNS):
        return "private-data"
    if ALL_CAPS_RE.fullmatch(value):
        return "enum-or-constant"
    if FILEISH_RE.search(value):
        return "file-or-code-path"
    if sum(char.isdigit() for char in value) / max(1, len(value)) > 0.2:
        return "number-heavy"
    lower = value.lower()
    if any(error in lower for error in KNOWN_LIBRARY_ERRORS):
        return "known-library-error"
    if evidence == "string-literal" and not re.search(r"[A-Z]", value):
        return "no-capital-letter"
    if re.fullmatch(r"[A-Za-z0-9_$./-]+", value) and not re.search(r"\s", value):
        return "identifier-or-command"
    return ""


def _tokens(text):
    return {token.lower() for token in TOKEN_RE.findall(text) if len(token) > 1}


def _nearby_matches(text, candidates, threshold, token_index=None):
    tokens = _tokens(text)
    possible = []
    if token_index is None:
        pool = candidates
    else:
        unique = {}
        for token in tokens:
            for entry in token_index.get(token, ()):
                unique.setdefault((entry["file"], entry["key"]), entry)
        pool = sorted(unique.values(), key=lambda item: (item["normalized"], item["file"]))[:MAX_NEARBY_CANDIDATES]
    for entry in pool:
        other_tokens = entry["_tokens"]
        if not tokens or not other_tokens or not tokens.intersection(other_tokens):
            continue
        left = len(text.casefold())
        right = len(entry["normalized"].casefold())
        if 2 * min(left, right) / (left + right) < threshold:
            continue
        score = difflib.SequenceMatcher(None, text.casefold(), entry["normalized"].casefold(),
                                        autojunk=False).ratio()
        if score >= threshold:
            possible.append((score, entry))
    possible.sort(key=lambda item: (-item[0], item[1]["normalized"], item[1]["file"]))
    return [{"key": entry["key"], "file": entry["file"], "similarity": round(score, 4)}
            for score, entry in possible[:3]]


def _terminology(entries):
    findings = []
    for entry in entries:
        source = entry["key"]
        translation = entry["value"]
        for source_term, target_term in TERMS:
            matches = list(re.finditer(r"(?i)\b" + re.escape(source_term) + r"\b", source))
            if not matches:
                continue
            eligible = [match for match in matches if not any(
                re.match(re.escape(exception) + r"(?![A-Za-z0-9_])", source[match.start():], re.IGNORECASE)
                for exception in TERM_EXCEPTIONS)]
            translated_count = len(re.findall(re.escape(target_term), translation))
            if not eligible or translated_count >= len(eligible):
                continue
            findings.append({"file": entry["file"], "key": source, "translation": translation, "term": source_term,
                             "expected": target_term, "kind": "term-review"})
    return findings


def audit(bundle_path, version, dict_dir="dicts", similarity=0.85, expected_sha256=None):
    path = Path(bundle_path)
    try:
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise AuditError("bundle exceeds the 64 MiB limit")
        data = path.read_bytes()
    except AuditError:
        raise
    except OSError:
        raise AuditError("cannot read bundle") from None
    if not data or len(data) > MAX_BUNDLE_BYTES:
        raise AuditError("bundle is empty or exceeds the 64 MiB limit")
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        raise AuditError("bundle is not valid UTF-8") from None
    bundle_hash = hashlib.sha256(data).hexdigest()
    if expected_sha256 and bundle_hash.lower() != expected_sha256.lower():
        raise AuditError("bundle SHA-256 does not match --expect-sha256")

    entries, dict_files, dictionary_problems = _dictionary(dict_dir)
    literals = _scan_bundle(source)
    exact = defaultdict(list)
    dynamic = {}
    excluded = Counter()
    for item in literals:
        if item["kind"] == "template":
            key = item["text"]
            dynamic.setdefault(key, {"template": key, "parts": item["parts"], "occurrences": []})
            if len(dynamic[key]["occurrences"]) < 20:
                dynamic[key]["occurrences"].append({"line": item["line"], "column": item["column"],
                                                    "char_offset": item["char_offset"]})
            continue
        normalized = hanhua_v2.normalize_text(item["text"])
        if normalized:
            exact[normalized].append(item)

    bundle_texts = set(exact)
    dict_by_normalized = {}
    for entry in entries:
        dict_by_normalized.setdefault(entry["normalized"], []).append(entry)
    missing = [entry for normalized, items in dict_by_normalized.items() if normalized not in bundle_texts
               for entry in items]
    available_missing = [{**entry, "_tokens": _tokens(entry["normalized"])} for entry in missing]
    missing_by_token = defaultdict(list)
    for entry in available_missing:
        for token in entry["_tokens"]:
            missing_by_token[token].append(entry)
    additions = []
    possible_rewrites = []
    for normalized in sorted(bundle_texts):
        if normalized in dict_by_normalized:
            continue
        occurrences = exact[normalized]
        representative = min(occurrences, key=lambda item: item["char_offset"])
        evidence_set = {item["evidence"] for item in occurrences}
        filter_evidence = ("ui-context" if any(value.startswith("ui-property:")
                                               or value in {"direct-text", "children-property"}
                                               for value in evidence_set)
                           else representative["evidence"])
        reason = _candidate_filter(normalized, filter_evidence)
        if reason:
            excluded[reason] += len(occurrences)
            continue
        locations = [{"line": item["line"], "column": item["column"], "char_offset": item["char_offset"],
                      "evidence": item["evidence"]} for item in occurrences[:20]]
        near = _nearby_matches(normalized, available_missing, similarity, missing_by_token)
        record = {"text": normalized, "occurrences": len(occurrences), "locations": locations,
                  "evidence": sorted({item["evidence"] for item in occurrences}),
                  "nearby_dictionary_entries": near}
        if near:
            record["classification"] = "possible-rewrite"
            possible_rewrites.append(record)
        else:
            record["classification"] = "new-candidate"
        additions.append(record)

    dictionary_not_found = []
    rewrite_keys = {match["key"] for candidate in possible_rewrites
                    for match in candidate["nearby_dictionary_entries"]}
    for entry in missing:
        dictionary_not_found.append({
            "file": entry["file"], "key": entry["key"],
            "classification": "not-found-in-bundle",
            "review_note": "May be rewritten, conditionally loaded, or legacy; do not delete automatically.",
            "possible_rewrite_seen": entry["key"] in rewrite_keys,
        })

    placeholder_findings = []
    for entry in entries:
        source_placeholders = _placeholder_tokens(entry["key"])
        target_placeholders = _placeholder_tokens(entry["value"])
        if source_placeholders != target_placeholders:
            placeholder_findings.append({
                "file": entry["file"], "key": entry["key"], "translation": entry["value"],
                "source_tokens": sorted(source_placeholders.elements()),
                "translation_tokens": sorted(target_placeholders.elements()),
                "kind": "placeholder-review",
            })

    occurrences = 0
    candidate_count = 0
    for items in exact.values():
        occurrences += len(items)
        if len(items) > MAX_REPORT_OCCURRENCES:
            raise AuditError("bundle produces too many reportable string occurrences")
    candidate_count = len(additions) + len(dynamic) + len(dictionary_not_found)
    if candidate_count > MAX_REPORT_OCCURRENCES:
        raise AuditError("bundle produces too many report entries")

    return {
        "schema": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "bundle": {"file": path.name, "version": version, "version_source": "user-supplied; not inferred",
                   "bytes": len(data), "sha256": bundle_hash,
                   "expected_sha256_match": bool(expected_sha256)},
        "dictionaries": dict_files,
        "summary": {
            "string_occurrences": sum(1 for item in literals if item["kind"] == "string"),
            "dynamic_template_occurrences": sum(1 for item in literals if item["kind"] == "template"),
            "unique_static_strings": len(bundle_texts),
            "exact_dictionary_matches": sum(1 for value in bundle_texts if value in dict_by_normalized),
            "new_candidates": sum(1 for item in additions if item["classification"] == "new-candidate"),
            "possible_rewrites": len(possible_rewrites), "dictionary_not_found": len(dictionary_not_found),
            "dynamic_templates": len(dynamic), "placeholder_reviews": len(placeholder_findings),
            "terminology_reviews": len(_terminology(entries)),
        },
        "extraction": {"method": "conservative JavaScript literal lexer", "all_literals_retained_for_exact_diff": True,
                       "offset_unit": "Unicode code points",
                       "excluded_occurrences_by_reason": dict(sorted(excluded.items()))},
        "new_candidates": [item for item in additions if item["classification"] == "new-candidate"],
        "possible_rewrites": possible_rewrites,
        "dynamic_templates": sorted(dynamic.values(), key=lambda item: item["template"]),
        "dictionary_not_found": dictionary_not_found,
        "placeholder_reviews": placeholder_findings,
        "terminology_reviews": _terminology(entries),
        "dictionary_integrity": {"problems": dictionary_problems},
        "review_policy": "Read-only report. Never add translations or remove dictionary keys automatically.",
    }


def _write_report(report, output):
    try:
        encoded = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(encoded) > MAX_REPORT_BYTES:
        raise AuditError("report exceeds the 32 MiB limit")
    if output is None:
        sys.stdout.buffer.write(encoded)
        return True
    destination = Path(output)
    try:
        if not destination.parent.exists():
            raise OSError("report directory does not exist")
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, prefix="." + destination.name + ".",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a read-only report from an explicitly selected local bundle")
    parser.add_argument("--bundle", required=True, help="path to a local JavaScript bundle (not downloaded by this tool)")
    parser.add_argument("--version", required=True, help="version label confirmed by the operator")
    parser.add_argument("--dict-dir", default="dicts", help="dictionary directory (default: dicts)")
    parser.add_argument("--output", help="write JSON report here; default is stdout")
    parser.add_argument("--expect-sha256", help="fail closed unless the bundle hash matches this value")
    parser.add_argument("--similarity", type=float, default=0.85, help="review-only rewrite threshold (default: 0.85)")
    args = parser.parse_args(argv)
    if not args.version.strip() or not 0.85 <= args.similarity <= 1:
        parser.error("--version must be non-empty and --similarity must be between 0.85 and 1")
    if args.expect_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.expect_sha256):
        parser.error("--expect-sha256 must contain 64 hexadecimal characters")
    try:
        report = audit(args.bundle, args.version.strip(), args.dict_dir, args.similarity, args.expect_sha256)
        if not _write_report(report, args.output):
            print("[error] report write failed", file=sys.stderr)
            return 1
    except AuditError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    print(f"[audit] bundle={report['bundle']['file']} version={report['bundle']['version']} "
          f"sha256={report['bundle']['sha256']}", file=sys.stderr)
    print(f"[report] new={report['summary']['new_candidates']} "
          f"possible-rewrites={report['summary']['possible_rewrites']} "
          f"not-found={report['summary']['dictionary_not_found']} "
          f"dynamic={report['summary']['dynamic_templates']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
