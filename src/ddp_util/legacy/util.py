from typing import Dict, Generator, List, Union, BinaryIO, Optional
import os
import re
from pathlib import Path, PurePosixPath
from pprint import pprint
import hashlib
import random


def img2imgid(img:Union[BinaryIO, str, Path], return_bytes:bool=False):
    """Provides the id of a document's image given the image

    Args:
        img (Union[BinaryIO, str, Path]): _description_
        return_bytes (bool, optional): _description_. Defaults to False.

    Returns:
        Tuple[str, bytes]: A tuple of the image id as string, and the bytes used to compute the id
    """
    if isinstance(img, str) and Path(img).is_file():
        img_bytes = open(img, "rb").read()
    elif isinstance(img, Path):
        img_bytes = open(img, "rb").read()
    else:  #  assuming BinaryIO. todo (anguelos test explicitly)
        img_bytes = img.read()
    md5_str = hashlib.md5(img_bytes).hexdigest()
    return md5_str, img_bytes


def get_path_generator(directory: str, file_extension: str, ordered: bool = True) -> Generator:
    """
    Returns Generator of file paths in (sub)directories.
    @param directory: directory of monasterium xml files as a string
    @param file_extension: specifies file type, monasterium files would be .cei.xml
    @param ordered: specifies whether the result should be ordered alphabetically
    @return Generator with file paths
    """
    entries = sorted(os.scandir(directory), key=lambda entry: entry.name) if ordered else os.scandir(directory)
    for entry in entries:
        if entry.is_file() and entry.name.endswith(file_extension):
            yield Path(entry.path)
        elif entry.is_dir():
            yield from get_path_generator(entry.path, file_extension, ordered)
        else:
            continue


def get_path_list(directory: str, 
                  file_extension: str, 
                  amount: float = 100, 
                  ordered: bool = True,
                  exclude_patterns: Optional[Union[str, re.Pattern, List[Union[str, re.Pattern]]]] = None) -> List[str]:
    """
    Returns List containing file paths.
    @param directory: directory of monasterium xml files as a string
    @param file_extension: specifies file type (e.g. ".cei.xml")
    @param amount: specifies the amount of files to return (default: 100)
    @param ordered: specifies whether the result should be ordered alphabetically
    @param exclude_patterns: pattern or list of patterns (either strings or regex) to exclude from results
    @return List with file paths
    """
    
    # Ensure exclude_patterns is a list
    if exclude_patterns is None:
        exclude_patterns = []
    elif not isinstance(exclude_patterns, list):
        exclude_patterns = [exclude_patterns]

    def is_excluded(path: str) -> bool:
        for pattern in exclude_patterns:
            if isinstance(pattern, re.Pattern):
                if pattern.search(path):
                    return True
            elif path.endswith(pattern):
                return True
        return False

    paths = [f"{PurePosixPath(path)}" for path in get_path_generator(directory, file_extension, ordered)
        if str(path).endswith(file_extension) and not is_excluded(str(path))]


    return paths if amount == 100 else random.sample(paths, int(round(len(paths) / 100 * amount)))


def to_md5(string, trunc_threshold=0): 
    md5sum = hashlib.md5(string.encode('utf-8')).hexdigest()[trunc_threshold:]
    return md5sum