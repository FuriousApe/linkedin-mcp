FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Pre-process LCA data at build time — extracts certified H-1B employer
# names into a compact text file and discards the large xlsx.
COPY data/lca.xlsx ./data/lca.xlsx
RUN python -m src.build_lca && rm data/lca.xlsx

# MCP servers communicate with Claude Desktop over stdio (stdin/stdout).
# The -u flag ensures Python stdout is unbuffered — critical for MCP.
CMD ["python", "-u", "-m", "src.server"]
