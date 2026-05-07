This is an experimental collection of tools to alow LM studio have memory, edit files that has git initialized on them and more.
All of the code (even the README) was VibeCoded using Qwen3.6

# Sword AI Memory & File Tools

Give your AI assistant a **memory** so it remembers what you've discussed, and the ability to **edit files** directly — all through a simple MCP connection.

All of the code was VibeCoded using Qwen3.6.

---

## 🧠 memorylite - AI Memory System

Let your AI remember conversations, facts, and preferences across sessions using SQLite. No more starting each chat from scratch.

### What it does
- Remembers everything you've discussed in a structured way
- Searches by keyword, type, or free-text patterns
- Stores memories with semantic keywords for better recall later
- Fast SQLite backend (replaces the old JSON file approach)

### Setup
1. Install dependencies:
   ```bash
   pip install fastmcp
   ```

2. Add to your MCP config (`mcp.json` or equivalent):
```json
{
  "mcpServers": {
    "memorylite": {
      "command": "python",
      "args": ["/full/path/to/memorylite.py"]
    }
  }
}
```

3. That's it — your AI will now remember things between sessions.

**Where memories are stored:** `~/.swordmemory/memory.db` (SQLite database)

---

## 📝 pythonFileTools - File Editing Tool

Let your AI edit files on disk without copying/pasting full content each time. Tell it to fix a typo or update a config file, and it does it directly.

### What it does
- Edit files by specifying which lines/sections to change
- Create new files with specified content
- Read existing files
- Perfect for files that have git initialized on them

### Setup
Add to your MCP config:
```json
{
  "mcpServers": {
    "python_file_tools": {
      "command": "python",
      "args": ["/full/path/to/pythonFileTools.py"]
    }
  }
}
```

---

## Quick Start

1. Clone this repo somewhere on your machine
2. Install dependencies: `pip install fastmcp`
3. Add both tools to your MCP config as shown above
4. Restart your AI frontend (LM Studio, Open WebUI, etc.)

Your AI will now remember things and edit files for you.