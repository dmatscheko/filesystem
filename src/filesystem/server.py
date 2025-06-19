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

# Module-level storage for allowed directories
_allowed_dirs: List[str] = []


def set_allowed_dirs(dirs: List[str]) -> None:
    """Set the global list of allowed directories."""
    global _allowed_dirs
    _allowed_dirs = dirs


def validate_path(requested_path: str) -> str:
    """Validate that a path is within allowed directories, handling symlinks and new files."""
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


async def build_directory_tree(path: str) -> List[Dict[str, Any]]:
    """Recursively build a directory tree structure."""
    entries = await asyncio.to_thread(lambda: os.listdir(path))
    tree = []
    for entry in entries:
        full_path = os.path.join(path, entry)
        node = {"name": entry, "type": "file" if os.path.isfile(full_path) else "directory"}
        if node["type"] == "directory":
            node["children"] = await build_directory_tree(full_path)
        tree.append(node)
    return tree


async def search_files(root_path: str, pattern: str, exclude_patterns: List[str]) -> List[str]:
    """Recursively search for files matching a pattern."""
    results = []
    for root, dirs, files in os.walk(root_path):
        dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in exclude_patterns)]
        files = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in exclude_patterns)]
        for name in dirs + files:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                results.append(os.path.join(root, name))
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
            description="Get a recursive tree view of files and directories as a JSON structure. Each entry includes 'name', 'type' (file/directory), and 'children' for directories. Files have no children array, while directories always have a children array (which may be empty). The output is formatted with 2-space indentation for readability. Only works within allowed directories.",
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
            for file_path in args.paths:
                try:
                    valid_path = validate_path(file_path)
                    content = await asyncio.to_thread(lambda: open(valid_path, "r", encoding="utf-8").read())
                    results.append(f"{file_path}:\n{content}\n")
                except Exception as e:
                    results.append(f"{file_path}: Error - {str(e)}")
            return [types.TextContent(type="text", text="\n---\n".join(results))]

        elif name == "write_file":
            args = WriteFileArgs(**arguments)
            valid_path = validate_path(args.path)
            await asyncio.to_thread(lambda: open(valid_path, "w", encoding="utf-8").write(args.content))
            return [types.TextContent(type="text", text=f"Successfully wrote to {args.path}")]

        elif name == "edit_file":
            args = EditFileArgs(**arguments)
            valid_path = validate_path(args.path)
            edits = [{"oldText": e.oldText, "newText": e.newText} for e in args.edits]
            diff_str = await asyncio.to_thread(apply_file_edits_sync, valid_path, edits, args.dryRun)
            return [types.TextContent(type="text", text=diff_str)]

        elif name == "create_directory":
            args = CreateDirectoryArgs(**arguments)
            valid_path = validate_path(args.path)
            await asyncio.to_thread(lambda: os.makedirs(valid_path, exist_ok=True))
            return [types.TextContent(type="text", text=f"Successfully created directory {args.path}")]

        elif name == "list_directory":
            args = ListDirectoryArgs(**arguments)
            valid_path = validate_path(args.path)
            entries = await asyncio.to_thread(lambda: os.listdir(valid_path))
            formatted = [f"[{'DIR' if os.path.isdir(os.path.join(valid_path, e)) else 'FILE'}] {e}" for e in entries]
            return [types.TextContent(type="text", text="\n".join(formatted))]

        elif name == "directory_tree":
            args = DirectoryTreeArgs(**arguments)
            valid_path = validate_path(args.path)
            tree = await build_directory_tree(valid_path)
            return [types.TextContent(type="text", text=json.dumps(tree, indent=2))]

        elif name == "move_file":
            args = MoveFileArgs(**arguments)
            valid_source = validate_path(args.source)
            valid_destination = validate_path(args.destination)
            await asyncio.to_thread(lambda: os.rename(valid_source, valid_destination))
            return [types.TextContent(type="text", text=f"Successfully moved {args.source} to {args.destination}")]

        elif name == "search_files":
            args = SearchFilesArgs(**arguments)
            valid_path = validate_path(args.path)
            results = await search_files(valid_path, args.pattern, args.excludePatterns)
            return [types.TextContent(type="text", text="\n".join(results) if results else "No matches found")]

        elif name == "get_file_info":
            args = GetFileInfoArgs(**arguments)
            valid_path = validate_path(args.path)
            stats = await asyncio.to_thread(lambda: os.stat(valid_path))
            info = {
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
            return [types.TextContent(type="text", text="Allowed directories:\n" + "\n".join(_allowed_dirs))]

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
    print(f"Allowed directories: {allowed_dirs}")

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
