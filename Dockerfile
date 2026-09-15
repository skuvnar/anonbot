# Pin this by digest before you deploy, so the image an auditor verifies is
# byte-for-byte the one that runs. Resolve a digest with:
#   docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim

ARG GIT_SHA=unknown
ARG AVATAR_BASE=
ENV GIT_SHA=${GIT_SHA} \
    ANON_AVATAR_BASE=${AVATAR_BASE} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 anon

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py ./
COPY avatars/ ./avatars/

USER anon
CMD ["python", "-u", "bot.py"]
