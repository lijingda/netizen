"""Keep Git fixtures independent of the repository that launched a test gate."""

import os


# git rev-parse --local-env-vars, plus repository discovery and receive-pack selectors.
_REPOSITORY_ENV = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX", "GIT_QUARANTINE_PATH",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_SHALLOW_FILE",
    "GIT_GRAFT_FILE", "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS",
    "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
}


def clear_git_repository_environment() -> None:
    for key in tuple(os.environ):
        if key in _REPOSITORY_ENV or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del os.environ[key]
