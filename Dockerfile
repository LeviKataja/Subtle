# Subtle with ffmpeg, for running next to Sonarr/Radarr/Bazarr.
# Mount the media library at the same paths those apps use and no path mapping is needed.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY subtle ./subtle
RUN pip install --no-cache-dir .

ENV SUBTLE_HOME=/data
VOLUME /data
EXPOSE 8756
CMD ["subtle", "serve", "--host", "0.0.0.0", "--no-open"]
