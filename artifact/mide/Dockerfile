FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

# Install uv from distroless image (preferred over curl installer)
COPY --from=ghcr.io/astral-sh/uv:0.8.11 /uv /uvx /bin/

WORKDIR /app

# Copy lockfile and project metadata first (layer cache optimization)
COPY pyproject.toml uv.lock ./

# Install production deps into /venv (outside /app so volume mounts don't shadow it)
ENV UV_PROJECT_ENVIRONMENT=/venv
RUN uv sync --frozen --no-dev

# Add venv to PATH so `python` resolves to the venv Python (not system Python)
ENV PATH="/venv/bin:$PATH"

# Install Playwright Chromium binary (used by BFS crawler)
RUN uv run --no-dev playwright install chromium

# Copy application code
COPY main.py .
COPY lib/ lib/
COPY config/ config/

# Create output and logs directories
RUN mkdir -p output logs

ENTRYPOINT ["python", "-u", "main.py"]
