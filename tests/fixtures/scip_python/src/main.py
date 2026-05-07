"""
@file main.py
@brief Fixture call site for Python SCIP resolver tests.
"""

from helpers import Greeter


def run() -> str:
    """@brief Exercise constructor and method references for the fixture."""
    greeter = Greeter()
    return greeter.greet()
