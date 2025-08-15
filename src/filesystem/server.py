from collections import deque
from datetime import datetime
import difflib
import fnmatch
from fastmcp import FastMCP
import os
from pydantic import BaseModel, Field, ValidationError
import re
import sys
from typing import Dict, List, Optional


# Custom error class
class CustomFileSystemError(ValueError):
    """Custom error for filesystem operations."""
    pass


# Global mappings for directory access
_allowed_real_dirs: List[str] = []  # Real file system paths
_virtual_to_real: Dict[str, str] = {}  # Virtual path -> Real path
_real_to_virtual: Dict[str, str] = {}  # Real path -> Virtual path


def set_allowed_dirs(real_dirs: List[str]) -> None:
    """Configure allowed real directories and map them to virtual paths (e.g., /data/a)."""
    global _allowed_real_dirs, _virtual_to_real, _real_to_virtual
    _allowed_real_dirs = [os.path.abspath(os.path.expanduser(d)) for d in real_dirs]
    _virtual_to_real = {f"/data/{chr(97 + i)}": real_dir for i, real_dir in enumerate(_allowed_real_dirs)}
    _real_to_virtual = {real_dir: virtual_dir for virtual_dir, real_dir in _virtual_to_real.items()}


def validate_virtual_path(virtual_path: str) -> str:
    """Convert a virtual path to a real path, ensuring it’s within allowed directories."""
    for virtual_dir, real_dir in _virtual_to_real.items():
        if virtual_path.startswith(virtual_dir + "/") or virtual_path == virtual_dir:
            relative = virtual_path[len(virtual_dir) :].lstrip("/")
            real_path = os.path.join(real_dir, relative) if relative else real_dir
            break
    else:
        raise CustomFileSystemError(f"Path must start with a virtual directory (e.g., /data/a): {virtual_path}")

    real_path = os.path.normpath(os.path.abspath(real_path))
    try:
        resolved_real_path = os.path.realpath(real_path)
        if any(resolved_real_path.startswith(d + os.sep) or resolved_real_path == d for d in _allowed_real_dirs):
            return resolved_real_path
        raise PermissionError("Access denied")
    except FileNotFoundError:
        real_parent = os.path.realpath(os.path.dirname(real_path))
        if not os.path.exists(real_parent):
            raise FileNotFoundError("Parent directory not found")
        if any(real_parent.startswith(d + os.sep) or real_parent == d for d in _allowed_real_dirs):
            return real_path
        raise PermissionError("Access denied")


def get_error_message(message, virtual_path: str, e: Exception) -> str:
    """Generate a user-friendly error message using the virtual path."""
    virtual_path = virtual_path or "Unknown path"
    if isinstance(e, FileNotFoundError):
        return f"{message}: No such file or directory: {virtual_path}"
    elif isinstance(e, PermissionError):
        return f"{message}: Permission denied: {virtual_path}"
    elif isinstance(e, IsADirectoryError):
        return f"{message}: Is a directory: {virtual_path}"
    elif isinstance(e, NotADirectoryError):
        return f"{message}: Not a directory: {virtual_path}"
    elif isinstance(e, FileExistsError):
        return f"{message}: File already exists: {virtual_path}"
    elif isinstance(e, CustomFileSystemError):
        return f"{message}: {e}"
    elif isinstance(e, ValidationError):
        errors = e.errors()
        error_details = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in errors)
        return f"{message}: Input validation error: {error_details}"
    elif isinstance(e, ValueError):
        return f"{message}: Invalid value: {virtual_path}"
    else:
        return f"{message}: {virtual_path}"


# File operation helpers
def head_file(real_path: str, lines: int) -> str:
    """Read first N lines of a file."""
    with open(real_path, "r", encoding="utf-8") as f:
        return "".join(line for i, line in enumerate(f) if i < lines)


def tail_file(real_path: str, lines: int) -> str:
    """Read last N lines of a file."""
    with open(real_path, "r", encoding="utf-8") as f:
        return "".join(deque(f, maxlen=lines))


def apply_edits(virtual_path: str, edits: List[Dict[str, str]], dry_run: bool) -> str:
    """Apply text replacements and return a diff."""
    real_path = validate_virtual_path(virtual_path)
    with open(real_path, "r", encoding="utf-8") as f:
        content = new_content = f.read()
    for edit in edits:
        pattern = rf"^{re.escape(edit['oldText'])}(\r?\n|\r|$)"
        new_content = re.sub(pattern, lambda m: edit["newText"] + m.group(1), new_content, flags=re.MULTILINE)
    diff = "".join(difflib.unified_diff(content.splitlines(keepends=True), new_content.splitlines(keepends=True), fromfile=virtual_path, tofile=virtual_path))
    if not dry_run:
        with open(real_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    return diff


def list_files_recursive(virtual_path: str, pattern: Optional[str] = None, exclude_patterns: Optional[List[str]] = None) -> str:
    """List files and directories recursively, optionally filtering by pattern."""
    real_path = validate_virtual_path(virtual_path)
    matches = []
    for root, dirs, files in os.walk(real_path):
        if exclude_patterns:
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in exclude_patterns)]
            files = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in exclude_patterns)]
        rel_root = os.path.relpath(root, real_path) if root != real_path else ""
        for name in dirs + files:
            if pattern is None or fnmatch.fnmatch(name.lower(), pattern.lower()):
                rel_path = os.path.join(rel_root, name).replace(os.sep, "/")
                if os.path.isdir(os.path.join(root, name)):
                    rel_path += "/"
                matches.append(rel_path)
    return "\n".join([f"### Contents of {virtual_path}:"] + sorted(matches))


# Tool argument models
class ReadFileArgs(BaseModel):
    virtual_path: str = Field(..., alias="path")
    head: Optional[int] = None
    tail: Optional[int] = None


class ReadMultipleArgs(BaseModel):
    virtual_paths: List[str] = Field(..., alias="paths")


class WriteFileArgs(BaseModel):
    virtual_path: str = Field(..., alias="path")
    content: str


class EditOp(BaseModel):
    oldText: str = Field(..., description="Line to be replaced")
    newText: str = Field(..., description="Replacement line")


class EditFileArgs(BaseModel):
    virtual_path: str = Field(..., alias="path")
    edits: List[EditOp]
    dryRun: bool = False


class DirArgs(BaseModel):
    virtual_path: str = Field(..., alias="path")


class MoveArgs(BaseModel):
    virtual_source: str = Field(..., alias="source")
    virtual_destination: str = Field(..., alias="destination")


class SearchArgs(BaseModel):
    virtual_path: str = Field(..., alias="path")
    pattern: Optional[str] = None
    excludePatterns: Optional[List[str]] = []


# Server setup
mcp = FastMCP("secure-filesystem-server")


@mcp.tool
def read_file(args: ReadFileArgs) -> str:
    """Read file contents. Allows to head or tail the file. Limited to allowed dirs."""
    try:
        real_path = validate_virtual_path(args.virtual_path)
        if args.head is not None and args.tail is not None:
            raise CustomFileSystemError("Specify either head or tail, not both")
        if args.head is not None:
            return head_file(real_path, args.head)
        elif args.tail is not None:
            return tail_file(real_path, args.tail)
        else:
            with open(real_path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as e:
        return get_error_message("Error reading", args.virtual_path, e)


@mcp.tool
def read_multiple_files(args: ReadMultipleArgs) -> str:
    """Read the contents of multiple files efficiently. Limited to allowed dirs."""
    try:
        results = []
        seen = set()
        for virtual_path in args.virtual_paths:
            if virtual_path not in seen:
                try:
                    seen.add(virtual_path)
                    real_path = validate_virtual_path(virtual_path)
                    content = open(real_path, "r", encoding="utf-8").read()
                    results.append(f"### {virtual_path}:\n```\n{content}\n```\n")
                except Exception as e:
                    results.append(f"### {virtual_path}:\n{get_error_message('Error reading', virtual_path, e)}\n")
        return "\n".join(results)
    except Exception as e:
        return get_error_message("Error reading multiple files", None, e)


@mcp.tool
def write_file(args: WriteFileArgs) -> str:
    """Write or overwrite file with text content. Limited to allowed dirs."""
    try:
        real_path = validate_virtual_path(args.virtual_path)
        with open(real_path, "w", encoding="utf-8") as f:
            f.write(args.content)
        return f"Wrote to {args.virtual_path}"
    except Exception as e:
        return get_error_message("Error writing", args.virtual_path, e)


@mcp.tool
def edit_file(args: EditFileArgs) -> str:
    """Edit file with line-based replacements, returns diff. Limited to allowed dirs."""
    try:
        diff = apply_edits(args.virtual_path, [{"oldText": e.oldText, "newText": e.newText} for e in args.edits], args.dryRun)
        return diff
    except Exception as e:
        return get_error_message("Error editing", args.virtual_path, e)


@mcp.tool
def create_directory(args: DirArgs) -> str:
    """Create directory, including nested ones. Limited to allowed dirs."""
    try:
        real_path = validate_virtual_path(args.virtual_path)
        os.makedirs(real_path, exist_ok=True)
        return f"Created {args.virtual_path}"
    except Exception as e:
        return get_error_message("Error creating", args.virtual_path, e)


@mcp.tool
def list_directory(args: DirArgs) -> str:
    """List files/dirs with [FILE]/[DIR] prefixes. Limited to allowed dirs."""
    try:
        real_path = validate_virtual_path(args.virtual_path)
        entries = os.listdir(real_path)
        listing = [f"[{'DIR' if os.path.isdir(os.path.join(real_path, e)) else 'FILE'}] {e}" for e in entries]
        return "\n".join(listing)
    except Exception as e:
        return get_error_message("Error listing", args.virtual_path, e)


@mcp.tool
def directory_tree(args: DirArgs) -> str:
    """Show recursive directory listing. Limited to allowed dirs."""
    try:
        return list_files_recursive(args.virtual_path)
    except Exception as e:
        return get_error_message("Error listing", args.virtual_path, e)


@mcp.tool
def search_files(args: SearchArgs) -> str:
    """Search files or directories by file name pattern. Limited to allowed dirs."""
    try:
        return list_files_recursive(args.virtual_path, args.pattern, args.excludePatterns)
    except Exception as e:
        return get_error_message("Error searching", args.virtual_path, e)


@mcp.tool
def move_file(args: MoveArgs) -> str:
    """Move/rename file or directory. Fails if destination exists. Limited to allowed dirs."""
    try:
        real_source = validate_virtual_path(args.virtual_source)
        real_destination = validate_virtual_path(args.virtual_destination)
        os.rename(real_source, real_destination)
        return f"Moved {args.virtual_source} to {args.virtual_destination}"
    except Exception as e:
        return get_error_message("Error moving", args.virtual_source, e)


@mcp.tool
def get_file_info(args: DirArgs) -> str:
    """Get file or directory metadata (size, times, permissions). Limited to allowed dirs."""
    try:
        def format_time(timestamp):
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        real_path = validate_virtual_path(args.virtual_path)
        stats = os.stat(real_path)
        info = {
            "path": args.virtual_path,
            "size": stats.st_size,
            "created": format_time(stats.st_ctime),
            "modified": format_time(stats.st_mtime),
            "accessed": format_time(stats.st_atime),
            "isDirectory": os.path.isdir(real_path),
            "isFile": os.path.isfile(real_path),
            "permissions": oct(stats.st_mode)[-3:],
        }
        return "\n".join(f"{k}: {v}" for k, v in info.items())
    except Exception as e:
        return get_error_message("Error getting info", args.virtual_path, e)


@mcp.tool
def list_allowed_directories() -> str:
    """List accessible directories. Use this once before trying to access files."""
    return "### Allowed directories:\n" + "\n".join(_virtual_to_real.keys())


def main() -> None:
    """Run the server with allowed directories from command-line arguments."""
    if len(sys.argv) < 2:
        print("Usage: filesystem <allowed-directory> [additional-directories...]")
        sys.exit(1)
    real_dirs = sys.argv[1:]
    for real_dir in real_dirs:
        if not os.path.isdir(real_dir):
            print(f"Error: {real_dir} is not a directory")
            sys.exit(1)
    set_allowed_dirs(real_dirs)
    virtual_dirs_mapping = "\n".join(f"{v} -> {r}" for v, r in _virtual_to_real.items())
    print(f"MCP Filesystem Server running on stdio\nVirtual to real directory mappings:\n{virtual_dirs_mapping}")
    mcp.run()


if __name__ == "__main__":
    main()
