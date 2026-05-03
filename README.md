This is an experimental tool to alow LM studio have memory and edit files that has git initialized on them.
All of the code was VibeCoded.

Main files to use are:
SwordMemory.py
pythonFileTools.py

To setup, go to mcp.json file and edit it to:

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
