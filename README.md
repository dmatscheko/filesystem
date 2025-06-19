# Secure MCP Filesystem Server

A secure Model Context Protocol (MCP) server for filesystem operations, allowing safe file and directory manipulation within specified directories.

## Build and Run

```bash
cd filesystem
uvx --from . filesystem /Users/dma/Eigenes/Development/mcp/base
```

or

```bash
uvx --from file:///full/path/to/folder/filesystem filesystem /Users/dma/Eigenes/Development/mcp/base
```

or

```bash
cd filesystem
uv sync --dev --all-extras
uv run filesystem /Users/dma/Eigenes/Development/mcp/base
```

If it does not build, you can try:
```bash
uv cache clean
```
