FROM python:3.11-slim

WORKDIR /app

# Create a non-privileged user
RUN useradd -u 10001 -m -U appuser && \
    chown -R appuser:appuser /app

# Install python-cryptography (uses precompiled wheels)
RUN pip install --no-cache-dir cryptography

USER 10001

# Copy application script
COPY --chown=appuser:appuser broker.py .

EXPOSE 8080

ENV PORT=8080

ENTRYPOINT ["python3", "broker.py"]
