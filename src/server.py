import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from .scraper import LinkedInScraper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,  # logs go to stderr so they don't corrupt the MCP stdio stream
)
logger = logging.getLogger("linkedin-mcp")

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

li_at = os.environ.get("LINKEDIN_LI_AT", "")
jsessionid = os.environ.get("LINKEDIN_JSESSIONID", "")

if not li_at or not jsessionid:
    logger.error("LINKEDIN_LI_AT and LINKEDIN_JSESSIONID must be set in .env")
    sys.exit(1)

scraper = LinkedInScraper(li_at=li_at, jsessionid=jsessionid)
app = Server("linkedin-jobs")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="scrape_jobs",
            description=(
                "Search LinkedIn for job listings using authenticated session. "
                "Returns full job descriptions, required skills, applicant count, "
                "salary (when available), seniority level, and direct LinkedIn URLs. "
                "Supports boolean keywords: 'AI Engineer OR ML Engineer'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": "Job title / keywords. Supports boolean: 'AI Engineer OR Software Engineer NOT Senior'",
                    },
                    "location": {
                        "type": "string",
                        "description": "Location string, e.g. 'United States', 'New York', 'San Francisco Bay Area'",
                        "default": "United States",
                    },
                    "days_ago": {
                        "type": "integer",
                        "description": "Only return jobs posted within this many days. Supported: 1, 2, 3, 7, 14, 30. Ignored if hours_ago is set.",
                        "default": 3,
                        "enum": [1, 2, 3, 7, 14, 30],
                    },
                    "hours_ago": {
                        "type": "integer",
                        "description": "Only return jobs posted within this many hours. Overrides days_ago when set. Supported: 1, 2, 6, 12, 24",
                        "enum": [1, 2, 6, 12, 24],
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of jobs to return (max 49 per search)",
                        "default": 25,
                        "maximum": 49,
                    },
                    "remote_only": {
                        "type": "boolean",
                        "description": "If true, only return remote jobs",
                        "default": False,
                    },
                },
                "required": ["keywords"],
            },
        ),
        types.Tool(
            name="get_job_details",
            description=(
                "Fetch complete details for a single LinkedIn job posting. "
                "Pass either a LinkedIn job URL or a numeric job ID. "
                "Returns the full description, all skills, salary, and company info."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id_or_url": {
                        "type": "string",
                        "description": "LinkedIn job URL (https://www.linkedin.com/jobs/view/1234567890/) or numeric job ID",
                    },
                },
                "required": ["job_id_or_url"],
            },
        ),
        types.Tool(
            name="check_auth",
            description=(
                "Verify that the LinkedIn session is authenticated and check "
                "which account is active. Run this at the start of each session."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    logger.info(f"Tool called: {name} with args: {list(arguments.keys())}")

    try:
        if name == "scrape_jobs":
            result = await scraper.search(
                keywords=arguments["keywords"],
                location=arguments.get("location", "United States"),
                days_ago=arguments.get("days_ago", 3),
                hours_ago=arguments.get("hours_ago"),
                count=arguments.get("count", 25),
                remote_only=arguments.get("remote_only", False),
            )
            return [types.TextContent(type="text", text=result.model_dump_json(indent=2))]

        elif name == "get_job_details":
            job = await scraper.get_job_details(arguments["job_id_or_url"])
            return [types.TextContent(type="text", text=job.model_dump_json(indent=2))]

        elif name == "check_auth":
            result = await scraper.check_auth()
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.exception(f"Tool {name} failed")
        error_payload = json.dumps({"error": type(e).__name__, "message": str(e)})
        return [types.TextContent(type="text", text=error_payload)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    logger.info("LinkedIn MCP server starting...")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )

if __name__ == "__main__":
    asyncio.run(main())
