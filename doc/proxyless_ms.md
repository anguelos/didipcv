# Running DiDip microservices — with or without a proxy

DiDip microservices are decentralized: each is launched independently, owns a short URL **prefix**
(`^[a-z][a-z0-9_]*$`, e.g. `st`, `sl`, `ly`), and they **discover each other at runtime** rather
than from a central config. This document explains how to launch a set of services and have them
learn of one another — both **behind a reverse proxy** and **proxyless**.

## What each service exposes for discovery

All under the service's own prefix `/<prefix>/…`:

| Endpoint | Purpose |
|---|---|
| `GET /<prefix>/health` | liveness (`"OK", 200`); polled by peers every ~15 s. |
| `GET /<prefix>/info` | the **descriptor**: `{prefix, base_url, name, icon, launch_cmd, views}`. |
| `PUT /<prefix>/register` | a peer announces itself (pushes its descriptor). **PUT-only**, **loopback / trusted-subnet only**, and **never forwarded by a proxy**. Registering a new peer triggers a one-time reciprocal registration, so both sides end up knowing each other. `GET` shows a manual debug form. |

A service keeps a **runtime registry** (`prefix → descriptor`) and a live subset (pruned by the
health poll). The registry is what the topnav's cross-service hand-off links are built from.

## Mode A — behind a proxy (recommended)

A reverse proxy makes everything **one origin**, which is required for a **shared basket** (OPFS is
per-origin) and is the simplest way to bootstrap discovery. The proxy is deployment-configured with
every service and serves `GET /roster` (`[{prefix, base_url}]`).

Using the bundled dev gateway:

```bash
# 1. a roster the gateway fronts
cat > roster.json <<'JSON'
[{"prefix":"st","base_url":"http://127.0.0.1:5001","launch_cmd":"ddpa_static_fsdb_serve"},
 {"prefix":"sl","base_url":"http://127.0.0.1:5005","launch_cmd":"ddpa_slicer_serve"},
 {"prefix":"ly","base_url":"http://127.0.0.1:5003","launch_cmd":"ddpa_layout_serve"}]
JSON

# 2. the gateway (single origin at :8080; never forwards PUT)
ddp_gateway -roster roster.json -ms_health_freq 30 &

# 3. each service, told where the rendezvous proxy is (it polls /roster, then registers with peers)
ddpa_static_fsdb_serve -proxy_url http://127.0.0.1:8080 &
ddpa_slicer_serve       -proxy_url http://127.0.0.1:8080 &
ddpa_layout_serve       -proxy_url http://127.0.0.1:8080 &
```

Each service polls the gateway's `/roster` on startup, then `PUT`s its descriptor to every peer;
reciprocation fills in the rest. Sibling links stay **root-relative** (`/<prefix>/…`), so the whole
VRE is one origin and the basket is shared. Browse `http://127.0.0.1:8080/st/`.

For nginx/Caddy instead of the dev gateway, see the snippets in `gateway.py`'s module docstring —
they must also **refuse to forward PUT** and **serve `/roster`**.

## Mode B — proxyless (each service on its own port)

No proxy: each service is its **own origin** (`host:port`). Discovery is seeded by hand, and sibling
links must be **absolute** (to each sibling's own host) for hand-off to work. Leaving `proxy_url`
empty (no gateway to bootstrap from) is exactly what selects absolute links — nothing else to set.

```bash
# launch each service directly (bind to a reachable interface if not same-host).
# No -proxy_url -> proxyless -> absolute sibling links (derived automatically).
ddpa_static_fsdb_serve -bind 0.0.0.0 &
ddpa_slicer_serve       -bind 0.0.0.0 &
ddpa_layout_serve       -bind 0.0.0.0 &

# seed the mesh from a roster (each service's /info is pushed to every peer's /register;
# registration reciprocates, so the whole set becomes mutually aware). Run from a trusted host.
# Create the throwaway `ddp_seed_mesh` helper from the snippet in the README
# ("Seed the microservice mesh (proxyless)"), then:
cat > roster.json <<'JSON'
[{"prefix":"st","base_url":"http://static.lan:5001"},
 {"prefix":"sl","base_url":"http://slicer.lan:5005"},
 {"prefix":"ly","base_url":"http://layout.lan:5003"}]
JSON
./ddp_seed_mesh roster.json
```

(`ddp_seed_mesh` just pushes each service's own `/info` descriptor to every peer's `/register`;
because registration reciprocates, this makes the whole mesh mutually aware. It is a throwaway
snippet — see the [README](../README.md), not an installed command.)

## `proxy_url` decides sibling-link rendering

`proxy_url` does double duty: it is the rendezvous URL a service polls to bootstrap discovery, **and**
it selects how the topnav renders **sibling hand-off links** (via the derived `absolute_links`).
These two concerns coincide in both canonical deployments — with a gateway you set `proxy_url`; without
one you leave it empty — so there is a single knob:

| `proxy_url` | `absolute_links` | sibling link | when |
|---|---|---|---|
| **set** (a gateway URL) | false | root-relative `/<sibling_prefix>/…` | behind a single-origin front proxy (shared basket) |
| **empty** (default) | true | absolute `{sibling_base_url}/<sibling_prefix>/…` | proxyless — each service on its own host:port |

So the same service image works in both modes with nothing extra to configure: set `-proxy_url <gw>`
behind a gateway, or leave it empty to run proxyless. Either way hand-off links resolve to a service
that actually
serves that prefix. (Proxyless means baskets are **not** shared across origins; transfer them with
the basket copy/paste feature instead.)

**Edge case — external nginx/Caddy with hand-seeded discovery.** If you front the services with a
non-Python proxy (single origin) but seed discovery by hand rather than polling a Python gateway,
still set `-proxy_url` to that front proxy's URL (it serves `/roster` anyway — see `gateway.py`).
That keeps links root-relative, matching the single origin. The rule is simply: **`proxy_url` set ⇔
there is one front origin ⇔ root-relative links.**

## Security

`PUT /<prefix>/register` is accepted only from **loopback** or a configured
**`register_trusted_cidr`**, and a proxy **must never forward PUT** — so registration is reachable
only by services on the trusted network, never by outside clients through the gateway. Keep backend
ports on loopback / a private interface; expose only the proxy publicly.

See also: the `ddp_online` skill (discovery contract), `gateway.py` (proxy config), and
`.claude/status.md` (roadmap).
