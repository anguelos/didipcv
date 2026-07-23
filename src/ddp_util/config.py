import importlib.resources as resources
import json
from pathlib import Path
import os
from typing import List, Optional


def config_dict() -> dict:
    with resources.files("ddp_util").joinpath("default/config_service.json").open("r") as f:
        defaults = json.load(f)
    config_paths = resolv_config_paths(defaults["CONFIG_PATHS"].copy())
    
    res = defaults
    for config_path in config_paths:
        if Path(config_path).exists():
            with open(config_path, "r") as f:
                user_conf = json.load(f)
            res.update(user_conf)
    res['CONFIG_PATHS'] = resolv_config_paths(config_paths)
    res['FSDB_ROOT'] = resolv_config_paths([res['FSDB_ROOT']])[0]
    return res

def get_app_config(app_name: str, allow_missing: bool = False) -> dict:
    cfg = config_dict()
    try:
        return cfg["APPS"][app_name]
    except KeyError as e:
        if allow_missing:
            return {}
        else:
            raise e


def resolv_config_paths(config_paths: Optional[List[str]]=None) -> List[str]:
    APP_DIR = Path(__file__).resolve().parent.parent.parent
    HOME = Path.home()
    if config_paths is None:
        with resources.files("ddp_util").joinpath("default/config_service.json").open("r") as f:
            defaults = json.load(f)
        config_paths = defaults["CONFIG_PATHS"].copy()
    config_paths = [p.replace("${APP_DIR}", str(APP_DIR)) for p in config_paths]
    config_paths = [p.replace("${HOME}", str(HOME)) for p in config_paths]
    return config_paths


def get_microservice(name: str) -> dict:
    cfg = config_dict()
    for ms in cfg["MICROSERVICES"]:
        if ms["name"].lower() == name.lower():
            if not ms.get("host"):
                ms["host"] = ms.get("ip","127.0.0.1")
            return ms
    raise ValueError(f"Microservice with name {name} not found in configuration.")
