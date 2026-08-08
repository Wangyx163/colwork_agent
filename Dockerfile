# 3.12 because pyproject declares it as the supported floor and CI runs it
# there. Development happens on 3.14; building on the floor keeps the declared
# minimum honest.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependency metadata first so a source edit does not invalidate the pip layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -e ".[postgres,feishu]"

COPY db ./db
COPY fixtures ./fixtures
COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# The workbench serves a port and the worker talks to a model provider; neither
# needs root. Ownership is set after the editable install so the generated
# egg-info stays writable.
RUN useradd --create-home --uid 10001 colwork \
 && chown -R colwork:colwork /app
USER colwork

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "collab_agent", "--help"]
