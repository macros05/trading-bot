---
name: clean-code-review
description: Reviews Python files for common code quality issues — long functions, missing type hints, print() instead of logging, and non-intentional names. Invoke with /clean-code-review.
argument-hint: [file_or_dir ...]
allowed-tools: [Read, Glob, Grep]
---

# Clean Code Review

Review Python source files for four categories of quality issues. Report every finding with a `file:line` reference so the user can jump directly to each location.

## Arguments

The user invoked this with: $ARGUMENTS

- If arguments were provided, review only those files or directories.
- If no arguments were given, review all `**/*.py` files under the project root, excluding `venv/`, `__pycache__/`, and `.git/`.

## Review Checklist

Work through each file. For every issue found, emit a line in this format:

```
[CATEGORY] file.py:NN — <short explanation>
```

### 1. Long Functions (> 30 lines)
- Count non-blank, non-comment lines between `def` and the next `def` / end of class / end of file.
- Flag any function or method whose body exceeds 30 lines.
- Note the actual line count: `NN lines — consider splitting`.

### 2. Missing Type Hints
Flag any `def` that is missing **any** annotation (parameters or return type):
- Missing parameter annotation: `def foo(x, y)` → flag `x`, `y`
- Missing return annotation: `def foo(x: int)` → flag missing `->`
- `self` and `cls` are exempt.
- `**kwargs` / `*args` are exempt only if clearly a pass-through.

### 3. `print()` Instead of `logging`
- Flag every bare `print(` call found in non-test files.
- Test files (`tests/`, `test_*.py`, `*_test.py`) are exempt.
- Suggest the appropriate `logger.debug` / `logger.info` / `logger.warning` / `logger.error` level based on context.

### 4. Names Without Intention
Flag identifiers that do not communicate purpose:
- Single-letter variables outside of loop counters (`for i in range(...)` is fine) or comprehensions.
- Abbreviations that lose meaning: `tmp`, `val`, `res`, `ret`, `data2`, `x1`, `flag`, `ok`, `d`, `n` when used as long-lived variables.
- Functions named `do_stuff`, `handle`, `process`, `run` with no qualifier.
- Boolean variables not prefixed with `is_`, `has_`, `can_`, `should_`.

## Output Format

Group findings by file. Use this structure:

```
## path/to/file.py

[LONG_FN]   path/to/file.py:12 — `calculate` is 45 lines — consider splitting
[TYPE_HINT] path/to/file.py:12 — `calculate(price, qty)` missing return type
[PRINT]     path/to/file.py:34 — print() call — use logger.debug or logger.info
[NAME]      path/to/file.py:67 — variable `res` — use a name that states what it holds
```

After all files, print a **Summary** table:

```
## Summary
| Category    | Issues |
|-------------|--------|
| LONG_FN     |      2 |
| TYPE_HINT   |      5 |
| PRINT       |      1 |
| NAME        |      3 |
| **Total**   |     11 |
```

If no issues are found in a file, skip that file entirely. If zero issues across all files, print:

```
No issues found. Code looks clean.
```

## Scope Limits

- Only report issues in files under the project root that are **not** inside `venv/`.
- Do not modify any file — this skill is read-only.
- Do not report the same issue twice (e.g., if a function is both long and missing type hints, emit both categories on separate lines, not the same category twice).
