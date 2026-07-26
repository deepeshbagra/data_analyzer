# Shared image for api, worker and migrations. Python pinned to 3.12 regardless
# of what the host has installed.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml constraints.txt ./
# Install the dependency set in its own layer so it is only rebuilt when
# pyproject.toml changes; the real source arrives below and, in dev, is
# bind-mounted over it.
#
# The stub packages are required, not incidental: pyproject declares
# `packages = ["api", "worker"]`, so setuptools aborts with "package directory
# 'api' does not exist" when it builds metadata against pyproject.toml alone.
# The editable install records a path pointer to /app, so once the real files
# are in place -- COPY'd here, bind-mounted in dev -- they are what gets
# imported, and these empty files are overwritten.
RUN mkdir -p api worker \
    && touch api/__init__.py worker/__init__.py \
    && pip install --no-cache-dir -c constraints.txt -e ".[dev]"

COPY . .

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
