# Pin this by digest before you deploy, so the image an auditor verifies is
# byte-for-byte the one that runs. Resolve a digest with:
#   docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim

ARG GIT_SHA=unknown
ARG SOURCE_URL=unknown
ENV GIT_SHA=${GIT_SHA} \
    SOURCE_URL=${SOURCE_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 anon

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py ./

USER anon
CMD ["python", "-u", "bot.py"]
