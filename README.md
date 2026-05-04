This is an experimental collection of tools to alow LM studio have memory, edit files that has git initialized on them and more.
All of the code was VibeCoded.

Main files to use are:
SwordMemory.py
pythonFileTools.py

To setup, go to mcp.json file and edit it to:
```
{
  "mcpServers": {
    "python_file_tools": {
      "command": "python",
      "args": [
        "/path/to/pythonFileTools.py"
      ]
    },
    "SwordMemory": {
      "command": "python",
      "args": [
        "/path/to/SwordMemory.py"
      ]
    }
  }
}
```
