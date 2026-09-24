# The launcher. Build from the repo root:
#   docker build -f docker/sandboxd.Dockerfile -t claims-qa-sandboxd:1 .
# Compose mounts /var/run/docker.sock into it. That socket is root on the host whatever user this
# container runs as, so the defence here is the narrow surface: one endpoint, one fixed argv.
FROM docker:29-cli@sha256:018edbc908e08fcc9dbf029c812c34251e9b4719e6f71ca0e5eae2a987d014ca AS cli

FROM python:3.13.13-slim-trixie@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286

COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker

# Same layout as the repo, so sandboxd imports the one codecheck.py the app also uses.
WORKDIR /srv
COPY app/sandbox/__init__.py app/sandbox/codecheck.py app/sandbox/
COPY sandbox/__init__.py sandbox/sandboxd.py sandbox/

EXPOSE 8700
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8700/healthz', timeout=2)"]
CMD ["python", "-m", "sandbox.sandboxd"]
