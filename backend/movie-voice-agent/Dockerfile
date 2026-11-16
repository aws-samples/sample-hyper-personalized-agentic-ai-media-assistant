# ARM64 base image for AgentCore Runtime.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

# System deps for PyAudio. BidiAgent pulls in pyaudio (via strands[bidi-all])
# even though this server receives audio over the WebSocket, not a local mic —
# so PortAudio must be present for the import to succeed.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libasound-dev \
    libportaudio2 \
    libportaudiocpp0 \
    portaudio19-dev \
    gcc \
    python3-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Agent code
COPY app.py movie_agent.py ./

# Non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Tell app.py it's containerized (bind 0.0.0.0)
ENV CONTAINER_ENV=true

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping').read()"]

# AgentCore Runtime expects the server on port 8080 (/ws + /ping).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
