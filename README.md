# didip

##  Lauch Static Locally
```bash
ddpa_static_fsdb_serve -host 0.0.0.0 -verbose -fsdb_root /mnt/data/full_fsdb/slices/new_archives/fsdb/ -port 7000
ddpa_static_fsdb_serve -host 0.0.0.0 -verbose -fsdb_root /mnt/data/full_fsdb/slices/illuminated/fsdb/ -port 6000
ddpa_static_fsdb_serve -host 0.0.0.0 -verbose -fsdb_root /mnt/data/full_fsdb/fsdb/ -port 5000
```


Accessing
* New [http://143.50.30.59:7000](http://143.50.30.59:7000)
* Illuminated [http://143.50.30.59:6000](http://143.50.30.59:6000)
* Full [http://143.50.30.59:5000](http://143.50.30.59:5000)

## Seed the microservice mesh (proxyless)

Microservices discover each other at runtime. Behind a reverse proxy this is automatic (they poll
the proxy's `/roster`). **Without** a proxy, seed the mesh by pushing each service's `/info`
descriptor to every peer's `PUT /<prefix>/register` — registration reciprocates, so a push from each
service makes the whole set mutually aware. Create a throwaway script and run it from a host on the
trusted loopback/subnet (all `/register` accepts):

```bash
cat > ddp_seed_mesh <<'EOF'
#!/usr/bin/env bash
# Seed the discovery mesh without a proxy. Usage: ./ddp_seed_mesh roster.json
set -euo pipefail
roster="${1:?usage: ddp_seed_mesh roster.json}"
mapfile -t entries < <(python3 -c '
import json, sys
for e in json.load(open(sys.argv[1])):
    url = e.get("base_url") or e.get("url")
    if e.get("prefix") and url:
        print(e["prefix"], url)
' "$roster")
for a in "${entries[@]}"; do
  read -r ap aurl <<<"$a"
  desc="$(curl -fsS "$aurl/$ap/info")" || { echo "skip $ap ($aurl): no /info" >&2; continue; }
  for b in "${entries[@]}"; do
    read -r bp burl <<<"$b"
    [ "$ap" = "$bp" ] && continue
    curl -fsS -X PUT "$burl/$bp/register" -H 'Content-Type: application/json' -d "$desc" >/dev/null \
      && echo "$ap -> $bp" || echo "FAIL $ap -> $bp ($burl)" >&2
  done
done
EOF
chmod +x ddp_seed_mesh

cat > roster.json <<'JSON'
[{"prefix":"st","base_url":"http://static.lan:5001"},
 {"prefix":"sl","base_url":"http://slicer.lan:5005"},
 {"prefix":"ly","base_url":"http://layout.lan:5003"}]
JSON

./ddp_seed_mesh roster.json
```

Launch each service proxyless by leaving `-proxy_url` unset — sibling hand-off links then render
absolute (each sibling's own `base_url`) automatically.
Full guide (proxied and proxyless): [`doc/proxyless_ms.md`](doc/proxyless_ms.md).

## Docker
add your deployment parameters with:
```bash
#cp .env.example .env
vim .env
```

Bring Down, Build and launch
```bash
# From inside to project root
DIDIPCV_CONTEXT=. docker compose -f docker-compose.vre.yml down && DIDIPCV_CONTEXT=. docker compose -f docker-compose.vre.yml up -d --build
```

Debugging
Look at the logs for tex module
```bash
docker compose -f docker-compose.vre.yml logs --tail=60 tex
```
