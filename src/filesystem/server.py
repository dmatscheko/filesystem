import asyncio
import os
import sys
from typing import List, Dict, Any
from pydantic import BaseModel, Field, ValidationError
import difflib
from collections import deque
import fnmatch
from mcp.types import Tool, TextContent
from mcp.server import Server, InitializationOptions, NotificationOptions
import mcp.server.stdio
from typing import Optional
from datetime import datetime
import re


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
        # Simplify ValidationError message, excluding URL
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
        # Apply exclusions only if there are patterns to exclude
        if exclude_patterns:
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in exclude_patterns)]
            files = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in exclude_patterns)]
        rel_root = os.path.relpath(root, real_path) if root != real_path else ""
        # Collect matches
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


class ListAllowedArgs(BaseModel):
    pass


# Server setup
server = Server("secure-filesystem-server")


@server.list_tools()
async def list_tools() -> List[Tool]:
    """List available tools, specifying virtual path inputs."""
    return [
        Tool(
            name="read_file",
            description="Read file contents. Allows to head or tail the file. Limited to allowed dirs.",
            inputSchema=ReadFileArgs.schema(),
        ),
        Tool(
            name="read_multiple_files",
            description="Read the contents of multiple files efficiently. Limited to allowed dirs.",
            inputSchema=ReadMultipleArgs.schema(),
        ),
        Tool(
            name="write_file",
            description="Write or overwrite file with text content. Limited to allowed dirs.",
            inputSchema=WriteFileArgs.schema(),
        ),
        Tool(
            name="edit_file",
            description="Edit file with line-based replacements, returns diff. Limited to allowed dirs.",
            inputSchema=EditFileArgs.schema(),
        ),
        Tool(
            name="create_directory",
            description="Create directory, including nested ones. Limited to allowed dirs.",
            inputSchema=DirArgs.schema(),
        ),
        Tool(
            name="list_directory",
            description="List files/dirs with [FILE]/[DIR] prefixes. Limited to allowed dirs.",
            inputSchema=DirArgs.schema(),
        ),
        Tool(
            name="directory_tree",
            description="Show recursive directory listing. Limited to allowed dirs.",
            inputSchema=DirArgs.schema(),
        ),
        Tool(
            name="search_files",
            description="Search files or directories by file name pattern. Limited to allowed dirs.",
            inputSchema=SearchArgs.schema(),
        ),
        Tool(
            name="move_file",
            description="Move/rename file or directory. Fails if destination exists. Limited to allowed dirs.",
            inputSchema=MoveArgs.schema(),
        ),
        Tool(
            name="get_file_info",
            description="Get file or directory metadata (size, times, permissions). Limited to allowed dirs.",
            inputSchema=DirArgs.schema(),
        ),
        Tool(
            name="list_allowed_directories",
            description="List accessible directories. Use this once before trying to access files.",
            inputSchema=ListAllowedArgs.schema(),
        ),
    ]


@server.call_tool()
async def call_tool(name: str, args: Dict[str, Any] | None) -> List[TextContent]:
    """Execute tools, converting virtual paths to real paths and returning virtual paths in output."""
    if name == "read_file":
        try:
            a = ReadFileArgs(**args)
            real_path = validate_virtual_path(a.virtual_path)
            if a.head is not None and a.tail is not None:
                raise CustomFileSystemError("Specify either head or tail, not both")
            if a.head is not None:
                content = head_file(real_path, a.head)
            elif a.tail is not None:
                content = tail_file(real_path, a.tail)
            else:
                with open(real_path, "r", encoding="utf-8") as f:
                    content = f.read()
            return [TextContent(type="text", text=content)]
        except Exception as e:
            print(f"Debug: read_file error: type={type(e).__name__}, message={str(e)}")
            return [TextContent(type="text", text=get_error_message("Error reading", None if "a" not in locals() else a.virtual_path, e))]

    elif name == "read_multiple_files":
        try:
            a = ReadMultipleArgs(**args)
            results = []
            seen = set()
            for virtual_path in a.virtual_paths:
                if virtual_path not in seen:
                    try:
                        seen.add(virtual_path)
                        real_path = validate_virtual_path(virtual_path)
                        content = open(real_path, "r", encoding="utf-8").read()
                        results.append(f"### {virtual_path}:\n```\n{content}\n```\n")
                    except Exception as e:
                        results.append(f"### {virtual_path}:\n{get_error_message('Error reading', virtual_path, e)}\n")
            return [TextContent(type="text", text="\n".join(results))]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error reading multiple files", None, e))]

    elif name == "write_file":
        try:
            a = WriteFileArgs(**args)
            real_path = validate_virtual_path(a.virtual_path)
            open(real_path, "w", encoding="utf-8").write(a.content)
            return [TextContent(type="text", text=f"Wrote to {a.virtual_path}")]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error writing", None if "a" not in locals() else a.virtual_path, e))]

    elif name == "edit_file":
        try:
            a = EditFileArgs(**args)
            diff = apply_edits(a.virtual_path, [{"oldText": e.oldText, "newText": e.newText} for e in a.edits], a.dryRun)
            return [TextContent(type="text", text=diff)]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error editing", None if "a" not in locals() else a.virtual_path, e))]

    elif name == "create_directory":
        try:
            a = DirArgs(**args)
            real_path = validate_virtual_path(a.virtual_path)
            os.makedirs(real_path, exist_ok=True)
            return [TextContent(type="text", text=f"Created {a.virtual_path}")]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error creating", None if "a" not in locals() else a.virtual_path, e))]

    elif name == "list_directory":
        try:
            a = DirArgs(**args)
            real_path = validate_virtual_path(a.virtual_path)
            entries = os.listdir(real_path)
            listing = [f"[{'DIR' if os.path.isdir(os.path.join(real_path, e)) else 'FILE'}] {e}" for e in entries]
            return [TextContent(type="text", text="\n".join(listing))]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error listing", None if "a" not in locals() else a.virtual_path, e))]

    elif name == "directory_tree":
        try:
            a = DirArgs(**args)
            return [TextContent(type="text", text=list_files_recursive(a.virtual_path))]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error listing", None if "a" not in locals() else a.virtual_path, e))]

    elif name == "search_files":
        try:
            a = SearchArgs(**args)
            return [TextContent(type="text", text=list_files_recursive(a.virtual_path, a.pattern, a.excludePatterns))]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error searching", None if "a" not in locals() else a.virtual_path, e))]

    elif name == "move_file":
        try:
            a = MoveArgs(**args)
            real_source = validate_virtual_path(a.virtual_source)
            real_destination = validate_virtual_path(a.virtual_destination)
            os.rename(real_source, real_destination)
            return [TextContent(type="text", text=f"Moved {a.virtual_source} to {a.virtual_destination}")]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error moving", None if "a" not in locals() else a.virtual_source, e))]

    elif name == "get_file_info":
        try:

            def format_time(timestamp):
                return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

            a = DirArgs(**args)
            real_path = validate_virtual_path(a.virtual_path)
            stats = os.stat(real_path)
            info = {
                "path": a.virtual_path,
                "size": stats.st_size,
                "created": format_time(stats.st_ctime),
                "modified": format_time(stats.st_mtime),
                "accessed": format_time(stats.st_atime),
                "isDirectory": os.path.isdir(real_path),
                "isFile": os.path.isfile(real_path),
                "permissions": oct(stats.st_mode)[-3:],
            }
            return [TextContent(type="text", text="\n".join(f"{k}: {v}" for k, v in info.items()))]
        except Exception as e:
            return [TextContent(type="text", text=get_error_message("Error getting info", None if "a" not in locals() else a.virtual_path, e))]

    elif name == "list_allowed_directories":
        return [TextContent(type="text", text="### Allowed directories:\n" + "\n".join(_virtual_to_real.keys()))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main() -> None:
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
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="filesystem-server",
                server_version="0.2.1",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
