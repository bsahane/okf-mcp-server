FROM registry.access.redhat.com/ubi9/python-312:latest

# --------------------------------------------------------------------------------------------------
# set the working directory to /app
# --------------------------------------------------------------------------------------------------

WORKDIR /app

# --------------------------------------------------------------------------------------------------
# Copy manifest files and install python packages
# --------------------------------------------------------------------------------------------------

USER root
COPY pyproject.toml /app/pyproject.toml
# EXTRAS=ingest builds the ingestion image (Docling for PDF/Office sources);
# EXTRAS=semantic adds the embedding runtime. The default is the server only.
ARG EXTRAS=""
# No uv cache in the layer; CPU-only PyTorch (the default Linux wheel pulls
# ~5 GB of CUDA libraries that CPU-only nodes never use). torchvision must
# come from the same index, or its operators fail to load against CPU torch.
ENV UV_NO_CACHE=1
RUN pip install --no-cache-dir uv \
    && uv venv \
    && if [ -n "$EXTRAS" ]; then \
         uv pip install "torch==2.14.0" "torchvision==0.29.0" --index-url https://download.pytorch.org/whl/cpu; \
       fi \
    && uv pip install -r pyproject.toml $(for e in $EXTRAS; do printf -- "--extra %s " "$e"; done) \
    # rapidocr pulls the GUI OpenCV build, which needs libGL (absent in UBI);
    # the headless build provides the same cv2 module without it.
    && if uv pip show opencv-python >/dev/null 2>&1; then \
         v=$(uv pip show opencv-python | sed -n 's/^Version: //p'); \
         uv pip uninstall opencv-python && uv pip install "opencv-python-headless==$v"; \
       fi \
    # Bake Docling's layout, table and OCR models into the image: pods run with
    # a read-only root filesystem and often without internet access.
    && if [ -x /app/.venv/bin/docling-tools ]; then \
         /app/.venv/bin/docling-tools models download -q -o /app/models layout tableformer rapidocr \
         && chmod -R a+rX /app/models; \
       fi
ENV DOCLING_ARTIFACTS_PATH=/app/models
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# Numeric, so Kubernetes can verify runAsNonRoot (OpenShift assigns its own UID).
USER 1001

# --------------------------------------------------------------------------------------------------
# copy source code and files
# --------------------------------------------------------------------------------------------------

COPY okf_mcp_server /app/okf_mcp_server

# --------------------------------------------------------------------------------------------------
# Set PYTHONPATH to include /app
# --------------------------------------------------------------------------------------------------

ENV PYTHONPATH=/app

EXPOSE 5001

# --------------------------------------------------------------------------------------------------
# add entrypoint for the container
# --------------------------------------------------------------------------------------------------

CMD ["/app/.venv/bin/python", "-m", "okf_mcp_server.src.main"]
