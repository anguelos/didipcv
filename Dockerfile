# syntax=docker/dockerfile:1
#
# didipcv -- the BASE image of the DiDip ecosystem.
#
# It carries the core (`fsdb`, `ddp_util`, `ddp_microservices`) and the three CLIs that ship with
# it: ddp_gateway, ddpa_static_fsdb_serve, ddpa_slicer_serve. It also carries the three packages
# that are installed from GITHUB rather than PyPI -- deep_dataclasses, fargv and bilde (a C++17
# pybind11 extension) -- so every app image inherits one consistent set of them. Every third-party
# app image (ddpa_layout, ddpa_cei2json, ddpa_texture, ...) is built FROM this one, so the core
# exists exactly once and every service inherits the same Flask/numpy/fargv stack:
#
#     FROM didipcv:latest
#     COPY . /src/ddpa_layout
#     RUN pip install /src/ddpa_layout
#     CMD ["ddpa_layout_serve", "-bind", "0.0.0.0", "-host", "ly", "-proxy_url", "http://gateway:8080"]
#
# Deliberately ends as ROOT so a derived image can `pip install` without a USER dance. Drop
# privileges in the final image (`USER didip`) or, as docker-compose.yml here does, with `user:`.
#
# NOTE the toolchain is confined to the first stage, so a derived image that needs to COMPILE
# something (a dependency with no wheel) must install build-essential itself.
#
# Build from a local checkout (development -- picks up uncommitted edits):
#         docker build -t didipcv:latest .
#         docker build -t didipcv:latest --build-arg FARGV_URL=https://github.com/you/fargv .
#
# Build straight from GitHub (deployment -- needs NO local files at all; docker fetches the repo
# and uses it as the build context, so src/, MANIFEST.in and docker/ all come from the ref):
#         docker build -t didipcv:latest https://github.com/anguelos/didipcv.git
#         docker build -t didipcv:latest https://github.com/anguelos/didipcv.git#v0.3.0
# Note this installs what is PUSHED, not what is in your working tree.
#
# The three GitHub packages track their default branch, so `--no-cache` (or a changed *_URL) is what
# picks up an upstream push; a plain rebuild reuses the cached wheel layer.

# ---------------------------------------------------------------------------------------------
# Stage 1 -- build wheels for the three packages that come from GitHub rather than PyPI.
#
# They are built HERE and merely installed in the runtime stage, so the compiler, the headers and
# git never reach the final image. `bilde` is the reason: it is a C++17 pybind11 extension.
# ---------------------------------------------------------------------------------------------
FROM python:3.12-slim AS ddp-deps

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# libboost-dev: bilde's C++ headers include <boost/math/special_functions/round.hpp>,
# <boost/algorithm/string.hpp> and <boost/shared_ptr.hpp> (from include/wrapping_api.hpp and
# include/util/argv.hpp, both unconditionally included by bilde.hpp). It is a SYSTEM dependency
# declared nowhere in bilde's packaging, so pip cannot pull it. All three uses are header-only --
# nothing links against Boost, which is why the runtime stage needs no boost package at all.
#
# NOT needed, despite appearing in bilde's headers: OpenCV, libpng and libtiff. Their includes sit
# behind `#ifdef CV_VERSION` / `#ifdef PNG_LIBPNG_VER` / `#ifdef TIFF_VERSION`, and those macros
# are only defined once the corresponding library header has been included -- which the numpy
# binding never does. Adding libopencv-dev here would cost ~300MB for code that is compiled out.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git libboost-dev \
 && rm -rf /var/lib/apt/lists/*

# bilde's setup.py does `import pybind11` at module level and compiles with -std=c++17; setuptools
# and wheel are the rest of what a --no-build-isolation build needs. (numpy is a RUNTIME dependency
# of bilde, not a build one -- its setup.py never imports it.)
RUN --mount=type=cache,target=/root/.cache/pip pip install setuptools wheel pybind11

ARG DEEP_DATACLASSES_URL=https://github.com/anguelos/deep_dataclasses
ARG FARGV_URL=https://github.com/anguelos/fargv
ARG BILDE_URL=https://github.com/anguelos/bilde

# --no-deps everywhere: we want wheels for exactly these three, and nothing else pulled in from
# PyPI at this point. In particular fargv REQUIRES `deep-dataclasses`, and without --no-deps pip
# would satisfy that from PyPI instead of from the GitHub checkout we are building here.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --no-deps -w /wheels "git+${DEEP_DATACLASSES_URL}" "git+${FARGV_URL}"

# bilde: --no-build-isolation is deliberate and load-bearing. Its pyproject [build-system] requires
# lists `torch`, so an isolated build would download ~2.5GB of PyTorch merely to compile. Building
# in THIS environment (which has no torch) makes setup.py take its `except ImportError` branch --
# it prints "PyTorch not found, skipping PyTorch extension" and builds only the numpy/pybind11
# extension `pybilde.npbilde`. The torch-backed `pybilde.ptbilde` is therefore NOT in this image;
# it is rpath'd to $ORIGIN/../torch/lib and would need torch installed at runtime too.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --no-deps --no-build-isolation -w /wheels "git+${BILDE_URL}"


# ---------------------------------------------------------------------------------------------
# Stage 2 -- the base image itself.
# ---------------------------------------------------------------------------------------------
FROM python:3.12-slim AS didipcv-base

# 3.12 matches the interpreter the project is developed against; the code uses PEP 604 unions
# (`str | None`) and builtin generics throughout, so 3.10 is the hard floor. It must also match the
# stage above: bilde's extension is compiled against this exact CPython ABI.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libmagic1 is NOT optional: `python-magic` is a ctypes binding to the system library and the wheel
# does not bundle it, so `import magic` fails at startup without this. Everything else in
# install_requires (numpy, Pillow, lxml, ...) ships manylinux wheels.
# libstdc++6 is what bilde's compiled extension links against (it is normally already present in
# the debian base; named explicitly so it can never be dropped by accident).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagic1 libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

# The GitHub packages, from the wheels built in stage 1. This MUST precede the didipcv install
# below: didipcv depends on `fargv`, and an unqualified requirement is satisfied by whatever is
# already installed -- so installing these first is what stops pip from pulling fargv (and,
# through it, deep-dataclasses) from PyPI and silently overriding the GitHub versions.
# Their own PyPI dependencies (numpy, scikit-learn, Pillow, pybind11) are resolved normally here.
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,from=ddp-deps,source=/wheels,target=/wheels \
    pip install /wheels/*.whl

# PyTorch -- installed ON DEMAND, in the base so every image inherits it when enabled.
#   WITH_TORCH=      (default) torch NOT installed -- keeps the image ~200MB smaller
#   WITH_TORCH=1               install torch (from TORCH_INDEX_URL)
# TORCH_INDEX_URL picks the build:
#   CPU-only (default): https://download.pytorch.org/whl/cpu  -- ~200MB, no GPU
#   CUDA 12.1         : https://download.pytorch.org/whl/cu121 -- ~2.5GB, needs the nvidia runtime
#   pinned versions   : TORCH_SPEC="torch==2.3.1 torchvision==0.18.1"
#
# NOTE cei2json currently NEEDS this on: its dependency `pylelemmatize` imports torch
# unconditionally from its __init__ (phoc / substitution_augmenter), so `import pylelemmatize`
# fails without torch and the cei service exits at startup. Build with WITH_TORCH=1 until that
# import is guarded upstream.
ARG WITH_TORCH=
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_SPEC="torch torchvision"
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "${WITH_TORCH}" ]; then \
        echo "[didipcv] installing torch from ${TORCH_INDEX_URL}: ${TORCH_SPEC}"; \
        pip install --index-url ${TORCH_INDEX_URL} ${TORCH_SPEC}; \
    else \
        echo "[didipcv] torch NOT installed (build with --build-arg WITH_TORCH=1; cei2json needs it)"; \
    fi

# The unprivileged runtime identity, created BEFORE the ssh setup below (which installs a key into
# its home) and before anything chowns to it. uid 1000 is the common default for a single-user
# host; the FSDB bind-mount must be readable by whatever uid actually runs (see
# doc/docker_images.md).
RUN useradd --create-home --uid 1000 --shell /bin/bash didip

# ---- interactive debugging toolkit --------------------------------------------------------
# In the BASE image by request, so every container (gateway, st, sl, and every app built FROM
# this) is debuggable in place. It costs a few hundred MB everywhere and puts editors, browsers
# and an ssh daemon into web-facing containers -- the trade the `didipcv:debug` split would have
# avoided. Grouped in one layer so it can be deleted in one edit.
#
# NOTE: ipython/flasgger/pytest are installed with PIP, NOT apt. Debian's `ipython3` package is
# built against Debian's own python3.11 and would install a SECOND interpreter that cannot see
# fsdb/ddp_microservices/fargv at all -- the exact opposite of a debugging tool.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      lynx links2 mc vim less file tree \
      curl wget jq \
      iproute2 iputils-ping dnsutils netcat-openbsd \
      procps htop lsof strace \
      openssh-server sudo \
 && rm -rf /var/lib/apt/lists/*

# flasgger is didipcv's SOFT dependency: microservice.py imports it lazily, so installing it here
# is what switches on the /documentation/ Swagger UI in these containers.
RUN --mount=type=cache,target=/root/.cache/pip pip install ipython flasgger pytest

# ---- ssh (key-only, port 2222) ------------------------------------------------------------
# Host keys are generated at BUILD time, so every container from this image shares them. That is
# fine for a debug shell on a private network, but it means the images are not distinguishable by
# host key -- expect ssh's known_hosts to complain when you point the same port at another service.
COPY docker/sshd_config /etc/ssh/sshd_config
COPY docker/authorized_keys /etc/ddp/authorized_keys
RUN ssh-keygen -A \
 && mkdir -p /root/.ssh /home/didip/.ssh \
 && cp /etc/ddp/authorized_keys /root/.ssh/authorized_keys \
 && cp /etc/ddp/authorized_keys /home/didip/.ssh/authorized_keys \
 && chmod 700 /root/.ssh /home/didip/.ssh \
 && chmod 600 /root/.ssh/authorized_keys /home/didip/.ssh/authorized_keys \
 && chown -R didip:didip /home/didip/.ssh \
 && echo 'didip ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/didip \
 && chmod 440 /etc/sudoers.d/didip

COPY --chmod=0755 docker/ddp-entrypoint.sh /usr/local/bin/ddp-entrypoint.sh
# Starts sshd (when the container runs as root) and then execs the CMD, so the service stays PID 1.
# A derived image that sets its own ENTRYPOINT loses the sshd; override CMD instead.
ENTRYPOINT ["/usr/local/bin/ddp-entrypoint.sh"]

WORKDIR /src
# One COPY + one install: the dependency set lives in setup.py and is NOT restated here (restating
# it is how a Dockerfile silently drifts from the package). The cost is that touching any source
# file invalidates the install layer -- which the BuildKit pip cache mount below makes cheap, since
# the wheels are never re-downloaded.
# MANIFEST.in is REQUIRED here, not decorative: it is what carries src/ddp_microservices/static
# (the stylesheet, the ES modules, the icons) into the installed package. Omit it and the build
# still succeeds, the pages still render -- and every CSS/JS request 404s, because the wheel has
# templates but no static/. (Templates sneak in from a stale src/*.egg-info/SOURCES.txt, which is
# why the failure looks like "only the styling is missing" rather than an obvious error.)
COPY setup.py MANIFEST.in README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip pip install .

# `/documentation/` (the Swagger UI) needs `flasgger`, which is a SOFT dependency: microservice.py
# imports it lazily and simply skips the docs when it is absent. It is not in install_requires, so
# it is not installed here either -- add it deliberately if you want Swagger in the containers.

# (the `didip` user is created further up, before the ssh setup that writes into its home)

# Where every service looks for the database (config_ms.GlobalConfig.fsdb_root default), so a
# bind-mount here needs no flag. Mount it READ-ONLY for serving containers: online services are
# defined never to write into the FSDB.
VOLUME ["/mnt/data/fsdb"]

WORKDIR /home/didip

LABEL org.opencontainers.image.title="didipcv" \
      org.opencontainers.image.description="DiDip core: fsdb, ddp_util, ddp_microservices + base image for DiDip app services" \
      org.opencontainers.image.source="https://zimlab.uni-graz.at/gams/projects/didip/general" \
      org.opencontainers.image.licenses="GPL-3.0"

# No service is the "default" one -- the image is a base. Say what it holds instead of guessing.
CMD ["python", "-c", "print('didipcv base image.\\nCLIs: ddp_gateway, ddpa_static_fsdb_serve, ddpa_slicer_serve, ddp_slice_fsdb\\nPick one as the command, or build a service image FROM this one (see doc/docker_images.md).')"]


# ---------------------------------------------------------------------------------------------
# Stage 3 (optional target) -- didip-vre: the WHOLE VRE in one image.
#
# The base above is the composable path (one image per app, each in its own repo). This target is
# the DEMO path: every service in a single image, so the deployment is one build, one compose file
# and one config file. Deliberately gives up per-app rebuilds and dependency isolation.
#
#     docker build --target didip-vre -t didip-vre:latest .
#     docker build --target didip-vre -t didip-vre:latest https://github.com/anguelos/didipcv.git
#     docker compose -f docker-compose.vre.yml up -d --build
#
# The apps are installed AFTER didipcv (stage 2), so their `didip_util` requirement is already
# satisfied and pip does not fetch it from PyPI, shadowing the local build. Same ordering rule as
# fargv in the base.
# ---------------------------------------------------------------------------------------------
FROM didipcv-base AS didip-vre

# Refs default to EMPTY, meaning "clone the repo's own default branch" -- which differs per repo
# (ddpa_layout is on `master`, the others on `main`). Pinning `@main` for everything failed on
# layout with `git checkout -q main did not run successfully`. Set a ref to a tag/branch/sha to
# pin; leave it empty to track whatever the repo's default branch is.
ARG LAYOUT_URL=https://github.com/anguelos/ddpa_layout
ARG LAYOUT_REF=
ARG CEI_URL=https://github.com/anguelos/ddpa_cei2json
ARG CEI_REF=
ARG TEXTURE_URL=https://github.com/anguelos/ddpa_texture
ARG TEXTURE_REF=

# `ddpa_layout[serve]` (PEP 508 form) installs the serving set only -- the YOLOv5 stack lives in
# ddpa_layout's `ml` extra and would add several GB for code the serving path never imports.
#
# ddpa_texture brings `ddpa_texture_serve` (TextureMicroservice, a @scoped_ms
# SharedIndexMicroservice owning the `tex` prefix, ms_id 7 -> port 7007) plus its compute CLIs.
# It depends on `bilde`, which is why the base image carries it. NOTE its setup.py does not declare
# `didip_util` / `flask` although it imports ddp_microservices, ddp_util and flask -- that works
# here only because didipcv is installed first, in the stage above.
#
# git is not in this stage, so pip needs it: installed and removed in one layer to keep it out of
# the image.
# `${REF:+@${REF}}` appends `@<ref>` only when the ref is non-empty, so an empty ref clones the
# repo's default branch (no bogus trailing `@`). `ddpa_layout[serve]` (PEP 508 form) installs the
# serving set only -- the YOLOv5 stack lives in ddpa_layout's `ml` extra.
RUN --mount=type=cache,target=/root/.cache/pip \
    apt-get update && apt-get install -y --no-install-recommends git \
 && pip install "ddpa_layout[serve] @ git+${LAYOUT_URL}${LAYOUT_REF:+@${LAYOUT_REF}}" \
                "git+${CEI_URL}${CEI_REF:+@${CEI_REF}}" \
                "git+${TEXTURE_URL}${TEXTURE_REF:+@${TEXTURE_REF}}" \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# The texture model bundle, FETCHED FROM GITHUB into the image. It lives at the repo's tmp/, outside
# the package dir, so `pip install` never ships it -- but it IS committed, so GitHub's raw endpoint
# serves it. Baking it in is what lets a deployment depend on nothing but .env (no host file to
# mount). Same ref as the pip install above, so code and bundle stay consistent; `raw/HEAD/...`
# resolves the default branch when TEXTURE_REF is empty. curl comes from the base image's toolkit.
ARG TEXTURE_BUNDLE_REPO_PATH=tmp/texture_model_bundle.pkl
RUN curl -fsSL -o /opt/texture_model_bundle.pkl \
      "${TEXTURE_URL}/raw/${TEXTURE_REF:-HEAD}/${TEXTURE_BUNDLE_REPO_PATH}" \
 && chmod 0644 /opt/texture_model_bundle.pkl

# Baked in, NOT bind-mounted: with every service in one image the topology is fixed, so the roster
# is part of the artefact and the deployment needs exactly ONE external file (the .env).
COPY docker/roster.json /etc/didip/roster.json

LABEL org.opencontainers.image.title="didip-vre" \
      org.opencontainers.image.description="Whole DiDip VRE in one image: core + static + slicer + layout + cei2json"

# The gateway is the sensible default: `docker run didip-vre` then serves the manifest.
CMD ["ddp_gateway", "--bind", "0.0.0.0", "--gateway_port", "8080", \
     "--roster", "/etc/didip/roster.json", "--ms_health_freq", "15"]
