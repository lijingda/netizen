"""Shared test infrastructure; support modules never import test cases."""


# Discovery rooted at tests/ imports this package as "support" even for a
# single-file pattern. Load the same suite setup as a tests.* module invocation.
if __name__ == "support":
    import tests  # noqa: F401
