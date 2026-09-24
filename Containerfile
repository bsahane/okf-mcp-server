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
RUN pip install uv \
    && uv venv \
    && uv pip install -r pyproject.toml $(for e in $EXTRAS; do printf -- "--extra %s " "$e"; done)
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
