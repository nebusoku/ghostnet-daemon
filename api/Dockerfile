FROM python:3.12-slim

WORKDIR /app

# System build deps for numpy, grpcio, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \build-essential \&& rm -rf /var/lib/apt/lists/*

# Copy API requirements from the api/ subdir
COPY api/requirements.txt ./requirements.txt

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the API code into the image
COPY api/ /app/

# Expose (for docs / clarity; docker run -p still decides host port)
EXPOSE 8001

# Run the FastAPI app
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8001"]
