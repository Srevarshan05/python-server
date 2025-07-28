# Use official Python runtime as base image
FROM python:3.9-slim

# Set working directory in container
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV FASTAPI_SERVER_NAME=0.0.0.0
ENV FASTAPI_SERVER_PORT=7860

# Install build dependencies for some Python packages (like uvicorn, fastapi)
# and for the signal handling
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements.txt and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# --- Diagnostic step: List contents of /app before copying index.html ---
# This command will output the contents of the /app directory in the build logs.
# It can help diagnose if an unexpected 'file' is already present.
RUN echo "Contents of /app before copying index.html:"
RUN ls -la /app
# --- End Diagnostic step ---

# Copy the application files
COPY app.py .
# Explicitly copy index.html to /app/index.html to avoid any ambiguity with '.'
# This is the primary change to address "cannot copy to non-directory: .../app/file"
COPY index.html /app/index.html

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash app && \
    chown -R app:app /app
USER app

# Expose the port FastAPI will run on
EXPOSE 7860

# Command to run the FastAPI application using Uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
