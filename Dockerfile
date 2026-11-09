# Alternative à uv : image Playwright officielle (navigateur inclus).
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config.example.toml /app/config.example.toml
# Montes ton config.toml et data/ en volume :
#   docker compose up -d
CMD ["lbc-alerte", "daemon", "-c", "/app/config.toml"]
