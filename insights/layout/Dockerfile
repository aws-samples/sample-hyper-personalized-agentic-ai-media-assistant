FROM public.ecr.aws/lambda/python:3.12

# Exact pins, resolved and tested together (umap-learn on numba/llvmlite).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# numba caches compiled functions next to the source by default; Lambda's code
# directory is read-only, so point the cache at /tmp.
ENV NUMBA_CACHE_DIR=/tmp/numba

COPY catalog.py labels.py build_layout.py ./
CMD ["build_layout.handler"]
