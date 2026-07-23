import fsdb
__version__ = fsdb.__version__

from .scope import scope   # request-scoped basket scope proxy: `from ddp_microservices import scope`

__all__ = ["__version__", "scope"]
