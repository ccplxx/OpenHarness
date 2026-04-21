"""沙箱文件操作路径边界校验模块。

本模块提供路径安全验证功能，确保沙箱内的文件读写操作不会越界访问
项目目录和额外授权目录之外的文件系统路径，是沙箱安全隔离的关键防线。
"""

from __future__ import annotations

from pathlib import Path


def validate_sandbox_path(
    path: Path,
    cwd: Path,
    extra_allowed: list[str] | None = None,
) -> tuple[bool, str]:
    """校验给定路径是否在沙箱允许的边界范围内。

    首先检查路径是否在项目工作目录（cwd）下，若不在则检查是否在
    额外授权的路径列表中。路径在解析后进行比对，防止符号链接绕过。

    Args:
        path: 待校验的目标路径。
        cwd: 项目工作目录路径，作为主边界。
        extra_allowed: 额外允许的路径列表（来自文件系统配置）。

    Returns:
        tuple[bool, str]: 允许时返回 (True, "")，拒绝时返回 (False, 原因说明)。
    """
    resolved = path.resolve()
    resolved_cwd = cwd.resolve()

    # Primary check: path must be within the project directory
    try:
        resolved.relative_to(resolved_cwd)
        return True, ""
    except ValueError:
        pass

    # Secondary: check extra allowed paths (from filesystem settings)
    for allowed in extra_allowed or []:
        allowed_path = Path(allowed).expanduser().resolve()
        try:
            resolved.relative_to(allowed_path)
            return True, ""
        except ValueError:
            continue

    return False, f"path {resolved} is outside the sandbox boundary ({resolved_cwd})"
