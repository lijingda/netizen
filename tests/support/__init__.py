"""Shared test infrastructure; support modules never import test cases."""

from importlib.util import find_spec
import unittest

# Discovery rooted at tests/ imports this package as "support" even for a
# single-file pattern. Load the same suite setup as a tests.* module invocation.
if __name__ == "support":
    import tests  # noqa: F401


has_claude_sdk = find_spec("claude_agent_sdk") is not None
requires_claude_sdk = unittest.skipUnless(has_claude_sdk, "requires the Claude SDK")
