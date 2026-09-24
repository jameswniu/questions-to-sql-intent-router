# The throwaway image each analysis job runs in. Build from the repo root:
#   docker build -f docker/sandbox.Dockerfile -t claims-qa-sandbox:1 .
FROM python:3.13.13-slim-trixie@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286

RUN pip install --no-cache-dir --disable-pip-version-check \
        numpy==2.5.3 pandas==3.0.6 python-dateutil==2.9.0.post0 six==1.17.0 \
    && pip uninstall --yes --disable-pip-version-check pip

# The runner and the golden templates, root-owned so uid 65534 cannot change them even before
# --read-only applies. The app imports this same templates.py from app/sandbox/.
COPY sandbox/runner.py app/sandbox/templates.py /runner/
RUN python -m compileall -q /runner

# OpenBLAS starts one thread per visible core, and threads count against --pids-limit 64: on an
# 18-core host numpy's import alone took 18 of the 64, and past 64 cores the import would fail.
ENV OPENBLAS_NUM_THREADS=1

USER 65534:65534
WORKDIR /tmp
# Every job runs under its own deadline, so a container ends itself even if the daemon that
# started it never kills it. timeout is PID 1 and SIGKILLs the job's whole process group; sandboxd
# passes the seconds and the command on each run, and this CMD is only the default.
# -I keeps PYTHON* variables, the user site and the script directory out of the import path.
# -S is not used: it also drops site-packages, where numpy and pandas live.
ENTRYPOINT ["timeout", "--signal=KILL"]
CMD ["11", "python", "-I", "/runner/runner.py"]
