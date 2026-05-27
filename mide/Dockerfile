FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# Install uv from distroless image (preferred over curl installer)
COPY --from=ghcr.io/astral-sh/uv:0.8.11 /uv /uvx /bin/

# Install real Google Chrome (required by from_system_chrome).
# Playwright's bundled Chromium works for BFS crawling but Google invalidates
# Gmail sessions in non-Chrome browsers due to fingerprint mismatch.
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget gnupg \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Create Chrome profile + Copilot token directories (mount from host)
RUN mkdir -p /root/.config/google-chrome /root/.config/method-interchangeability

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
