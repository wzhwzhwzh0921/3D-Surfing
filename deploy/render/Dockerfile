# Optional worker image; this Dockerfile is not required for local execution.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libegl1 libgl1 libgl1-mesa-dri libglib2.0-0 tar \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'numpy>=1.24,<3' 'trimesh>=4,<5' \
    'pyrender>=0.1.45' 'Pillow>=10' 'scipy>=1.10'
ENV PYOPENGL_PLATFORM=egl
# K8s backend uploads an immutable worker snapshot at runtime.
CMD ["sleep", "infinity"]
