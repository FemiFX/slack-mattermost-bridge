FROM python:3.12-slim

WORKDIR /app

RUN adduser --system --group --no-create-home mmbridge

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mmbridge/ ./mmbridge/

# The mapping database lives here; mount it so threading, edits and loop
# prevention survive a restart.
RUN mkdir -p /data && chown mmbridge:mmbridge /data
VOLUME ["/data"]
ENV MMBRIDGE_DB=/data/mmbridge.sqlite3 \
    PYTHONUNBUFFERED=1

USER mmbridge
ENTRYPOINT ["python", "-m", "mmbridge"]
CMD ["run"]
