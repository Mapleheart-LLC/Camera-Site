FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source
COPY bot.py .

# Unbuffered output for clean Docker logs
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
