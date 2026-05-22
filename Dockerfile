FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY sync_camera_time.py ./sync_camera_time.py

RUN uv sync --frozen --no-dev

CMD ["uv", "run", "tpts"]
