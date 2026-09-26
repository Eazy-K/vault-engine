#!/bin/sh
# Shared by pre-commit and commit-msg: picks a Python interpreter that actually
# runs, not just one whose name resolves on PATH. On Windows, `command -v python`
# (and `python3`) can succeed against the Microsoft Store's stub executable,
# which prints a message and exits without importing anything -- so every
# candidate is actually run (`-c "import sys"`) before it is trusted. Sets
# PYTHON_CMD and PYTHON_ARGS on success, or prints an error and exits 1.
_try_python() {
    cmd=$1
    shift
    command -v "$cmd" >/dev/null 2>&1 && "$cmd" "$@" -c "import sys" >/dev/null 2>&1
}

if _try_python py -3; then
    PYTHON_CMD=py
    PYTHON_ARGS="-3"
elif _try_python python3; then
    PYTHON_CMD=python3
    PYTHON_ARGS=""
elif _try_python python; then
    PYTHON_CMD=python
    PYTHON_ARGS=""
else
    echo "guard: python not found (tried py -3, python3, python)" >&2
    exit 1
fi
