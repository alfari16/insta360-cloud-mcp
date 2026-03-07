# insta360-cloud-mcp

[![PyPI](https://img.shields.io/pypi/v/insta360-cloud-mcp)](https://pypi.org/project/insta360-cloud-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

MCP server for Insta360 cloud storage — browse, download, and manage your 360 media from any MCP client.

## Features

- **list_media** — Fetch all media from your Insta360 cloud (auto-paginated)
- **get_download_urls** — Get download URLs for a specific media item
- **download_media** — Download all files for a specific media item
- **download_all** — Download your entire Insta360 cloud library
- **delete_media** — Move media items to the recycle bin

## Prerequisites

- An [Insta360](https://www.insta360.com/) account with cloud storage
- [uv](https://docs.astral.sh/uv/) installed

## Quick Start (uvx)

Add to your MCP client config (e.g. `claude_desktop_config.json` or `.claude/mcp_servers.json`):

```json
{
  "mcpServers": {
    "insta360": {
      "command": "uvx",
      "args": ["insta360-cloud-mcp"],
      "env": {
        "INSTA360_EMAIL": "your-email@example.com",
        "INSTA360_PASSWORD": "your-password"
      }
    }
  }
}
```

## Quick Start (from source)

```bash
git clone https://github.com/alfari16/insta360-cloud-mcp.git
cd insta360-cloud-mcp
```

Then configure your MCP client:

```json
{
  "mcpServers": {
    "insta360": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/insta360-cloud-mcp", "insta360-cloud-mcp"],
      "env": {
        "INSTA360_EMAIL": "your-email@example.com",
        "INSTA360_PASSWORD": "your-password"
      }
    }
  }
}
```

## Environment Variables

| Variable | Description |
|---|---|
| `INSTA360_EMAIL` | Your Insta360 account email |
| `INSTA360_PASSWORD` | Your Insta360 account password (MD5-hashed internally by the server) |

## Tools Reference

| Tool | Parameters | Description |
|---|---|---|
| `list_media` | `last_update_position?` | Fetch all media items, auto-paginating through all pages |
| `get_download_urls` | `media_id` | Get download URLs for a specific media item |
| `download_media` | `media_id`, `download_dir?` | Download all files for a media item (default: `~/gdrive/Insta360`) |
| `download_all` | `download_dir?`, `last_update_position?` | Download entire cloud library |
| `delete_media` | `media_ids` | Move media items to the recycle bin |

## Token Caching

The server caches authentication tokens at `~/.insta360_tokens.json` to avoid re-authenticating on every startup. Tokens are automatically refreshed when they expire. Delete this file to force a fresh login.

## License

MIT
