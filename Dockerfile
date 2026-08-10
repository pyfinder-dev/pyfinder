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
ARG PYFINDER_BASE_DIGEST=sha256:2102c2b4609ae1496e00a022f3e30d9a995b7a33924b1e13a5582eaa86ffaf1b

LABEL org.opencontainers.image.base.name="ghcr.io/sceylan/finder-base:gmt5" \
      io.pyfinder.base.digest="${PYFINDER_BASE_DIGEST}" \
      io.pyfinder.python.version="${PYTHON_VERSION}" \
      io.pyfinder.build-information="/usr/local/share/pyfinder/build-info.json"

COPY --from=build /opt/python-3.12 /opt/python-3.12
COPY --from=build /wheelhouse /tmp/wheelhouse
COPY --from=build /build/paramws-commit /tmp/paramws-commit

ENV PATH="/opt/python-3.12/bin:${PATH}"
ENV PYTHONNOUSERSITE=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN python3.12 -m pip install \
        --no-cache-dir \
        --no-index \
        --find-links /tmp/wheelhouse \
        pyfinder==1.0.0 \
        paramws-clients==0.1.0 \
    && mkdir -p /usr/local/share/pyfinder \
    && PARAMWS_COMMIT="$(cat /tmp/paramws-commit)" python3.12 -c \
        'import importlib.metadata as metadata; import json; import os; import pathlib; import platform; import paramws; import pyfinder; pyfinder_root = pathlib.Path(pyfinder.__file__).resolve().parent; paramws_root = pathlib.Path(paramws.__file__).resolve().parent; assert "site-packages" in pyfinder_root.parts; assert "site-packages" in paramws_root.parts; assert not any(part in {"build", "paramws-clients"} for part in pyfinder_root.parts + paramws_root.parts); information = {"base_digest": "sha256:2102c2b4609ae1496e00a022f3e30d9a995b7a33924b1e13a5582eaa86ffaf1b", "base_image": "ghcr.io/sceylan/finder-base:gmt5", "base_os": "Debian GNU/Linux 10 (buster)", "paramws": {"commit": os.environ["PARAMWS_COMMIT"], "distribution_origin": str(metadata.distribution("paramws-clients").locate_file("").resolve()), "module_origin": str(paramws_root), "version": metadata.version("paramws-clients")}, "pyfinder": {"distribution_origin": str(metadata.distribution("pyfinder").locate_file("").resolve()), "module_origin": str(pyfinder_root), "version": metadata.version("pyfinder")}, "python_version": platform.python_version()}; pathlib.Path("/usr/local/share/pyfinder/build-info.json").write_text(json.dumps(information, indent=2, sort_keys=True) + "\n", encoding="utf-8")' \
    && test "$(python3.12 -c 'import platform; print(platform.python_version())')" = "${PYTHON_VERSION}" \
    && test "$(python3 -c 'import platform; print(platform.python_version())')" = "${PYTHON_VERSION}" \
    && ! command -v python3.9 \
    && test "$(getent passwd 1000 | cut -d: -f1,3,4)" = "sysop:1000:1000" \
    && test "$(getent group 1000 | cut -d: -f1,3)" = "sysop:1000" \
    && command -v pyfinder \
    && pyfinder --help > /dev/null \
    && test -x /usr/local/src/FinDer/finder_run \
    && test -x /usr/local/src/FinDer/finder_create_mask \
    && python3.12 -c \
        'import pathlib; import pyfinder; root = pathlib.Path(pyfinder.__file__).resolve().parent; required = (root / "extern/finder_regional_wkt", root / "extern/ne_110m_admin_0_countries"); assert all(path.is_dir() and any(item.is_file() for item in path.iterdir()) for path in required); assert not (root / "extern/shakemap-conf-eu").exists()' \
    && cat /usr/local/share/pyfinder/build-info.json \
    && rm -rf /tmp/wheelhouse /tmp/paramws-commit

WORKDIR /home/sysop
USER 1000:1000

ENTRYPOINT ["pyfinder"]
CMD ["continuous"]
