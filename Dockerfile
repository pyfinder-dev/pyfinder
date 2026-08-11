# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12.13
ARG PYTHON_SOURCE_SHA256=c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684

FROM ghcr.io/sceylan/finder-base:gmt5 AS build

ARG PYTHON_VERSION
ARG PYTHON_SOURCE_SHA256

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Debian 10 has left the active mirrors. The base records this dated snapshot,
# which keeps the compiler inputs reproducible without changing the final
# image's operating-system foundation.
RUN sed -i \
        -e 's|^# deb http://snapshot.debian.org|deb http://snapshot.debian.org|' \
        -e '/deb.debian.org/d' \
        /etc/apt/sources.list \
    && printf 'Acquire::Check-Valid-Until "false";\n' \
        > /etc/apt/apt.conf.d/99snapshot \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        libbz2-dev \
        libncurses5-dev \
        libreadline-dev \
        tk-dev \
        uuid-dev \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/python-build
RUN curl --fail --location --show-error \
        --output Python.tar.xz \
        "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
    && printf '%s  %s\n' "${PYTHON_SOURCE_SHA256}" Python.tar.xz \
        | sha256sum --check --strict \
    && tar --extract --file Python.tar.xz \
    && cd "Python-${PYTHON_VERSION}" \
    && ./configure \
        --prefix=/opt/python-3.12 \
        --with-ensurepip=install \
    && make --jobs=2 \
    && make install \
    && /opt/python-3.12/bin/python3.12 -c \
        'import bz2, ctypes, lzma, sqlite3, ssl; print(ssl.OPENSSL_VERSION)'

ENV PATH="/opt/python-3.12/bin:${PATH}"
ENV PYTHONNOUSERSITE=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /build/pyfinder
COPY pyproject.toml README.md LICENSE ./
COPY pyfinder ./pyfinder

# ParamWS remains a separate, normally installed distribution. Its source is
# used only to construct the wheel and its exact master commit is carried into
# the final image as provenance.
RUN git clone \
        --branch master \
        --single-branch \
        https://github.com/pyfinder-dev/paramws-clients.git \
        /build/paramws-clients \
    && git -C /build/paramws-clients rev-parse HEAD \
        > /build/paramws-commit \
    && printf 'ParamWS commit: ' \
    && cat /build/paramws-commit \
    && python3.12 -m pip wheel \
        --wheel-dir /wheelhouse \
        /build/paramws-clients \
        /build/pyfinder

FROM ghcr.io/sceylan/finder-base:gmt5

ARG PYTHON_VERSION
# The caller supplies the digest observed after pulling the required base. A
# missing value is a build error so image provenance cannot silently reuse an
# old digest merely because the Dockerfile was not updated.
ARG PYFINDER_BASE_DIGEST

LABEL org.opencontainers.image.base.name="ghcr.io/sceylan/finder-base:gmt5" \
      io.pyfinder.base.digest="${PYFINDER_BASE_DIGEST}" \
      io.pyfinder.python.version="${PYTHON_VERSION}" \
      io.pyfinder.build-information="/usr/local/share/pyfinder/build-info.json"

COPY --from=build /opt/python-3.12 /opt/python-3.12
COPY --from=build /wheelhouse /tmp/wheelhouse
COPY --from=build /build/paramws-commit /tmp/paramws-commit
COPY --chmod=0755 scripts/pyfinder-entrypoint \
    /usr/local/bin/pyfinder-entrypoint

ENV PATH="/opt/python-3.12/bin:${PATH}"
ENV PYTHONNOUSERSITE=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN <<'SHELL'
set -eu

if [ -z "${PYFINDER_BASE_DIGEST}" ]; then
    printf 'PYFINDER_BASE_DIGEST is required; pull and inspect the base image before building.\n' >&2
    exit 1
fi

python3.12 -m pip install \
    --no-cache-dir \
    --no-index \
    --find-links /tmp/wheelhouse \
    pyfinder==1.0.0 \
    paramws-clients==0.1.0
mkdir -p /usr/local/share/pyfinder

# ParamWS configures its file handler when imported. This temporary safe path
# supports build-time imports and is removed again before this layer ends.
export PARAMWS_COMMIT="$(cat /tmp/paramws-commit)"
export PARAMWS_LOG_FILE=/tmp/pyfinder-build-paramws.log
export PYFINDER_BASE_DIGEST

test "$(python3.12 -c 'import platform; print(platform.python_version())')" = "${PYTHON_VERSION}"
test "$(python3 -c 'import platform; print(platform.python_version())')" = "${PYTHON_VERSION}"
! command -v python3.9
test "$(getent passwd 1000 | cut -d: -f1,3,4)" = "sysop:1000:1000"
test "$(getent group 1000 | cut -d: -f1,3)" = "sysop:1000"
command -v pyfinder
pyfinder --help > /dev/null
command -v mountpoint
bash -n /usr/local/bin/pyfinder-entrypoint
test -x /usr/local/src/FinDer/finder_run
test -x /usr/local/src/FinDer/finder_create_mask

python3.12 - <<'PYTHON'
import bz2
import ctypes
import importlib.metadata as metadata
import json
import lzma
import os
from pathlib import Path
import platform
import sqlite3
import ssl

import geopandas
import numpy
import paramws
from paramws.clients import (
    EMSCFeltReportClient,
    ESMShakeMapClient,
    FeltReportEventData,
    FeltReportIntensityData,
    PeakMotionData,
    RRSMPeakMotionClient,
    ShakeMapEventData,
    ShakeMapStationAmplitudes,
)
from paramws.utils import customlogger as paramws_customlogger
import pyfinder
from pyfinder import cli, finderexec, findermanager, runtime
import shapely
import tornado

pyfinder_root = Path(pyfinder.__file__).resolve().parent
paramws_root = Path(paramws.__file__).resolve().parent
pyfinder_distribution = metadata.distribution("pyfinder")
paramws_distribution = metadata.distribution("paramws-clients")

for root in (pyfinder_root, paramws_root):
    assert "site-packages" in root.parts, root
    assert not any(part in {"build", "paramws-clients"} for part in root.parts), root

required_resources = (
    pyfinder_root / "extern/finder_regional_wkt",
    pyfinder_root / "extern/ne_110m_admin_0_countries",
)
assert all(path.is_dir() and any(item.is_file() for item in path.iterdir())
           for path in required_resources)
assert not (pyfinder_root / "extern/shakemap-conf-eu").exists()

for path in pyfinder_root.rglob("*"):
    assert path.name not in {
        ".pyfinder_alert_config",
        ".pyfinder_alert_config.json",
        "gmt.conf",
        "gmt.history",
    }, path
    assert path.suffix.lower() not in {".db", ".log", ".sqlite", ".sqlite3"}, path

base_os = platform.freedesktop_os_release()
information = {
    "base_digest": os.environ["PYFINDER_BASE_DIGEST"],
    "base_image": "ghcr.io/sceylan/finder-base:gmt5",
    "base_os": base_os["PRETTY_NAME"],
    "paramws": {
        "commit": os.environ["PARAMWS_COMMIT"],
        "distribution_origin": str(paramws_distribution.locate_file("").resolve()),
        "module_origin": str(paramws_root),
        "version": paramws_distribution.version,
    },
    "pyfinder": {
        "distribution_origin": str(pyfinder_distribution.locate_file("").resolve()),
        "module_origin": str(pyfinder_root),
        "version": pyfinder_distribution.version,
    },
    "python_version": platform.python_version(),
}
Path("/usr/local/share/pyfinder/build-info.json").write_text(
    json.dumps(information, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PYTHON

rm -f /tmp/pyfinder-build-paramws.log
test ! -e /tmp/pyfinder-build-paramws.log
test ! -e /home/sysop/paramws.log
test ! -e /home/sysop/.pyfinder_alert_config
test ! -d /build
cat /usr/local/share/pyfinder/build-info.json
rm -rf /tmp/wheelhouse /tmp/paramws-commit
SHELL

WORKDIR /home/sysop
USER 1000:1000

ENTRYPOINT ["/usr/local/bin/pyfinder-entrypoint"]
CMD ["continuous"]
