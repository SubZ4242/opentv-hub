# Aufnahmen (TV Abo): records the subscribed games from Dispatcharr into HLS, plays them back.
FROM python:3.13-alpine
RUN apk add --no-cache ffmpeg tzdata && mkdir -p /app/data /app/recordings
WORKDIR /app
COPY rec.py .
ENV DATA=/app/data REC_DIR=/app/recordings ROOT=/app/recordings PORT=35090 PYTHONUNBUFFERED=1
EXPOSE 35090
CMD ["python3", "-u", "rec.py"]
