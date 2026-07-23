#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import glob
import sys
from typing import Any, List, Tuple, Generator, Union
import fargv
import re
from pathlib import Path
import string


def md5_postfix(input_str: str) -> str:
    if len(input_str) < 32:
        return False
    try:
        res = int(input_str[-32:],16)
        return res>0
    except ValueError:
        return False

def archive_postfix(input_str: str) -> str:
    lastname = input_str.split("/")[-1]
    allowed = string.ascii_uppercase+"-"
    return all([c in allowed for c in lastname]) and len(lastname) > 3


def get_output_from_input(input_str: str, keep_prefix: str = "(.*)/CH.atom.txt", replace_postfix: str = "/CH.tenor.txt") -> Tuple[bool, str]:
    """ This function processes the input string to extract a part of it based on the keep_prefix and replace_postfix.
    It returns a tuple containing a boolean indicating success and the modified string."""
    # Process the input string using fargv
    keep = re.findall(keep_prefix, input_str)
    if len(keep) != 1:
        if len(keep) > 1:
            raise ValueError(f"Multiple matches found for keep_prefix '{keep_prefix}' in input '{input_str}'")
        else:
            return False, ""
    return True, f"{keep[0]}{replace_postfix}"


def generate_fsdb(fsdb_root: str, arbitrary_archive_collection: List[str]=[], archive_marker: str = "", skip_marked_archives: bool = True) -> Generator[str, None, None]:
    """
    This function generates an archive string based on the provided directory and fond marker.
    """
    if arbitrary_archive_collection != []:
        assert fsdb_root == "", "fsdb_root must be empty if collection is provided"
        archive_dirs = [f for f in arbitrary_archive_collection if archive_postfix(f)]
    else:
        archive_dirs = [f for f in glob.glob(f"{fsdb_root}/*") if archive_postfix(f)]
    for archive_dir in archive_dirs:
        if archive_marker != "" and skip_marked_archives is not None:
            if Path(f"{archive_dir}/{archive_marker}").exists():
                continue
        yield from generate_archive(archive_dir=archive_dir)


def generate_archive(archive_dir: str, arbitrary_fond_collection: List[str]=[], fond_marker: str = "", skip_marked_fonds: bool = True) -> Generator[str, None, None]:
    """
    This function generates an archive string based on the provided directory and fond marker.
    """
    if arbitrary_fond_collection != []:
        assert archive_dir == "", "archive_dir must be empty if collection is provided"
        fond_dirs = [f for f in arbitrary_fond_collection if md5_postfix(f)]
    else:
        fond_dirs = glob.glob(f"{archive_dir}/*")
    for fond_dir in fond_dirs:
        if fond_marker != "" and skip_marked_fonds is not None:
            if Path(f"{fond_dir}/{fond_marker}").exists():
                continue
        yield from generate_fond(fond_dir=fond_dir)


def generate_fond(fond_dir: str, arbitrary_charter_collection: List[str]=[]) -> Generator[str, None, None]:
    """
    This function generates a fond string based on the provided directory.
    """
    if arbitrary_charter_collection != []:
        assert fond_dir == "", "fond_dir must be empty if collection is provided"
        charter_dirs = [f for f in arbitrary_charter_collection if md5_postfix(f)]
    else:
        charter_dirs = [f for f in glob.glob(f"{fond_dir}/*") if md5_postfix(f)]
    for charter_dir in charter_dirs:
        yield charter_dir


def generate_charter_appfiles(charters: Generator[str, None, None], 
                              file_glob: str = "CH.atom.txt",
                              keep_prefix: str = "(.*)/CH.atom.txt", 
                              replace_postfix: str = "/CH.tenor.txt", 
                              skip_existing: bool = True) -> Generator[Tuple[str, str], None, None]:
    """ This function generates a tuple of charter file paths and their corresponding output file paths.
    It processes each charter file based on the provided glob pattern and keeps or replaces the specified parts
    of the file paths. It also skips existing files if specified.

    args:
        charters (Generator[str, None, None]): A generator yielding charter directory paths.
        file_glob (str): A glob pattern to match charter files. "CH.atom.txt" will iterate over charter items.
            A file_glob of "*.img.*" will match all images in the charter.
        keep_prefix (str): A regex pattern to extract the part of the file path to keep.
        replace_postfix (str): A string to replace the postfix of the file path.
        skip_existing (bool): If True, skip files that already exist at the output path.
    returns:
        Generator[Tuple[str, str], None, None]: A generator yielding tuples of input and output file paths.
    """
    for charter_dir in charters:
        #print(f"Generating app files for charter: {charter_dir}", file=sys.stderr)
        charter_files = glob.glob(f"{charter_dir}/{file_glob}")
        for charter_file in charter_files:
            
            valid, output_file = get_output_from_input(charter_file, keep_prefix, replace_postfix)
            if not valid:
            #    print(f"Skipping invalid file: {charter_file}", file=sys.stderr)
                continue
            if skip_existing and Path(output_file).exists():
            #    print(f"Skipping existing file: {charter_file} => {output_file}", file=sys.stderr)
                continue
            #print(f"Generating charter file: {charter_file} => {output_file}", file=sys.stderr)
            yield (charter_file, output_file)


def get_charter_iteration_fargv_dict(replace_postfix: str="/CH.tenor.txt", fsdb_root: str="./", keep_prefix: str="(.*)/CH.atom.txt", iterate_existing_charters: bool=False, 
                                     file_glob: str="CH.atom.txt", iterate_marked_archives: bool=False, fond_marker: str="", iterate_marked_fonds: bool=False) -> dict:
    charter_fargv_p = {
        "fsdb_root": fsdb_root,
        "archives": set([]),
        "fonds": set([]),
        "charters": set([]),
        "keep_prefix": keep_prefix,
        "replace_postfix": replace_postfix,
        "iterate_existing_charters": iterate_existing_charters,
        "archive_marker": "",
        "iterate_marked_archives": iterate_marked_archives,
        "fond_marker": "",
        "iterate_marked_fonds": False,
        "file_glob": file_glob,
    }
    return charter_fargv_p


def get_image_iteration_fargv_dict(replace_postfix: str=".layout.pred.json", fsdb_root: str="./", keep_prefix: str="(.*).img.*", keep_existing: bool=False, file_glob: str="*.img.*") -> dict:
    charter_fargv_p = {
        "fsdb_root": fsdb_root,
        "archives": set([]),
        "fonds": set([]),
        "charters": set([]),
        "images": set([]),
        "keep_prefix": keep_prefix,
        "replace_postfix": replace_postfix,
        "iterate_existing_charters": keep_existing,
        "archive_marker": "",
        "iterate_marked_archives": False,
        "fond_marker": "",
        "iterate_marked_fonds": False,
        "file_glob": file_glob,
    }
    return charter_fargv_p


def generate_app_files(fargv_namespace: Any):
    if len(fargv_namespace.archives) > 0:
        for charter_dir in generate_fsdb(
            fsdb_root=fargv_namespace.fsdb_root,
            arbitrary_archive_collection=list(fargv_namespace.archives),
            archive_marker=fargv_namespace.archive_marker,
            skip_marked_archives=not fargv_namespace.iterate_marked_archives
        ):
            generate_charter_appfiles(
                charters=[charter_dir],
                file_glob=fargv_namespace.file_glob,
                keep_prefix=fargv_namespace.keep_prefix,
                replace_postfix=fargv_namespace.replace_postfix,
                skip_existing=not(fargv_namespace.iterate_existing_charters)
            )
    if len(fargv_namespace.fonds) > 0:
        for fond_dir in generate_archive(
            archive_dir=fargv_namespace.fsdb_root,
            arbitrary_fond_collection=list(fargv_namespace.fonds),
            fond_marker=fargv_namespace.fond_marker,
            skip_marked_fonds=not fargv_namespace.iterate_marked_fonds
        ):
            generate_charter_appfiles(
                charters=[fond_dir],
                file_glob=fargv_namespace.file_glob,
                keep_prefix=fargv_namespace.keep_prefix,
                replace_postfix=fargv_namespace.replace_postfix,
                skip_existing=not(fargv_namespace.iterate_existing_charters)
            )
    if len(fargv_namespace.charters) > 0:
        for charter_dir in fargv_namespace.charters:
            generate_charter_appfiles('https://cloud.uni-graz.at/apps/files/files/154744794?dir=/DiDip/FSDB',
                charters=[charter_dir],
                file_glob=fargv_namespace.file_glob,
                keep_prefix=fargv_namespace.keep_prefix,
                replace_postfix=fargv_namespace.replace_postfix,
                skip_existing=not(fargv_namespace.iterate_existing_charters)
            )


def generate_app_image_files(fargv_namespace: Any):
    print("|-- generate_app_image_files --|", file=sys.stderr)
    if len(fargv_namespace.archives) > 0:
        for charter_dir in generate_fsdb(
            fsdb_root=fargv_namespace.fsdb_root,
            arbitrary_archive_collection=list(fargv_namespace.archives),
            archive_marker=fargv_namespace.archive_marker,
            skip_marked_archives=not fargv_namespace.iterate_marked_archives
        ):
            for input_file, output_file in generate_charter_appfiles(
                charters=[charter_dir],
                file_glob=fargv_namespace.file_glob,
                keep_prefix=fargv_namespace.keep_prefix,
                replace_postfix=fargv_namespace.replace_postfix,
                skip_existing=not(fargv_namespace.iterate_existing_charters)
            ):
                yield (input_file, output_file)
    if len(fargv_namespace.fonds) > 0:
        for fond_dir in generate_archive(
            archive_dir="",
            arbitrary_fond_collection=list(fargv_namespace.fonds),
            fond_marker=fargv_namespace.fond_marker,
            skip_marked_fonds=not fargv_namespace.iterate_marked_fonds
        ):
            for input_file, output_file in generate_charter_appfiles(
                charters=[fond_dir],
                file_glob=fargv_namespace.file_glob,
                keep_prefix=fargv_namespace.keep_prefix,
                replace_postfix=fargv_namespace.replace_postfix,
                skip_existing=not(fargv_namespace.iterate_existing_charters)
            ):
                yield (input_file, output_file)
    if len(fargv_namespace.charters) > 0:
        #print(f"|-- Processing {len(fargv_namespace.charters)} charters --|", file=sys.stderr)
        #for charter_dir in fargv_namespace.charters:
        #    for charter_dir in 
        for input_file, output_file in generate_charter_appfiles(
                    charters=fargv_namespace.charters,
                    file_glob=fargv_namespace.file_glob,
                    keep_prefix=fargv_namespace.keep_prefix,
                    replace_postfix=fargv_namespace.replace_postfix,
                    skip_existing=not(fargv_namespace.iterate_existing_charters)
                ):
            #print(f"|-- Processing charter file: {input_file} => {output_file}", file=sys.stderr)
            yield (input_file, output_file)
                #print(f"Charter Dir: {charter_dir}", file=sys.stderr)
                #for image_file in glob.glob(f"{charter_dir}/*.img.*"):
                #    valid, output_file = get_output_from_input(image_file, fargv_namespace.keep_prefix, fargv_namespace.replace_postfix)
                #    if not valid:
                #        continue
                #    if fargv_namespace.iterate_existing_charters or not Path(output_file).exists():
                #        yield (image_file, f"{charter_dir[0].replace(fargv_namespace.keep_prefix, '')}.img.{fargv_namespace.replace_postfix}")

    if len(fargv_namespace.images) > 0:
        for image_file in fargv_namespace.images:
            valid, output_file = get_output_from_input(image_file, fargv_namespace.keep_prefix, fargv_namespace.replace_postfix)
            if not valid:
                continue
            if not fargv_namespace.iterate_existing_charters or not Path(output_file).exists():
                yield (image_file, output_file)


def demo_main_image_size():
    import fargv
    from PIL import Image
    import tqdm
    p = {
        "progress": False,  # IGNORE
    }
    p.update(get_image_iteration_fargv_dict(replace_postfix=".imgsz.txt"))
    args, _ = fargv.fargv(p)
    if args.progress:
        pr = lambda x: tqdm.tqdm(x, desc="Processing images")
    else:
        pr = lambda x: x
    for input_file, output_file in pr(generate_app_image_files(args)):
        img = Image.open(input_file)
        print(f"Image: {input_file}, Size: {img.size}, Output: {output_file}")
        open( output_file, 'a').write(f"{img.size}\n")


def demo_main_cei_size():
    import fargv
    import tqdm
    p = {
        "progress": False,  # IGNORE
    }
    p.update(get_charter_iteration_fargv_dict(replace_postfix="/CH.sizecei.txt"))
    args, _ = fargv.parse_args(p)
    if args.progress:
        pr = lambda x: tqdm.tqdm(x, desc="Processing images")
    else:
        pr = lambda x: x
    for input_file, output_file in pr(args):
        img = Image.open(input_file)
        print(f"Image: {input_file}, Size: {img.size}, Output: {output_file}")
        open( output_file, 'a').write(f"{img.size}\n")


if __name__ == "__main__":
    demo_main_image_size()