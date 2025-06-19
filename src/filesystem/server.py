import asyncio
import os
import sys
from typing import List, Dict, Any
from pydantic import BaseModel
import difflib
from collections import deque
import json
import fnmatch
import mcp.types as types
from mcp.server import Server, InitializationOptions, NotificationOptions
import mcp.server.stdio

# Module-level storage for allowed directories and their virtual mappings
_allowed_dirs: List[str] = []
_virtual_to_real: Dict[str, str] = {}
_real_to_virtual: Dict[str, str] = {}


def set_allowed_dirs(dirs: List[str]) -> None:
    """Set the global list of allowed directories with virtual mappings."""
    global _allowed_dirs, _virtual_to_real, _real_to_virtual
    _allowed_dirs = [os.path.abspath(os.path.expanduser(d)) for d in dirs]

    # Create virtual path mappings (/data/a, /data/b, etc.)
    _virtual_to_real = {f"/data/{chr(97 + i)}": real_dir for i, real_dir in enumerate(_allowed_dirs)}
    _real_to_virtual = {real_dir: virtual_dir for virtual_dir, real_dir in _virtual_to_real.items()}


def validate_path(requested_path: str) -> str:
    """Validate that a path is within allowed directories, handling virtual paths, symlinks, and new files."""
    # Check if the path starts with a virtual directory
    for virtual_dir, real_dir in _virtual_to_real.items():
        if requested_path.startswith(virtual_dir + "/") or requested_path == virtual_dir:
            # Replace virtual path prefix with real path
            relative_path = requested_path[len(virtual_dir) :].lstrip("/")
            expanded_path = os.path.join(real_dir, relative_path) if relative_path else real_dir
            break
    else:
        # If not a virtual path, treat as a relative path under first allowed dir (for compatibility)
        expanded_path = os.path.expanduser(requested_path)

    absolute_path = os.path.abspath(expanded_path)
    normalized_path = os.path.normpath(absolute_path)

    try:
        real_path = os.path.realpath(normalized_path)
        if any(real_path.startswith(allowed_dir + os.sep) or real_path == allowed_dir for allowed_dir in _allowed_dirs):
            return real_path
        raise PermissionError(f"Access denied: {real_path} not in allowed directories")
    except FileNotFoundError:
        parent_dir = os.path.dirname(normalized_path)
        real_parent = os.path.realpath(parent_dir)
        if not os.path.exists(real_parent):
            raise FileNotFoundError(f"Parent directory does not exist: {real_parent}")
        if any(real_parent.startswith(allowed_dir + os.sep) or real_parent == allowed_dir for allowed_dir in _allowed_dirs):
            return normalized_path
        raise PermissionError(f"Access denied: parent directory {real_parent} not in allowed directories")


def convert_to_virtual_path(real_path: str) -> str:
    """Convert a real path to its virtual equivalent."""
    for real_dir, virtual_dir in _real_to_virtual.items():
        if real_path.startswith(real_dir + os.sep) or real_path == real_dir:
            relative_path = real_path[len(real_dir) :].lstrip(os.sep)
            return os.path.join(virtual_dir, relative_path) if relative_path else virtual_dir
    return real_path  # Fallback, should not happen with validated paths


# Helper functions
def head_file_sync(path: str, num_lines: int) -> str:
    """Read the first N lines of a file."""
    with open(path, "r", encoding="utf-8") as f:
        return "".join(line for _, line in zip(range(num_lines), f))


def tail_file_sync(path: str, num_lines: int) -> str:
    """Read the last N lines of a file efficiently."""
    with open(path, "r", encoding="utf-8") as f:
        lines = deque(f, maxlen=num_lines)
    return "".join(lines)


def apply_file_edits_sync(path: str, edits: List[Dict[str, str]], dry_run: bool) -> str:
    """Apply edits to a file and return a unified diff."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    new_content = content
    for edit in edits:
        new_content = new_content.replace(edit["oldText"], edit["newText"])
    diff = difflib.unified_diff(content.splitlines(keepends=True), new_content.splitlines(keepends=True), fromfile=path, tofile=path)
    diff_str = "".join(diff)
    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    return diff_str


async def build_directory_tree(path: str) -> str:
    """Build a newline-separated list of full virtual paths to all files, including empty directories with a trailing slash."""
    paths = []
    for root, dirs, files in await asyncio.to_thread(os.walk, path):
        virtual_root = convert_to_virtual_path(root)
        # Add all files
        for file in files:
            full_path = os.path.join(root, file)
            virtual_path = convert_to_virtual_path(full_path)
            paths.append(virtual_path)
        # Add directories (empty ones get a trailing slash)
        for dir in dirs:
            full_dir_path = os.path.join(root, dir)
            virtual_dir_path = convert_to_virtual_path(full_dir_path)
            dir_contents = os.listdir(full_dir_path)
            if not dir_contents:  # Empty directory
                paths.append(virtual_dir_path + "/")
    # Add the root directory itself if it's empty
    root_contents = await asyncio.to_thread(os.listdir, path)
    if not root_contents:
        virtual_root = convert_to_virtual_path(path)
        paths.append(virtual_root + "/")
    return "\n".join(sorted(paths))


async def search_files(root_path: str, pattern: str, exclude_patterns: List[str]) -> List[str]:
    """Recursively search for files matching a pattern, returning virtual paths."""
    results = []
    for root, dirs, files in os.walk(root_path):
        dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in exclude_patterns)]
        files = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in exclude_patterns)]
        for name in dirs + files:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                real_path = os.path.join(root, name)
                virtual_path = convert_to_virtual_path(real_path)
                results.append(virtual_path)
    return results


# Pydantic models for tool arguments
class ReadFileArgs(BaseModel):
    path: str
    tail: int = None
    head: int = None


class ReadMultipleFilesArgs(BaseModel):
    paths: List[str]


class WriteFileArgs(BaseModel):
    path: str
    content: str


class EditOperation(BaseModel):
    oldText: str
    newText: str


class EditFileArgs(BaseModel):
    path: str
    edits: List[EditOperation]
    dryRun: bool = False


class CreateDirectoryArgs(BaseModel):
    path: str


class ListDirectoryArgs(BaseModel):
    path: str


class DirectoryTreeArgs(BaseModel):
    path: str


class MoveFileArgs(BaseModel):
    source: str
    destination: str


class SearchFilesArgs(BaseModel):
    path: str
    pattern: str
    excludePatterns: List[str] = []


class GetFileInfoArgs(BaseModel):
    path: str


class ListAllowedDirectoriesArgs(BaseModel):
    pass


# Server instance
server = Server("secure-filesystem-server")


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List all available tools with their input schemas."""
    return [
        types.Tool(
            name="read_file",
            description="Read the complete contents of a file from the file system. Handles various text encodings and provides detailed error messages if the file cannot be read. Use this tool when you need to examine the contents of a single file. Only works within allowed directories.",
            inputSchema=ReadFileArgs.schema(),
        ),
        types.Tool(
            name="read_multiple_files",
            description="Read the contents of multiple files simultaneously. This is more efficient than reading files one by one when you need to analyze or compare multiple files. Each file's content is returned with its path as a reference. Failed reads for individual files won't stop the entire operation. Only works within allowed directories.",
            inputSchema=ReadMultipleFilesArgs.schema(),
        ),
        types.Tool(
            name="write_file",
            description="Create a new file or completely overwrite an existing file with new content. Use with caution as it will overwrite existing files without warning. Handles text content with proper encoding. Only works within allowed directories.",
            inputSchema=WriteFileArgs.schema(),
        ),
        types.Tool(
            name="edit_file",
            description="Make line-based edits to a text file. Each edit replaces exact line sequences with new content. Returns a git-style diff showing the changes made. Only works within allowed directories.",
            inputSchema=EditFileArgs.schema(),
        ),
        types.Tool(
            name="create_directory",
            description="Create a new directory or ensure a directory exists. Can create multiple nested directories in one operation. If the directory already exists, this operation will succeed silently. Perfect for setting up directory structures for projects or ensuring required paths exist. Only works within allowed directories.",
            inputSchema=CreateDirectoryArgs.schema(),
        ),
        types.Tool(
            name="list_directory",
            description="Get a detailed listing of all files and directories in a specified path. Results clearly distinguish between files and directories with [FILE] and [DIR] prefixes. This tool is essential for understanding directory structure and finding specific files within a directory. Only works within allowed directories.",
            inputSchema=ListDirectoryArgs.schema(),
        ),
        types.Tool(
            name="directory_tree",
            description="Get a newline-separated list of full paths to all files and empty directories (with trailing slash) in a directory. Paths are sorted alphabetically. Only works within allowed directories.",
            inputSchema=DirectoryTreeArgs.schema(),
        ),
        types.Tool(
            name="move_file",
            description="Move or rename files and directories. Can move files between directories and rename them in a single operation. If the destination exists, the operation will fail. Works across different directories and can be used for simple renaming within the same directory. Both source and destination must be within allowed directories.",
            inputSchema=MoveFileArgs.schema(),
        ),
        types.Tool(
            name="search_files",
            description="Recursively search for files and directories matching a pattern. Searches through all subdirectories from the starting path. The search is case-insensitive and matches partial names. Returns full paths to all matching items. Great for finding files when you don't know their exact location. Only searches within allowed directories.",
            inputSchema=SearchFilesArgs.schema(),
        ),
        types.Tool(
            name="get_file_info",
            description="Retrieve detailed metadata about a file or directory. Returns comprehensive information including size, creation time, last modified time, permissions, and type. This tool is perfect for understanding file characteristics without reading the actual content. Only works within allowed directories.",
            inputSchema=GetFileInfoArgs.schema(),
        ),
        types.Tool(
            name="list_allowed_directories",
            description="Returns the list of directories that this server is allowed to access. Use this to understand which directories are available before trying to access files.",
            inputSchema=ListAllowedDirectoriesArgs.schema(),
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any] | None) -> List[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool execution with error catching."""
    try:
        if name == "read_file":
            args = ReadFileArgs(**arguments)
            valid_path = validate_path(args.path)
            if args.head is not None and args.tail is not None:
                raise ValueError("Cannot specify both head and tail parameters simultaneously")
            if args.tail is not None:
                content = await asyncio.to_thread(tail_file_sync, valid_path, args.tail)
            elif args.head is not None:
                content = await asyncio.to_thread(head_file_sync, valid_path, args.head)
            else:
                content = await asyncio.to_thread(lambda: open(valid_path, "r", encoding="utf-8").read())
            return [types.TextContent(type="text", text=content)]

        elif name == "read_multiple_files":
            args = ReadMultipleFilesArgs(**arguments)
            results = []
            processed_paths = set()
            for file_path in args.paths:
                if file_path not in processed_paths:
                    try:
                        valid_path = validate_path(file_path)
                        content = await asyncio.to_thread(lambda: open(valid_path, "r", encoding="utf-8").read())
                        virtual_path = convert_to_virtual_path(valid_path)
                        results.append(f"### {virtual_path}:\n```\n{content}\n```\n")
                        processed_paths.add(file_path)
                    except Exception as e:
                        results.append(f"###{file_path}:\nError - {str(e)}\n")
                        processed_paths.add(file_path)
            return [types.TextContent(type="text", text="\n".join(results))]

        elif name == "write_file":
            args = WriteFileArgs(**arguments)
            valid_path = validate_path(args.path)
            virtual_path = convert_to_virtual_path(valid_path)
            await asyncio.to_thread(lambda: open(valid_path, "w", encoding="utf-8").write(args.content))
            return [types.TextContent(type="text", text=f"Successfully wrote to {virtual_path}")]

        elif name == "edit_file":
            args = EditFileArgs(**arguments)
            valid_path = validate_path(args.path)
            virtual_path = convert_to_virtual_path(valid_path)
            edits = [{"oldText": e.oldText, "newText": e.newText} for e in args.edits]
            diff_str = await asyncio.to_thread(apply_file_edits_sync, valid_path, edits, args.dryRun)
            return [types.TextContent(type="text", text=diff_str.replace(valid_path, virtual_path))]

        elif name == "create_directory":
            args = CreateDirectoryArgs(**arguments)
            valid_path = validate_path(args.path)
            virtual_path = convert_to_virtual_path(valid_path)
            await asyncio.to_thread(lambda: os.makedirs(valid_path, exist_ok=True))
            return [types.TextContent(type="text", text=f"Successfully created directory {virtual_path}")]

        elif name == "list_directory":
            args = ListDirectoryArgs(**arguments)
            valid_path = validate_path(args.path)
            virtual_path = convert_to_virtual_path(valid_path)
            entries = await asyncio.to_thread(lambda: os.listdir(valid_path))
            formatted = [f"[{'DIR' if os.path.isdir(os.path.join(valid_path, e)) else 'FILE'}] {e}" for e in entries]
            return [types.TextContent(type="text", text="\n".join(formatted))]

        elif name == "directory_tree":
            args = DirectoryTreeArgs(**arguments)
            valid_path = validate_path(args.path)
            paths = await build_directory_tree(valid_path)
            return [types.TextContent(type="text", text=paths)]

        elif name == "move_file":
            args = MoveFileArgs(**arguments)
            valid_source = validate_path(args.source)
            valid_destination = validate_path(args.destination)
            virtual_source = convert_to_virtual_path(valid_source)
            virtual_destination = convert_to_virtual_path(valid_destination)
            await asyncio.to_thread(lambda: os.rename(valid_source, valid_destination))
            return [types.TextContent(type="text", text=f"Successfully moved {virtual_source} to {virtual_destination}")]

        elif name == "search_files":
            args = SearchFilesArgs(**arguments)
            valid_path = validate_path(args.path)
            results = await search_files(valid_path, args.pattern, args.excludePatterns)
            return [types.TextContent(type="text", text="\n".join(results) if results else "No matches found")]

        elif name == "get_file_info":
            args = GetFileInfoArgs(**arguments)
            valid_path = validate_path(args.path)
            virtual_path = convert_to_virtual_path(valid_path)
            stats = await asyncio.to_thread(lambda: os.stat(valid_path))
            info = {
                "path": virtual_path,
                "size": stats.st_size,
                "created": stats.st_ctime,
                "modified": stats.st_mtime,
                "accessed": stats.st_atime,
                "isDirectory": os.path.isdir(valid_path),
                "isFile": os.path.isfile(valid_path),
                "permissions": oct(stats.st_mode)[-3:],
            }
            return [types.TextContent(type="text", text="\n".join(f"{k}: {v}" for k, v in info.items()))]

        elif name == "list_allowed_directories":
            return [types.TextContent(type="text", text="Allowed directories:\n" + "\n".join(_virtual_to_real.keys()))]

        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]


async def main() -> None:
    """Start the filesystem server with allowed directories from command-line arguments."""
    if len(sys.argv) < 2:
        print("Usage: filesystem <allowed-directory> [additional-directories...]")
        sys.exit(1)

    allowed_dirs = [os.path.abspath(os.path.expanduser(dir)) for dir in sys.argv[1:]]
    for dir in allowed_dirs:
        if not os.path.isdir(dir):
            print(f"Error: {dir} is not a directory or does not exist")
            sys.exit(1)

    set_allowed_dirs(allowed_dirs)
    print("Secure MCP Filesystem Server running on stdio")
    virtual_dirs_mapping = "\n".join(f"{v} -> {r}" for v, r in _virtual_to_real.items())
    print("Secure MCP Filesystem Server running on stdio")
    print(f"Virtual to real directory mappings:\n{virtual_dirs_mapping}")

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="secure-filesystem-server",
                server_version="0.2.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
