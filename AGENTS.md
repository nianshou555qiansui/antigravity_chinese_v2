# Repository Guidelines

## Project Structure & Module Organization

Root sources include `hanhua_v2.py` (engine), `cdp_*.py` (CDP), `diagnostics.py` (read-only checks), `guard_runtime.py` (watchdog states), `caiji.py` (statistics), `privacy_policy.py` (DOM rules), and `audit_bundle.py` (local bundle audit without CDP/network). `shouhu.py` is the Windows watchdog. Dictionaries are in `dicts/`, tests in `tests/`, assets in `docs/`. See `CLAUDE.md`.

## Build, Test, and Development Commands

Requires Python 3.10+ and Antigravity; tests need Python 3.11+. Use `python3` on macOS. No build.

- `python -m pip install -r requirements.txt`: install the `websockets` dependency.
- `python hanhua_v2.py`: discover the installation, launch, inject, and briefly watch.
- `python hanhua_v2.py --no-launch --watch 0`: reinject into an existing debug-enabled instance.
- `python hanhua_v2.py --doctor`: check dictionaries, windows, and engine snapshots without changing the app.
- `python caiji.py`: export source counts only; `--include-ui-text` explicitly exports audited static labels; `--misses` exports numeric engine misses.
- `python audit_bundle.py --bundle path/to/main.js --version 2.29.0 --output audit-report.json`: hash a local bundle and produce review-only candidates; never downloads or edits dictionaries.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions/variables, and `UPPER_SNAKE_CASE` constants. Preserve existing JavaScript naming and formatting. No formatter or linter is configured. Keep `qidong.bat` ASCII-only.

Dictionaries use UTF-8, two-space indentation, and English-to-Chinese string mappings. Add complete UI strings, avoid duplicate normalized keys, and preserve product names and user content. Put dynamic text in `REGEX_RULES`. Keep Python `normalize_text()` and JavaScript `norm()` aligned; account for both escaping layers in embedded JavaScript.

## Testing Guidelines

Offline `unittest` runs the full engine in Node.js without Antigravity. No coverage threshold.

- Run `python -m unittest discover -s tests -v` for dictionary, engine, privacy, and launcher checks (requires Node.js).
- Run `python -m compileall -q .` for Python syntax checks.
- Manually verify windows/reloads, menus, tooltips, and protected pages. Record platforms.

## Commit & Pull Request Guidelines

History uses concise Chinese, action-oriented subjects; no Conventional Commits prefix is required. Keep commits focused. PRs should explain changes, link issues, identify Antigravity version/platform, and report validation. Include sanitized screenshots for UI changes. Update `README.md` when CLI options or user-visible behavior change.

## Security & Configuration

Use runtime injection only; never modify installed Antigravity files. Keep CDP local and preserve proxy bypasses. Apply shared privacy checks before reading text or attributes; never export legacy raw misses. Never commit collection reports, temporary files, `shouhu.log`, or `shouhu.lock`. Default launchers and watchdogs must not close applications. Only confirmed `--force-restart` may do so; save work first.
