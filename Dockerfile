# syntax=docker/dockerfile:1.7
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.local/bin:${PATH}"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl build-essential libpq-dev gettext \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

COPY requirements/ /app/requirements/
ARG REQUIREMENTS=requirements/prod.txt
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --system -r ${REQUIREMENTS}

COPY . /app/
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

# The entrypoint runs migrations, compiles translations, and collects static
# assets before exec'ing CMD. ``docker-compose.dev.yml`` overrides CMD with
# ``runserver`` for local development — the entrypoint still runs first so
# dev containers also get a fresh migrate / compilemessages / collectstatic.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "acta.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
