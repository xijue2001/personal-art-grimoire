ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY grimoire/ grimoire/
COPY site/ site/
EXPOSE 18770
ENTRYPOINT ["python", "-B", "-m", "grimoire.app"]
