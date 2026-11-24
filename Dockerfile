FROM mcr.microsoft.com/playwright/python:v1.65.2-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

ENV PORT=8888
EXPOSE 8888

CMD ["python", "web_server.py"]
