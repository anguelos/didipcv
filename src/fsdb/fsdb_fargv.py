import sys

import fargv
from fargv import deep_dataclass, auxiliary, to_json_schema, FargvAutoConfig
from glob import glob
from pathlib import Path
import re
from collections.abc import Generator
from dataclasses import field
from collections.abc import Iterator


def _image_to_image_path(image: str|Path, fsdb_root: Path) -> str:
    p = Path(image)
    if p.exists() and p.parent.parent.parent.parent == fsdb_root:
        return str(p)
    elif isinstance(image, str) and not re.fullmatch(r"^[0-9a-fA-F]{32}$", image):
        matches = glob(f"{fsdb_root}/*/*/*/{image}.img.*")
        if len(matches) == 1:
            return str(Path(matches[0]))
        elif len(matches) == 0:
            raise ValueError(f"No matches for image '{image}' at root '{fsdb_root}'")
        else:
            raise ValueError(f"Multiple matches for image '{image}' at root '{fsdb_root}': {matches}")
    else:
        raise ValueError(f"Invalid image '{image}' at root '{fsdb_root}'")


def _charter_to_charter_path(charter: str|Path, fsdb_root: Path) -> str:
    p = Path(charter)
    if p.is_dir() and p.parent.parent.parent == fsdb_root:
        return str(p)
    elif isinstance(charter, str) and not re.fullmatch(r"[0-9a-fA-F]{32}", charter):
        matches = glob(f"{fsdb_root}/*/*/{charter}")
        if len(matches) == 1:
            return str(Path(matches[0]))
        elif len(matches) == 0:
            raise ValueError(f"No matches for charter '{charter}' at root '{fsdb_root}'")
        else:
            raise ValueError(f"Multiple matches for charter '{charter}' at root '{fsdb_root}': {matches}")
    else:
        raise ValueError(f"Invalid charter '{charter}'")


def _fond_to_fond_path(fond: str|Path, fsdb_root: Path) -> str:
    p = Path(fond)
    if p.is_dir() and p.parent.parent == fsdb_root:
        return str(p)
    elif isinstance(fond, str) and not re.fullmatch(r"[0-9a-fA-F]{32}", fond):
        matches = glob(f"{fsdb_root}/*/{fond}")
        if len(matches) == 1:
            return str(Path(matches[0]))
        elif len(matches) == 0:
            raise ValueError(f"No matches for fond '{fond}' at root '{fsdb_root}'")
        else:
            raise ValueError(f"Multiple matches for fond '{fond}' at root '{fsdb_root}': {matches}")
    else:
        raise ValueError(f"Invalid fond '{fond}' at root '{fsdb_root}'")


def _archive_to_archive_path(archive: str|Path, fsdb_root: Path) -> str:
    p = Path(archive)
    if p.is_dir() and p.parent.parent.parent.parent == fsdb_root:
        return str(p)
    elif isinstance(archive, str):
        if (fsdb_root / archive).is_dir():
            return str(fsdb_root / archive)
    else:
        raise ValueError(f"Invalid archive '{archive}' at root '{fsdb_root}'")


@deep_dataclass
class CharterFargvConfig(FargvAutoConfig):
    fsdb_root: str = "/mnt/data/full_fsdb/slices/mariapia/fsdb"
    charters: list[str] = []
    charter_glob: str = ""
    fonds: list[str] = []
    fond_glob: str = ""
    archives: list[str] = []
    archive_glob: str = ""
    input_ext_re: str = r'^(.*[/][0-9a-fA-F]{32})()$'
    process_existing: bool = False


@deep_dataclass
class CharterImageFargvConfig(FargvAutoConfig):
    images: list[str] = []
    image_glob: str = ""
    fsdb_root: str = "/mnt/data/full_fsdb/slices/mariapia/fsdb"
    charters: list[str] = []
    charter_glob: str = ""
    fonds: list[str] = []
    fond_glob: str = ""
    archives: list[str] = []
    archive_glob: str = ""    
    input_ext_re: str = r'^(.*?)((?:\.img)(?:\.[^./]+)*)$'
    process_existing: bool = False


class FsdbOutputInferer:
    def __init__(self, cfg: CharterFargvConfig|None = None, output_replace_name: str|None = "output_replace", input_ext_re: str|None=None, output_replace: str|None=None) -> None:
        self.is_null = False
        if cfg is None and input_ext_re is None and output_replace is None:
            self.is_null = True
        elif cfg is None:
            assert input_ext_re is not None and output_replace is not None, "If cfg is not provided, input_ext_re and output_replace must be provided"
            assert len(input_ext_re) > 0, "input_ext_re must be a non-empty string"
            assert len(output_replace) > 0, "output_replace must be a non-empty string"
            self.input_ext_re = input_ext_re
            self.output_replace = output_replace
        else:
            assert output_replace_name is not None, "output_replace_name must be provided if cfg is provided"
            assert output_replace_name in cfg.__dataclass_fields__, f"{output_replace_name} must be a field in cfg"
            self.input_ext_re = cfg.input_ext_re
            self.output_replace = getattr(cfg, output_replace_name)
    
    def __call__(self, input_path: str) -> str:
        if self.is_null:
            return ''
        m = re.match(self.input_ext_re, input_path)
        if m is None:
            raise ValueError(f"Path '{input_path}' does not match input_ext_re '{self.input_ext_re}'")
        else:
            output_path = re.sub(self.input_ext_re, lambda m: m.group(1) + self.output_replace, input_path)
        return output_path


def generate_charter_paths(cfg: CharterFargvConfig, generate_output: bool = False, output_replace_name: str = "output_replace", verbosity: int = 0) -> Iterator[str] | Iterator[tuple[str, str]]:
    processed_charters = set()
    fond_paths = []
    fsdb_root = Path(cfg.fsdb_root)
    produce_output = FsdbOutputInferer(cfg=cfg)
    existing_count = 0
    duplicates_count = 0
    if cfg.archives == [] and cfg.fonds == [] and cfg.charters == [] and cfg.archive_glob == "" and cfg.fond_glob == "" and cfg.charter_glob == "":
        cfg.fond_glob = f"{fsdb_root}/*/{'?'*32}"
        print(f"No items given. Defaulting fond_glob to '{cfg.fond_glob}'", file=sys.stderr)

    for archive in cfg.archives + list(glob(cfg.archive_glob)):
        archive_path = _archive_to_archive_path(archive, fsdb_root=fsdb_root)
        fond_paths += list(glob(f"{archive_path}/{'?'*32}"))
    
    for fond in cfg.fonds + list(glob(cfg.fond_glob)) + fond_paths:
        fond_path = _fond_to_fond_path(fond, fsdb_root=fsdb_root)
        charter_paths = glob(f"{fond_path}/{'?'*32}")
        for charter_path in charter_paths:
            if charter_path in processed_charters:
                continue
            processed_charters.add(charter_path)
            if generate_output:
                output_path = produce_output(charter_path)
                if not cfg.process_existing and Path(output_path).exists():
                    continue
                else:
                    yield charter_path, output_path
            else:
                yield charter_path

    for charter in cfg.charters + list(glob(cfg.charter_glob)):
        charter_path = _charter_to_charter_path(charter, fsdb_root=fsdb_root)
        if charter_path in processed_charters:
            duplicates_count += 1
            if verbosity > 2:
                print(f"Duplicate charter '{charter_path}' already processed, skipping. Total duplicates so far: {duplicates_count}", file=sys.stderr)
            continue
        processed_charters.add(charter_path)
        if generate_output:
            output_path = produce_output(charter_path)
            if not cfg.process_existing and Path(output_path).exists():
                existing_count += 1
                if verbosity > 2:
                    print(f"Output for charter '{charter_path}' already exists at '{output_path}', skipping. Total existing so far: {existing_count}", file=sys.stderr)
                continue
            else:
                yield charter_path, output_path
        else:
            yield charter_path
    if verbosity > 1:
        print(f"Finished generating charter paths. Total processed: {len(processed_charters)}, out of which {existing_count} already existed and were skipped, and {duplicates_count} were duplicates.", file=sys.stderr)


def generate_image_paths(cfg: CharterImageFargvConfig, generate_output: bool = False, output_replace_name: str = "output_replace", verbosity: int = 0) -> Iterator[str] | Iterator[tuple[str, str]]:
    processed_images = set()
    fond_paths = []
    charter_paths = []
    fsdb_root = Path(cfg.fsdb_root)
    produce_output = FsdbOutputInferer(cfg=cfg)
    existing_count = 0
    duplicates_count = 0

    for archive in cfg.archives + list(glob(cfg.archive_glob)):
        archive_path = _archive_to_archive_path(archive, fsdb_root=fsdb_root)
        fond_paths += list(glob(f"{archive_path}/{'?'*32}"))
    
    for fond in cfg.fonds + list(glob(cfg.fond_glob)) + fond_paths:
        fond_path = _fond_to_fond_path(fond, fsdb_root=fsdb_root)
        charter_paths += list(glob(f"{fond_path}/{'?'*32}"))

    for charter in cfg.charters + list(glob(cfg.charter_glob)) + charter_paths:
        charter_path = _charter_to_charter_path(charter, fsdb_root=fsdb_root)
        image_paths = glob(f"{charter_path}/**/*.img.*")
        for image_path in image_paths:
            if image_path in processed_images:
                duplicates_count += 1
                if verbosity > 2:
                    print(f"Duplicate image '{image_path}' already processed, skipping. Total duplicates so far: {duplicates_count}", file=sys.stderr)
                continue
            processed_images.add(image_path)
            output = produce_output(image_path)
            if not cfg.process_existing and Path(output).exists():
                existing_count += 1
                if verbosity > 2:
                    print(f"Output for image '{image_path}' already exists at '{output}', skipping. Total existing so far: {existing_count}", file=sys.stderr)
                continue

            if generate_output:
                yield image_path, output
            else:
                yield image_path
    
    for image in cfg.images + list(glob(cfg.image_glob)):
        if not Path(image).exists():  # We allow non FSDB images as well.
            image_path = str(Path(image))
        else:
            image_path = _image_to_image_path(image, fsdb_root=fsdb_root)

        if image_path in processed_images:
            duplicates_count += 1
            if verbosity > 2:
                print(f"Duplicate image '{image_path}' already processed, skipping. Total duplicates so far: {duplicates_count}", file=sys.stderr)
            continue

        processed_images.add(image_path)
        output = produce_output(image_path)
        if verbosity > 2:
            print(f"'{image_path}'\n'{output}'\nExists: {Path(output).exists()} Process existing: {cfg.process_existing}\n\n", flush=True, file=sys.stderr)
        if not cfg.process_existing and Path(output).exists():
            existing_count += 1
            if verbosity > 2:                
                print(f"Output for image '{image_path}' already exists at '{output}', skipping. Total existing so far: {existing_count}", file=sys.stderr)
            continue
        if generate_output:
            yield image_path, output
        else:
            yield image_path
    if verbosity > 1:
        print(f"Finished generating image paths. Total processed: {len(processed_images)}, out of which {existing_count} already existed and were skipped, and {duplicates_count} were duplicates.", file=sys.stderr)
