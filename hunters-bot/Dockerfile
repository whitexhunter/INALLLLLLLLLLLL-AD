FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hunters_bot.py .
COPY selfbot_client.py .
RUN mkdir -p data

CMD ["python", "bot.py"]
