FROM python:3.10.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    ffmpeg

RUN apt-get install build-essential

COPY ./bolna /app/bolna


COPY ./requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

COPY local_setup/quickstart_server.py /app/

EXPOSE 5001

CMD ["uvicorn", "quickstart_server:app", "--host", "0.0.0.0", "--port", "5001"]
