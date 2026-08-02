FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN groupadd --gid 65532 analyzer \
    && useradd --uid 65532 --gid 65532 --no-create-home --home-dir /nonexistent analyzer

COPY deploy/l2-analyzer-requirements.txt /opt/l2-analyzer-requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /opt/l2-analyzer-requirements.txt

COPY --chown=65532:65532 tools/l2_analyzer.py /opt/l2_analyzer.py
COPY --chown=65532:65532 ditto_screener/data/starter-kit-provenance-*.json /opt/starter-manifests/

USER 65532:65532
WORKDIR /scratch
ENTRYPOINT ["python3", "-I", "/opt/l2_analyzer.py"]
