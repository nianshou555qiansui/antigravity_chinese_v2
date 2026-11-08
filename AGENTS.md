# Repository Guidelines

## Project Structure & Module Organization

Sources live at the repository root. `hanhua_v2.py` launches Antigravity and injects the embedded JavaScript engine; `caiji.py` collects untranslated text; `shouhu.py` provides the Windows-only watchdog. `qidong.bat` and `qidong_mac.*` are platform launchers. `dicts/common.json` holds shared terms; `dicts/ui_v2*.json` holds version-specific translations. `docs/screenshot.png` illustrates the README. Consult `CLAUDE.md` for implementation pitfalls.

## Build, Test, and Development Commands

Requires Python 3.10+ and Antigravity; tests need Python 3.11+. Use `python3` on macOS. There is no build step.

- `python -m pip install -r requirements.txt`: install the `websockets` dependency.
- `python hanhua_v2.py`: discover the installation, launch, inject, and briefly watch.
- `python hanhua_v2.py --no-launch --watch 0`: reinject into an existing debug-enabled instance.
- `python caiji.py`: collect visible untranslated text into `pending.json`; add `--misses` to export engine misses.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions/variables, and `UPPER_SNAKE_CASE` constants. Preserve existing JavaScript naming and formatting. No formatter or linter is configured. Keep `qidong.bat` ASCII-only.

Dictionary files use UTF-8, two-space indentation, and English-to-Chinese string mappings. Add complete UI strings, avoid duplicate normalized keys across files, and preserve product names and user content. Put dynamic text in `REGEX_RULES`. Keep Python `normalize_text()` and JavaScript `norm()` aligned; account for both escaping layers when editing embedded JavaScript.

## Testing Guidelines

The offline `unittest` suite runs the full injected engine in Node.js; no coverage threshold is configured. It does not access a running Antigravity instance.

- Run `python -m unittest discover -s tests -v` for JSON integrity, normalized-key uniqueness, full-engine behavior, and ASCII launcher checks (requires Node.js).
- Run `python -m compileall -q hanhua_v2.py caiji.py shouhu.py` for Python syntax checks.
- Manually verify reinjection, page reloads, menus/tooltips, and unchanged code, terminals, inputs, and conversation titles. Record tested platforms.

## Commit & Pull Request Guidelines

History uses concise Chinese, action-oriented subjects describing fixes, dictionary additions, or version adaptations; no Conventional Commits prefix is required. Keep commits focused. PRs should explain the change, link relevant issues, identify Antigravity version/platform, and report validation. Include sanitized screenshots for UI changes. Update `README.md` when CLI options or user-visible behavior change.

## Security & Configuration

Use runtime injection only; never modify installed Antigravity files. Keep CDP local and preserve proxy bypasses. Never commit `pending.json`, `misses.json`, `shouhu.log`, or `shouhu.lock`. Avoid hardcoded account/path data. Default launchers and the watchdog must not close running instances. Only `--force-restart` with explicit confirmation may do so; save work first.
