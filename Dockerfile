# Deep-space TT&C time normalizer.
# Stdlib-only Python: no pip install layer, deterministic and small.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0

WORKDIR /srv

# Application code, unit tests (used by the one-shot verify service) and
# the verify runner itself.
COPY app ./app
COPY tests ./tests
COPY verify ./verify

# No network tools in slim; use the interpreter for the health probe.
HEALTHCHECK --interval=5s --timeout=3s --start-period=2s --retries=10 \
    CMD python3 -c "import os,urllib.request,sys; port=os.environ.get('PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status == 200 else 1)"

EXPOSE 8080

CMD ["python3", "-m", "app.server"]
