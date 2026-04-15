# Dockerfile for local-edge FastAPI agent
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Install system dependencies (if needed for ffmpeg)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the default agent port
EXPOSE 8080

# Set environment variables (can be overridden by compose)
ENV PYTHONUNBUFFERED=1

# Run the FastAPI app
CMD ["python", "agent.py"]
