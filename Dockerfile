FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /root/.cache
# The browser extension zips offered on the Settings page (and the version behind its update badge).
COPY extension ./extension
RUN bash extension/build.sh && mv extension/dist/*.zip /app/ && rm -rf extension
ENV KB_EXTENSION_DIR=/app

# Unraid's nobody:users by default; override with `user:` in compose.
USER 99:100
VOLUME /data

HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
    CMD ["python", "-m", "kbridge", "-c", "/data/config.yaml", "health"]

ENTRYPOINT ["python", "-m", "kbridge", "-c", "/data/config.yaml"]
CMD ["run"]
