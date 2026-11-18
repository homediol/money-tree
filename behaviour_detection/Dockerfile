FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt requirements.lock /app/
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app/behaviour_detection
RUN useradd --create-home --uid 10001 behaviour
USER behaviour
CMD ["uvicorn", "behaviour_detection.api:app", "--host", "0.0.0.0", "--port", "8100"]
