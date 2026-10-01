FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --create-home --uid 1000 app
USER app
CMD ["uv", "run", "--no-sync", "evil-captcha", "serve", \
     "--host", "0.0.0.0", "--admin-host", "0.0.0.0", "--trusted-proxy", "*"]
