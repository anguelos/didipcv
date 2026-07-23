import os

from fargv import deep_dataclass, auxiliary, FargvAutoConfig, FargvInt


@deep_dataclass
class DdpMsConfigs:
    @auxiliary
    class GlobalConfig(FargvAutoConfig):
        fsdb_root: str = "/mnt/data/fsdb"

    @auxiliary
    class Microservice(GlobalConfig):
        base_port: int = 5000
        microservice_monitor_interval: int = 10
        host: str = "localhost"
        monitor_frequency: int = 15
        "seconds between /health polls of known siblings (liveness pruning of the runtime registry)."
        ip: str = "0.0.0.0"
        # socket LISTEN address. Safe default: loopback only (reachable via a same-host gateway).
        # Override per deployment (e.g. a private interface behind a Proxmox/LAN reverse proxy).
        bind: str = "127.0.0.1"
        proxy_url: str = ""
        "rendezvous proxy base URL. MUST be a full http(s)://host[:port] URL (a scheme is required -- validated at startup, raises on a scheme-less host:port); e.g. http://localhost:8080. Empty -> no auto-bootstrap; seed the mesh with a curl script instead. ALSO decides sibling-link rendering: set -> single origin, root-relative /<prefix>/... links; empty -> proxyless, absolute links from each sibling's base_url. See doc/proxyless_ms.md and [[ddp_online]] discovery."
        proxy_poll_seconds: float = 1.0
        "how often to poll for the proxy at startup until its /roster answers."
        register_trusted_cidr: str = ""
        "extra subnet (CIDR) allowed to PUT /<prefix>/register, beyond loopback. Empty -> loopback only."
        protocol: str = "http://"
        verbosity: int = FargvInt(0, short_name="v", is_count_switch=True)
        "-v: load timing messages  -vv: adds tqdm progress bars over the FSDB walk."
        monitor_level: int = 1
        "request-count monitoring for /health_report: 0 off (no per-request hook), 1 grouped by 2nd-level route (IIIF/static merged), 2 per exact route."
        workers: int = os.cpu_count() or 4
        "worker processes for the load-time FSDB scan/reduce (CPU-bound: JSON parsing). Default: one per core; measured to plateau at cpu_count and degrade beyond it. 1 = serial, no pool."
        @property
        def port(self):
            return self.base_port + self.ms_id
        
        @property
        def url(self):
            return f"{self.protocol}{self.host}:{self.port}"

    # route_prefix: the single path segment each service OWNS behind a single-origin gateway
    # (routes are `/<route_prefix>/...`); also the client OPFS index-cache namespace. launch_cmd:
    # the CLI to (re)start the service, surfaced when a sibling is found down. Both live on the
    # config classes because siblings are discovered from here (a service does not import sibling
    # service classes).
    class MsStatic(Microservice):
        ms_id: int = 1
        icon: str = "static/icon_static_1.svg"
        route_prefix: str = "st"
        launch_cmd: str = "ddpa_static_fsdb_serve"

    class MsDetection(Microservice):
        ms_id: int = 2
        icon: str = "static/icon_detection_1.svg"
        route_prefix: str = "dt"
        launch_cmd: str = "ddpa_detection_serve"

    # MsLayout has MOVED to the ddpa_layout repo (ddp_layout/config.py) as the first fully
    # decentralized service: it subclasses Microservice and is parsed with suite_root=DdpMsConfigs
    # without being a member here. ms_id=3 is reserved for it. See [[status]] and the ddp_online skill.

    class MsFastSearch(Microservice):
        ms_id: int = 4
        icon: str = "static/icon_fastsearch.svg"
        route_prefix: str = "fs"
        launch_cmd: str = "ddpa_fastsearch_serve"

    class MsSlicer(Microservice):
        ms_id: int = 5
        icon: str = "static/icon_slice.svg"
        route_prefix: str = "sl"
        launch_cmd: str = "ddpa_slicer_serve"
        max_charter_count_allowed: int = 2000
