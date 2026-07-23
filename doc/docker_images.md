# Docker images

`didipcv` is the **base image** of the DiDip ecosystem. It carries the core packages (`fsdb`,
`ddp_util`, `ddp_microservices`) and the CLIs that ship with them; every third-party app image is
built `FROM` it, so the core exists once and every service shares one Flask/numpy/fargv stack.

(For host setup — installing Docker itself, the NVIDIA container toolkit — see `doc/docker.md`.)

```
                       python:3.12-slim
                              │
                       didipcv:latest          <- Dockerfile in this repo
                        │      │     │            fsdb + ddp_util + ddp_microservices
      ┌─────────────────┘      │     └─────────────────┐
      │                        │                       │
 ddpa_layout:…          ddpa_cei2json:…          ddpa_<yours>:…    <- each in its OWN repo
 (FROM didipcv)         (FROM didipcv)           (FROM didipcv)
```

## Build and run

The base image can be built from a local checkout or straight from GitHub:

```bash
docker build -t didipcv:latest .                                    # local: includes uncommitted edits
docker build -t didipcv:latest https://github.com/anguelos/didipcv.git        # from the default branch
docker build -t didipcv:latest https://github.com/anguelos/didipcv.git#v0.3.0 # from a tag
```

The git form needs **no local files whatsoever** — docker clones the ref and uses it as the build
context, so `src/`, `MANIFEST.in` and `docker/` all come from the repository. That is the
deployment path. The trade-off is that it installs what is *pushed*: while you are editing didipcv
locally, build from `.` or the image will silently lag your working tree.

`docker-compose.yml` builds from `.` (development). To make a deployment stack pull from GitHub
instead, point the build context at the repo:

```yaml
    build:
      context: https://github.com/anguelos/didipcv.git
```

but note compose still bind-mounts `./docker/roster.json`, which is deployment configuration and
stays local by design — the roster is the one file that differs per deployment.

```bash
docker build -t didipcv:latest .

DDP_FSDB_ROOT=/mnt/data/full_fsdb/fsdb \
DDP_UID=$(id -u) DDP_GID=$(id -g) \
docker compose up --build
```

Then open <http://localhost:7080/>. Only the gateway is published; `st` and `sl` are reachable
solely through it. That single origin is not cosmetic — client baskets live in OPFS, which is
per-origin, so cross-service baskets only work when everything is behind one host:port.

| variable | default | meaning |
|---|---|---|
| `DDP_FSDB_ROOT` | *(required)* | FSDB directory on the host, mounted read-only at `/mnt/data/fsdb` |
| `DDP_PUBLISHED_PORT` | `7080` | host port for the gateway |
| `DDP_UID` / `DDP_GID` | `1000` | uid:gid the containers run as; must be able to read the FSDB |
| `DDP_GATEWAY_PASSWORD` | *(empty)* | manual password gate; empty leaves the gateway open |

Ports inside the compose network are fixed at `base_port 7000` (`st` → 7001, `sl` → 7005, from
`base_port + ms_id`). They never touch the host, so they cannot collide with a 5000/6000/8080
deployment already running.

## The three GitHub packages

`deep_dataclasses`, `fargv` and `bilde` come from GitHub, not PyPI. They are built as wheels in a
first stage (`ddp-deps`) and merely installed in the base image, so `git` and the C++ toolchain
never reach the runtime image:

| package | source | notes |
|---|---|---|
| `deep_dataclasses` | `github.com/anguelos/deep_dataclasses` | pure Python; **fargv requires it** (`install_requires=["deep-dataclasses"]`) |
| `fargv` | `github.com/anguelos/fargv` | pure Python; re-exports `deep_dataclass`, `auxiliary`, `to_json_schema` |
| `bilde` | `github.com/anguelos/bilde` | C++17 pybind11 extension, compiled during the build |

Each URL is a build arg, so a fork or a pinned ref can be substituted without editing the file:

```bash
docker build -t didipcv:latest --build-arg FARGV_URL=https://github.com/you/fargv .
```

They track their default branch, so a plain rebuild reuses the cached wheel layer; use
`--no-cache` (or change a URL) to pick up an upstream push.

**Two details that are load-bearing, not stylistic:**

*They are installed before `pip install .`* — didipcv declares an unqualified `fargv` dependency,
and pip treats an unqualified requirement as satisfied by whatever is already installed. Install
them afterwards and pip would fetch `fargv` (and through it `deep-dataclasses`) from PyPI, silently
shadowing the GitHub versions. The wheels are also built with `--no-deps` for the same reason.

*bilde is built with `--no-build-isolation`* — its `pyproject.toml` `[build-system] requires` lists
`torch`, so an isolated build downloads ~2.5 GB of PyTorch purely to compile. Building inside the
stage's own environment (which has setuptools, wheel and pybind11, but no torch) makes `setup.py`
take its `except ImportError` branch:

```
PyTorch not found, skipping PyTorch extension.
Building with PyTorch support: False
```

so only the numpy/pybind11 extension `pybilde.npbilde` is compiled. **`pybilde.ptbilde` is not in
this image.** It links with `-Wl,-rpath,$ORIGIN/../torch/lib`, so enabling it means torch at build
*and* at runtime. To get it: install `torch` in the `ddp-deps` stage before the `pip wheel` line
(the CPU-only wheel index keeps that to a few hundred MB), and add `torch` to the runtime stage.

*bilde needs Boost headers* — `libboost-dev`, installed in the `ddp-deps` stage. Its C++ headers
include `<boost/math/special_functions/round.hpp>`, `<boost/algorithm/string.hpp>` and
`<boost/shared_ptr.hpp>`, and that is a **system** dependency declared nowhere in bilde's packaging,
so pip cannot pull it. All three uses are header-only and nothing links against Boost, so the
runtime stage needs no boost package.

OpenCV, libpng and libtiff appear in bilde's headers too but are **not** required: those includes
sit behind `#ifdef CV_VERSION` / `#ifdef PNG_LIBPNG_VER` / `#ifdef TIFF_VERSION`, macros that only
exist once the corresponding library header has been included — which the numpy binding never does.
Adding `libopencv-dev` would cost ~300 MB compiling nothing.

Note `bilde` is not a didipcv dependency at all — nothing in `src/` imports it; `ddpa_texture` is
its only consumer. It lives in the base image so every app inherits one consistent build.

## Two rules that containers make non-optional

**Bind to `0.0.0.0`, advertise the service name.** `bind` defaults to `127.0.0.1`, which in a
container is that container alone — the gateway could never reach it. And `host` (what the service
advertises in `/info`, to peers, and in the roster) must be a name that resolves *on the docker
network*, i.e. the compose service name. Hence every service command carries
`--bind 0.0.0.0 --host <prefix>`.

**Use `--double-dash` flags.** fargv parses argv in "unix" mode, where a single dash introduces a
cluster of *short* flags. `-base_port 7000` is therefore read as `-b -a -s -e …`, and depending on
the config class it either errors or **silently assigns the value to a different parameter**. The
worst case is `-proxy_url http://gateway:8080`, which is silently ignored: the service comes up
proxyless, never bootstraps from the roster, and renders absolute sibling links — with no error
anywhere. `--proxy_url http://gateway:8080` is correct. Genuine one-letter flags (`-v`) stay single.

## Building an app image on top

An app repo needs no copy of the core — only a Dockerfile:

```dockerfile
FROM didipcv:latest
COPY . /src/ddpa_layout
RUN pip install /src/ddpa_layout
USER didip
CMD ["ddpa_layout_serve", "--bind", "0.0.0.0", "--host", "ly", "--base_port", "7000", \
     "--fsdb_root", "/mnt/data/fsdb", "--proxy_url", "http://gateway:8080"]
```

Then add the service to `docker-compose.yml` and — this is the part that is easy to forget — to
`docker/roster.json`, whose `base_url` must be `http://<compose-service-name>:<port>`. The roster is
authoritative: the gateway routes from it, and every booting service blocks on `GET
{proxy}/roster` until it answers.

Two things to know before doing that:

- **App images are fat.** `ddpa_layout` declares `torch`, `torchvision`, `opencv-python`,
  `tensorboard`, `matplotlib`, `scipy` and `pandas` as unconditional dependencies, so its image runs
  to several GB — even though `layout_service.py` and `layout_index.py` import none of them (the
  serve mode only reads precomputed `.layout.pred.json` files). Moving the heavy ones into an
  `optional-dependencies` extra would let serve images install `.[serve]` and shrink accordingly.
- **Not every app is a service.** `ddpa_texture` and `ddpa_img_preprocessing` expose no `*_serve`
  entry point; they are batch/offline tools. They belong in run-to-completion containers with the
  FSDB mounted **read-write** (offline mode writes into charter directories), ideally under a
  separate compose profile so they never start alongside the web stack.

## Debugging inside a container

The base image carries a full toolkit, so every container is debuggable in place: `lynx`,
`links2`, `mc`, `vim`, `less`, `file`, `tree`; `curl`, `wget`, `jq`; `iproute2`, `iputils-ping`,
`dnsutils`, `netcat-openbsd`; `procps`, `htop`, `lsof`, `strace`; plus `ipython`, `flasgger` and
`pytest` from pip.

Two notes on that list:

- **`ipython` comes from pip, never apt.** Debian's `ipython3` is built against Debian's own
  python3.11 and would install a second interpreter that cannot import `fsdb`,
  `ddp_microservices` or `fargv`.
- **`flasgger` switches on `/documentation/`.** It is didipcv's soft dependency, lazily imported;
  installing it here is what makes the Swagger UI appear in the containers.
- **Text browsers cannot exercise the UI.** `lynx`/`links2` verify routing, headers and rendered
  HTML, but the basket, the context rail and the shared-index sync are ES modules plus OPFS —
  none of which a text browser runs. Use `curl` in the container and a real browser on the host.

The most useful first commands when something is wrong:

```bash
getent hosts st                       # does the service name resolve on the docker network?
ss -ltn                               # did it really bind 0.0.0.0 and not 127.0.0.1?
curl -s http://gateway:8080/roster | jq
curl -s http://st:7001/st/health
```

### Shell access

`docker compose exec st bash` needs nothing extra and is the quickest route.

The image also runs an **sshd on port 2222**, key-only, with `docker/authorized_keys` installed for
both `root` and `didip` (who has passwordless sudo). Passwords are disabled everywhere. compose
publishes it per service, bound to **loopback only** so it is never reachable off-host:

| service | ssh |
|---|---|
| gateway | `ssh -p 7020 root@127.0.0.1` |
| st | `ssh -p 7021 root@127.0.0.1` |
| sl | `ssh -p 7025 root@127.0.0.1` |

**sshd only starts when the container runs as root**, because privilege separation, the host keys
and `/run/sshd` all require it. The compose default is `user: 1000:1000`, so by default you get the
warning in the logs and no sshd. To use ssh:

```bash
DDP_USER=0:0 docker compose up
```

The entrypoint never lets an sshd failure stop the service — it warns and carries on, and `exec`s
the service so it stays PID 1 and still handles `docker stop` cleanly. `DDP_SSHD=0` disables it
entirely. Host keys are baked in at build time, so all these containers share them: ssh will
complain about a changed host key when you point the same port at a different service.

## Cloudflare tunnel

Two opt-in profiles put the gateway on the public internet without forwarding a port:

```bash
docker compose --profile tunnel up                       # ephemeral trycloudflare.com URL, no account
CLOUDFLARE_TUNNEL_TOKEN=… docker compose --profile tunnel-token up   # your named tunnel
```

### Passing the token

The token reaches cloudflared through `TUNNEL_TOKEN` in the service's `environment:` — never the
command line, where it would be visible in `ps` and `docker inspect`. Three ways to supply it, in
increasing order of hygiene:

```bash
CLOUDFLARE_TUNNEL_TOKEN=eyJ… docker compose --profile tunnel-token up   # inline: goes into shell history
export CLOUDFLARE_TUNNEL_TOKEN=eyJ…                                    # session-wide
cp .env.example .env && chmod 600 .env                                 # preferred
```

`.env` is read by compose automatically and is **gitignored**, so `docker compose --profile
tunnel-token up` then needs no environment at all. `.env.example` documents every variable and is
the file that is committed. `.env` is also excluded from the docker build context, so it can never
be captured into an image layer.

An empty/absent token is not a startup error for the stack as a whole — the variable defaults to
empty so plain `docker compose up` still works — cloudflared itself reports the missing token when
the profile is actually used.

The sidecar dials out to Cloudflare and reaches the gateway at `http://gateway:8080` over the
private network. Neither profile starts with a plain `docker compose up`.

### A tunnel needs `trusted_proxy_cidr`

By default the gate whitelists `remote_addr` and **ignores** `CF-Connecting-IP` /
`X-Forwarded-For` — trusting those from an arbitrary peer would let anyone whitelist themselves
with one header. Behind a tunnel that default is wrong in a specific way: every visitor arrives
wearing the *sidecar's* address, so one correct password admits everybody, and because the sidecar
sits on the docker bridge it is also **`_is_local()`**, so the `/health` + `/roster` exemption
(which exists so microservices can boot) is inherited by the whole internet — and `/roster`
discloses every backend's internal URL.

Set it to the network the sidecar is on:

```bash
DDP_TRUSTED_PROXY_CIDR=172.16.0.0/12 \
CLOUDFLARE_TUNNEL_TOKEN=… docker compose --profile tunnel-token up
```

Measured, gate on, request arriving via the sidecar with `CF-Connecting-IP: 203.0.113.99`:

| route | default | with `trusted_proxy_cidr` |
|---|---|---|
| `/roster` | **200 — public** | 303 gated |
| `/health` | **200 — public** | 303 gated |
| `/` | 303 gated | 303 gated |
| `/roster` *from a sibling container* | 200 boot OK | 200 boot OK |

How it resolves the address, only for peers inside the CIDR: `CF-Connecting-IP` first (Cloudflare
**overwrites** it at the edge, so a client-supplied value is discarded — unlike XFF it is one
unambiguous address), else the **last** `X-Forwarded-For` entry (what the trusted neighbour
appended; the first entry is whatever the client claimed). The value must parse as an IP, so a
broken proxy cannot inject junk into the whitelist. A peer *outside* the CIDR is still read from
`remote_addr`, so forging the header from the internet changes nothing. It assumes **one** trusted
hop — a longer chain needs a hop count, not this.

Accepts several networks: `--trusted_proxy_cidr "172.16.0.0/12, 10.0.0.0/8 192.0.2.7"`. A malformed
value raises at startup rather than silently trusting nothing.

**Even so, this is a weak control for a public deployment** — one shared password, addresses that
move with mobile networks and CGNAT, and anyone with the URL may try. For anything genuinely public
use **Cloudflare Access**: it authenticates at the edge, so unauthenticated requests never reach
the tunnel, and the password gate can be left off entirely.

Also worth knowing: a tunnel exposes the *gateway*, which fronts every service. Anything reachable
at `/<prefix>/…` becomes reachable publicly, including the Slicer's download endpoints.

## The FSDB mount

Serving containers mount the FSDB **read-only** (`:ro`) because an online service is defined never
to write into it. Offline/compute containers are the exception and need `:rw`.

The bind-mount keeps the host's uid/gid, so the container user must be able to read it — hence
`DDP_UID`/`DDP_GID`. Getting this wrong shows up as a service that starts and then serves an empty
database, rather than as a permission error.

## The password gate and client addresses

`DDP_GATEWAY_PASSWORD` enables the gateway's manual gate: the first correct password from an
address whitelists that address until the gateway restarts. `/health` and `/roster` stay reachable
without it from loopback and private ranges (including Docker's `172.16/12` bridges) — they must,
or no service could ever boot.

The gate keys on `request.remote_addr` and deliberately ignores `X-Forwarded-For` (honouring it
would let anyone forge a whitelisted address). The consequence in Docker: if traffic reaches the
gateway through another hop — a reverse proxy, an SSH tunnel, or Docker's userland proxy on some
setups — every client can arrive wearing the *same* address, and one correct password admits all of
them. The gateway prints a loud warning to stderr the first time it sees an `X-Forwarded-For`
header, which is exactly that situation. For a real deployment, terminate TLS and authenticate in a
front proxy instead.

## Notes

- `libmagic1` is installed via apt because `python-magic` is a ctypes binding to the system library;
  without it `import magic` fails at startup.
- `flasgger` (the `/documentation/` Swagger UI) is a **soft** dependency, lazily imported and
  skipped when absent. It is not in `install_requires`, so the image has no Swagger UI unless you
  add it.
- The image ends as `root` so derived images can `pip install` freely; the compose file drops
  privileges with `user:`, and a leaf app image can do the same with `USER didip`.
