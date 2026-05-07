"""
@file helpers.py
@brief Fixture declarations for Python SCIP resolver tests.
"""


class Greeter:
    """@brief Simple greeter used by the resolver fixture."""

    def greet(self) -> str:
        """@brief Return a deterministic greeting for the fixture."""
        return "hi"
