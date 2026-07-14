# Tundra Portal API image: torch+CUDA (for SAM on GPU) + GDAL (for rasterio/pyproj).
FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
      gdal-bin libgdal-dev g++ \
    && rm -rf /var/lib/apt/lists/*

# Allow PROJ to fetch datum-shift grids on demand (Pulkovo 1942 -> WGS84 accuracy).
ENV PROJ_NETWORK=ON \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements-phase2.txt .
RUN pip install --no-cache-dir -r requirements-phase2.txt

COPY utils ./utils
COPY services ./services

EXPOSE 8000
CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
