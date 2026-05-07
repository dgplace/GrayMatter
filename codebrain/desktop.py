#!./.venv/bin/python
"""
@file desktop.py
@brief Executable launcher for the CodeBrain desktop package.

Runs the desktop package entrypoint in-process so `./desktop.py` behaves like
`python -m desktop` while staying inside the same Python interpreter.
"""

from desktop.__main__ import main as run_desktop


def main() -> None:
    """@brief Invoke the desktop package main function directly."""
    run_desktop()


if __name__ == "__main__":
    main()
