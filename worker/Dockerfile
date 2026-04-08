FROM node:22-slim AS node-runtime

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app/worker
ENV PYTHONUNBUFFERED=1
ENV PATH=/opt/openclaw/npm-global/bin:/usr/local/bin:$PATH

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node

RUN pip install --no-cache-dir -U pip

COPY worker/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "worker/heartbeat_supervisor.py"]
