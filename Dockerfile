FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir --no-deps "."
COPY VERSION /app/VERSION
RUN groupadd -r tubemail && useradd -r -g tubemail -d /app tubemail
RUN mkdir -p /data && chown -R tubemail:tubemail /app /data
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
