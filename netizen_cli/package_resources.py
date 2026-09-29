"""Read files shipped with this exact installation, or an explicit checkout."""

from pathlib import Path


def resource_path(relative: str) -> Path:
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("package resource path must be relative without parent traversal")
    package = Path(__file__).resolve().parent
    root = package / "resources"
    if root.is_symlink():
        raise ValueError("package resource directory must not be a symlink")
    if not root.exists():
        source = package.parent
        if package.name != "netizen_cli" or not (source / "pyproject.toml").is_file():
            raise FileNotFoundError("installed Netizen resource directory is missing")
        root = source
    resource = root / requested
    if resource.is_symlink() or not resource.is_file() or not resource.resolve().is_relative_to(root):
        raise FileNotFoundError(f"Netizen package resource is missing or unsafe: {relative}")
    return resource
