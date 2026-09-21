FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/logs

# The entrypoint waits for the shared database and the schema Flask (Minty) owns; it runs
# NO migrate. `sed` strips CRLF so the script still runs when the repo is checked out on
# Windows with autocrlf.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && \
    chmod +x /usr/local/bin/entrypoint.sh

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8004

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Two workers, two schedulers: the pass's advisory lock lets one run (billing/scheduler.py).
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8004", "config.wsgi:application"]
