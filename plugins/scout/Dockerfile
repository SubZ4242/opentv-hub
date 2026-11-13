# Entdecken (Scout): films worth getting that are not in the library yet. Python, no packages.
FROM python:3.12-slim
RUN useradd --uid 568 --create-home scout && mkdir -p /app/data && chown 568:568 /app/data
WORKDIR /app
COPY scout.py .
USER 568
ENV DATA=/app/data PORT=35120 PYTHONUNBUFFERED=1
EXPOSE 35120
CMD ["python3", "scout.py"]
