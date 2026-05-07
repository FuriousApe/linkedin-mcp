FROM python:3.12-slim

# Keeps Python from generating .pyc files and enables stdout/stderr logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# MCP servers communicate with Claude Desktop over stdio (stdin/stdout).
# The -u flag ensures Python stdout is unbuffered — critical for MCP.
CMD ["python", "-u", "-m", "src.server"]
