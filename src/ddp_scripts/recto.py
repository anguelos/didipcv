from fsdb import FSDB, CharterFargvConfig, generate_charter_paths
import fargv
from PIL import Image
from pathlib import Path
import os
import tqdm
import time


def create_relative_symlink(target_path: str, link_path: str, transcode_to_png: bool = False, verbocity: int = 1):
    if os.path.islink(link_path):
        if verbocity >= 1:
            print(f"Symlink '{link_path}' is a link, removing.")
            os.unlink(link_path)
    if target_path[-3:].lower() != "png" and transcode_to_png:
        if verbocity >= 1:
            print(f"Transcoding '{target_path}' to PNG at '{link_path}'")
        with Image.open(target_path) as img:
            img.save(link_path, format="PNG")
        return
    if verbocity >= 3:
        print(f"Creating symlink '{link_path}' -> '{target_path}'")
    link_dir = os.path.dirname(os.path.abspath(link_path))
    rel_target = os.path.relpath(target_path, start=link_dir)
    os.symlink(rel_target, link_path)
    if verbocity >= 3:
        print(f"Created symlink '{link_path}' -> '{rel_target}'")


def recto_main():
    @fargv.deep_dataclass
    class Config(CharterFargvConfig):
        output_replace : str = "/CH_recto.png"
        transcode_to_png : bool = False
    cfg, _ = fargv.parse(definition=Config)
    if cfg.verbocity >= 2:
        progress_bar = tqdm.tqdm(total=100, desc="Processing charters", unit="charter")
    t = time.time()
    for charter_path, output_path in generate_charter_paths(cfg, generate_output=True):
        symlink_recto = Path(output_path).with_suffix("/CH_recto.png")
        create_relative_symlink(symlink_recto, symlink_recto, transcode_to_png=cfg.transcode_to_png, verbocity=cfg.verbocity)
        if cfg.verbocity >= 3:
            print(f"Processing charter '{charter_path}' with output '{output_path}'")
        if cfg.verbocity >= 2:
            progress_bar.update(1)
    if cfg.verbocity >= 2:
        progress_bar.close()
    if cfg.verbocity >= 1:
        print(f"Done processing {progress_bar.n} charters in {time.time() - t:.2f} seconds.")

