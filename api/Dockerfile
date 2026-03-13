FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

COPY frontend/package.json ./
RUN npm install --no-fund --no-audit

COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir -U pip

COPY api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./
COPY --from=frontend-builder /frontend/dist ./frontend_dist

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
