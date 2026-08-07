# AgentCore Runtime requires linux/arm64. An amd64 image fails at DEPLOY, not at
# build, so the platform is pinned here rather than left to the builder's host.
FROM --platform=linux/arm64 python:3.12-slim-bookworm

# NO browser, no Chromium, no fonts.
#
# Worth stating because the sibling ground-truth runtime prompts the question: that
# one attaches to a REMOTE browser and still installs no browser locally. This one
# never opens a page at all. It is a chat agent that emits AG-UI events.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Contract: 0.0.0.0:8080, POST /invocations, GET /ping.
EXPOSE 8080

# Single worker on purpose. The blocking Bedrock call is already dispatched off the
# event loop via `run_in_threadpool` in server.py, and the concurrency that governs
# spend is the gateway's R10 cap (3 active sessions per tenant) — a cap this image
# cannot see. Adding workers here would multiply it silently.
CMD ["uvicorn", "app.skill_builder.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
