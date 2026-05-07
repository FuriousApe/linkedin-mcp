# LinkedIn Jobs MCP Server

Self-hosted MCP server that scrapes LinkedIn jobs with your authenticated
session cookies. Runs in Docker. No Apify, no per-run costs.

## Setup

### 1. Get your cookies

1. Install the **Cookie-Editor** Chrome extension
2. Log into LinkedIn (use a burner account)
3. Click Cookie-Editor → Export (JSON)
4. Find and copy two values:
   - `li_at` — your session cookie
   - `JSESSIONID` — used as CSRF token (value looks like `ajax:1234...`)

### 2. Configure

```bash
cp .env.example .env
# Edit .env and paste your cookie values
```

### 3. Build and test

```bash
# Build the Docker image
docker build -t linkedin-mcp .

# Quick smoke test — should print the MCP server startup log
docker run --rm --env-file .env linkedin-mcp
# Ctrl+C to stop
```

### 4. Wire into Claude Desktop

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

### 5. Restart Claude Desktop

After saving the config, fully quit and reopen Claude Desktop.
You'll see a 🔧 tools icon in the chat bar — click it to confirm
`scrape_jobs`, `get_job_details`, `check_cookie`, and `update_cookies` are listed.

### 6. First conversation

```
You: Check if my LinkedIn cookie is valid
Claude: [calls check_cookie] ✓ Authenticated as John Doe

You: Scrape 20 AI Engineer or ML Engineer jobs posted in the last 3 days in the US
Claude: [calls scrape_jobs] ...returns full job list with descriptions
```

---

## Cookie refresh (every 30–60 days)

When cookies expire, re-export from Cookie-Editor and tell Claude:

```
Update my LinkedIn cookies: li_at is "new_value" and jsessionid is "new_value"
```

Claude will call `update_cookies` — no container restart needed.

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
│   ├── scraper.py       # LinkedIn Voyager API calls (httpx)
│   ├── models.py        # Pydantic models for Job data
│   └── __init__.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```
