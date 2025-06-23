# MCP Filesystem Server

A relatively secure Model Context Protocol (MCP) server for filesystem operations, allowing safe file and directory manipulation within specified directories.

## Build and Run

```bash
cd filesystem
uvx --from . filesystem /development/base_directory
```

or

```bash
uvx --from file:///full/path/to/folder/filesystem filesystem /development/base_directory
```

or

```bash
cd filesystem
uv sync --dev --all-extras
uv run filesystem /development/base_directory
```

If you changed the code, run the following to rebuild everything:
```bash
uv cache clean
```
