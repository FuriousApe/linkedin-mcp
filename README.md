# LinkedIn Jobs MCP Server

Self-hosted MCP server that scrapes LinkedIn jobs via the `linkedin-api` Voyager wrapper.
Runs in Docker. No Apify, no per-run costs.

## Setup

### 1. Configure credentials

```bash
cp .env.example .env
# Edit .env and fill in your LinkedIn email and password
```

> ⚠️ Use a burner LinkedIn account — automated access violates LinkedIn's ToS and risks the account being restricted.

### 2. Build and test

```bash
# Build the Docker image
docker build -t linkedin-mcp .

# Quick smoke test — should print the MCP server startup log
docker run --rm --env-file .env linkedin-mcp
# Ctrl+C to stop
```

### 3. Wire into Claude Desktop

Edit your Claude Desktop config file:
- **Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "linkedin-jobs": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/ABSOLUTE/PATH/TO/linkedin-mcp/.env",
        "linkedin-mcp"
      ]
    }
  }
}
```

> ⚠️ Use the **absolute path** to your .env file. `~/` does not expand here.

### 4. Restart Claude Desktop

After saving the config, fully quit and reopen Claude Desktop.
You'll see a 🔧 tools icon in the chat bar — click it to confirm
`scrape_jobs`, `get_job_details`, and `check_auth` are listed.

### 5. First conversation

```
You: Check if my LinkedIn session is authenticated
Claude: [calls check_auth] ✓ Authenticated as John Doe

You: Scrape 20 AI Engineer or ML Engineer jobs posted in the last 3 days in the US
Claude: [calls scrape_jobs] ...returns full job list with descriptions
```

---

## Development

```bash
# Run with live source reloading
docker compose up

# Inspect MCP tools without Claude Desktop
npx @modelcontextprotocol/inspector docker run --rm -i --env-file .env linkedin-mcp
```

## Project structure

```
linkedin-mcp/
├── src/
│   ├── server.py        # MCP server — tool definitions and handlers
│   ├── scraper.py       # linkedin-api Voyager wrapper calls
│   ├── models.py        # Pydantic models for Job data
│   └── __init__.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```
