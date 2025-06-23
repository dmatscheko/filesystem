# MCP Filesystem Server

A relatively secure, asynchronous [MCP](https://modelcontextprotocol.io/introduction) server allowing file and directory manipulation within specified directories.
Maps real directories to virtual paths (e.g., `/data/a`) to shorten paths and obscure real filesystem structure from clients, such as LLMs.

## File Operations:

* **read_file:** Read file contents. Allows to head or tail the file. Limited to allowed dirs.
* **read_multiple_files:** Read the contents of multiple files efficiently. Limited to allowed dirs.
* **write_file:** Write or overwrite file with text content. Limited to allowed dirs.
* **edit_file:** Edit file with line-based replacements, returns diff. Limited to allowed dirs.
* **create_directory:** Create directory, including nested ones. Limited to allowed dirs.
* **list_directory:** List files/dirs with [FILE]/[DIR] prefixes. Limited to allowed dirs.
* **directory_tree:** Show recursive directory listing. Limited to allowed dirs.
* **move_file:** Move/rename file or directory. Fails if destination exists. Limited to allowed dirs.
* **search_files:** Search files or directories by file name pattern. Limited to allowed dirs.
* **get_file_info:** Get file or directory metadata (size, times, permissions). Limited to allowed dirs.
* **list_allowed_directories:** List accessible directories. Use this once before trying to access files.
* **edit_file:** Edit file with line-based replacements, returns diff. Limited to allowed dirs.

## Jan.ai Configuration:

### MCP Server Setting

Add the following to your Jan.ai MCP server settings, replacing `/development/mcp/base` with your desired directory. You can also replace `git+https://github.com/dmatscheko/filesystem` with something like `file:///wherever/the/repository/is/filesystem` if you want to download the repository and use it offline:
```json
{
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/dmatscheko/filesystem",
    "filesystem",
    "/development/mcp/base"
  ],
  "env": {},
  "active": true
}
```

### Example Assistant

#### Name
Jan-Auditor

#### Description
Jan-Auditor is an expert source code auditor that reasons through complex tasks and uses tools to perform them on the user’s behalf.

#### Instructions
You have access to tools to assist in answering the user’s question. Use only one tool per message, and the tool's result will be provided in the user’s next response. Complete tasks by using tools step by step, with each step informed by the previous tool's outcome.

Tool Usage Rules:
1. Provide exact values as arguments for tools, not variable names.
2. You may use many tool steps to complete a task.
3. Avoid repeating a tool call with identical parameters to prevent loops.
4. Do not read more than 6 files at once with read_multiple_files.
5. If errors occur, recover by using available relevant information and continue.
6. Start with the list_allowed_directories and then a directory_tree command.

### Example Prompt
```
Find the vulnerabilites in the code. Prioritize security-critical and core logic files, such as those handling communication, authentication, or sensitive data, and process them first. Always quote the most important parts of the vulnerable code but not more than a few lines. Avoid repeating information but ALWAYS highlight if new findings update or contradict previous knowledge (e.g., discovering security measures that mitigate previously identified vulnerabilities). Iteratively expand the vulnerability audit scope and always continue without asking until you audited all files.
/no_think
```
Note: This prompt works e.g. with the model `devstral-small-2505` or more advanced models.


## Manual Build and Run

```bash
git clone https://github.com/dmatscheko/filesystem.git
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


## Example MCP protocol:

1. 
```json
{"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{"sampling":{},"roots":{"listChanged":true}},"clientInfo":{"name":"some-client","version":"0.14.3"}},"jsonrpc":"2.0","id":0}
```
2. 
```json
{"method":"notifications/initialized","jsonrpc":"2.0"}
```
3. 
```json
{"method":"tools/list","params":{"_meta":{"progressToken":1}},"jsonrpc":"2.0","id":1}
```
4. 
```json
{"method":"tools/call","params":{"name":"directory_tree","arguments":{"path":"/data/a"},"_meta":{"progressToken":5}},"jsonrpc":"2.0","id":5}
```
