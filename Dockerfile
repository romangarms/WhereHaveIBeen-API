FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY config.py .
COPY models.py .
COPY auth.py .

# Create directory for database
RUN mkdir -p /opt/usermanagement/database

# Expose port
EXPOSE 5002

# Run with waitress
CMD ["python", "app.py"]
