# syntax=docker/dockerfile:1
# Multi-platform index digest resolved from the official image on 2026-09-03.
ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
FROM ${PYTHON_BASE_IMAGE}

ARG JOB_SEARCH_UID=10001
ARG JOB_SEARCH_GID=10001
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$SOURCE_REVISION

ENV PYTHONDONTWRITEBYTECODE=1 \
    JOB_SEARCH_SOURCE_REVISION=$SOURCE_REVISION \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/var/lib/job-search/home \
    XDG_DATA_HOME=/var/lib/job-search/private-state

RUN groupadd --gid "${JOB_SEARCH_GID}" jobsearch \
    && useradd --uid "${JOB_SEARCH_UID}" --gid "${JOB_SEARCH_GID}" \
       --home-dir "${HOME}" --no-create-home --shell /usr/sbin/nologin jobsearch

WORKDIR /opt/job-search

COPY requirements/cloud.txt ./requirements/cloud.txt
RUN python -m pip install --no-deps -r requirements/cloud.txt \
    && python -m pip check

COPY --chown=root:root . .

# The scraper's registry cache retains its historical repository-relative name. The
# worker sends CSV/JSON/failure output directly to the database directory; only this
# one cache therefore needs a compatibility symlink into persistent state.
RUN mkdir -p /var/lib/job-search \
    && chown -R jobsearch:jobsearch /var/lib/job-search \
    && ln -s /var/lib/job-search/boards.json /opt/job-search/boards.json \
    && chmod -R a+rX /opt/job-search \
    && chmod -R a-w /opt/job-search \
    && chmod 0555 /opt/job-search

USER jobsearch

ENTRYPOINT ["python", "-m", "job_search.cloud"]
CMD ["--help"]
