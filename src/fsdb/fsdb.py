from typing import Literal
from collections.abc import Iterator, Iterable
from pathlib import Path
from glob import glob
import os
import sys
import json
import re
import requests
import tqdm
import tarfile
import io
import time
from PIL import Image


Blob = str | bytes
NamedBlobs = Iterable[tuple[str, Blob]]


def verbose_iterate(iterable, verbose=True, **kwargs):
    return tqdm.tqdm(iterable, **kwargs) if verbose else iterable


class FSDB:
    def __init__(self, root_path: Path | str):
        self.root_path = Path(root_path)
        self._archive_paths = None

    @property
    def archive_paths(self) -> list[Path]:
        if self._archive_paths is None:
            self._archive_paths = glob(f"{self.root_path}/[A-Z][a-zA-Z-]*")
        return self._archive_paths

    @property
    def archives(self) -> Iterator['Archive']:
        for archive_path in self.archive_paths:
            yield Archive(archive_path)
    
    @property
    def fond_paths(self) -> list[Path]:
        paths = []
        for archive in self.archives:
            paths.extend(archive.fond_paths)
        return paths
    
    @property
    def fonds(self) -> Iterator['Fond']:
        for archive in self.archives:
            for fond in archive.fonds:
                yield fond

    @property
    def charter_paths(self) -> list[Path]:
        paths = []
        for fond in self.fonds:
            paths.extend(fond.charter_paths)
        return paths
    
    @property
    def charters(self) -> Iterator['Charter']:
        for fond in self.fonds:
            for charter in fond.charters:
                yield charter

    def __str__(self):
        return f"FSDB at {self.root_path}"
    
    def exists(self):
        return self.root_path.exists() and self.root_path.is_dir()
    
    def is_valid(self):
        res = self.exists() 
        return res

    def generate_files_names_and_blobs(self, charter_ids: list[str]| None = None, fond_ids: list[str]| None = None,
                                       archive_ids: list[str]| None = None, whole_fsdb: bool = False,
                                       scope: Literal['fsdb_noimg', 'fsdb', 'fsdb_and_apps']='fsdb',
                                       verbose: bool=False,
                                       tolerate_missing: bool = True,
                                       max_charters_allowed: int = -1,
                                       skip_blobs: bool = False) -> NamedBlobs:
        assert scope in ['fsdb_noimg', 'fsdb', 'fsdb_and_apps'], f"Invalid scope: {scope}"
        if charter_ids is None and fond_ids is None and archive_ids is None and not whole_fsdb:
            return # Nothing to do
        if whole_fsdb:
            charter_ids = None
            fond_ids = None
            archive_ids = [archive.name for archive in self.archives]

        charter_paths = []
        fond_paths = []
        archive_paths = []
        
        if charter_ids is not None:
            if verbose:
                print(f"Resolving Charter IDs to paths...")
            for charter_id in verbose_iterate(charter_ids, verbose=verbose):
                charter_glob_str = f"{self.root_path}/[A-Z][A-Za-z-]*/????????????????????????????????/{charter_id}"
                charter_path = glob(charter_glob_str)
                print(f"Charter glob: {charter_glob_str} found {len(charter_path)} paths", file=sys.stderr)
                if len(charter_path) == 1:
                    charter_path = Path(charter_path[0])
                    charter_paths.append(charter_path)
                else:
                    if tolerate_missing and len(charter_path) == 0:
                        print(f"Warning: Charter {charter_id} not found. Skipping.", file=sys.stderr)
                    else:
                        raise ValueError(f"Charter {charter_id} not found or multiple found (found {len(charter_path)}).")
        
        if verbose:
            before_count = len(charter_paths)

        charter_paths = list(set(charter_paths))

        if verbose:
            after_count = len(charter_paths)
            print(f"Resolved {before_count} to {after_count} unique charter paths.", file=sys.stderr)
                
        if fond_ids is not None:
            if verbose:
                print(f"Resolving Fond IDs to paths...")
            for fond_id in verbose_iterate(fond_ids, verbose=verbose):
                charter_glob_str = f"{self.root_path}/[A-Z-]+/{fond_id}/" + "?"*32
                for charter_path in glob(charter_glob_str):
                    charter_paths.append(Path(charter_path))
        
        if archive_ids is not None:
            if verbose:
                print(f"Resolving Archive IDs to paths...")
            for archive_id in verbose_iterate(archive_ids, verbose=verbose):
                charter_glob_str = f"{self.root_path}/{archive_id}/????????????????????????????????/" + "?"*32
                for charter_path in glob(charter_glob_str):
                    charter_paths.append(Path(charter_path))

        
        
        if max_charters_allowed > 0 and len(charter_paths) > max_charters_allowed:
            raise ValueError(f"Number of charter paths ({len(charter_paths)}) exceeds max_charters ({max_charters_allowed}).")


        fond_paths = list(set([p.parent for p in charter_paths]))
        archive_paths = list(set([p.parent for p in fond_paths]))

        if verbose:
            print(f"Generating files for {len(charter_paths)} charters...")
        for charter_path in verbose_iterate(charter_paths, verbose=verbose):
            charter = Charter(charter_path)
            charter_pseudopath = charter_path.relative_to(self.root_path)
            if scope == 'fsdb_noimg':
                if not skip_blobs:
                    yield str(charter_pseudopath / "CH.atom_id.txt"), charter.atom_id
                    yield str(charter_pseudopath / "CH.url.txt"), charter.mom_url
                    yield str(charter_pseudopath / "CH.cei.xml"), charter.cei_str
                    yield str(charter_pseudopath / "CH.image_urls.json"), json.dumps(charter.original_image_urls).encode('utf-8')
                else:
                    yield str(charter_pseudopath / "CH.atom_id.txt"), b"NA CH.noimg charter"
                    yield str(charter_pseudopath / "CH.url.txt"), b"NA CH.noimg charter"
                    yield str(charter_pseudopath / "CH.cei.xml"), b"NA CH.noimg charter"
                    yield str(charter_pseudopath / "CH.image_urls.json"), b"NA CH.noimg charter"
            elif scope == 'fsdb':
                if not skip_blobs:
                    yield str(charter_pseudopath / "CH.atom_id.txt"), charter.atom_id
                    yield str(charter_pseudopath / "CH.url.txt"), charter.mom_url
                    yield str(charter_pseudopath / "CH.cei.xml"), charter.cei_str
                    yield str(charter_pseudopath / "CH.image_urls.json"), json.dumps(charter.original_image_urls).encode('utf-8')
                    for image_path in charter.image_paths:
                        image_filename = os.path.basename(image_path)
                        with open(image_path, 'rb') as f:
                            image_data = f.read()
                        yield str(charter_pseudopath / image_filename), image_data
                else:
                    yield str(charter_pseudopath / "CH.atom_id.txt"), b"NA CH charter"
                    yield str(charter_pseudopath / "CH.url.txt"), b"NA CH charter"
                    yield str(charter_pseudopath / "CH.cei.xml"), b"NA CH charter"
                    yield str(charter_pseudopath / "CH.image_urls.json"), b"NA CH charter"
                    for image_path in charter.image_paths:
                        image_filename = os.path.basename(image_path)
                        yield str(charter_pseudopath / image_filename), b"NA CH charter image"

            elif scope == 'fsdb_and_apps':
                if not skip_blobs:
                    for file_path in glob(f"{charter.path}/*"):
                        file_filename = Path(file_path).name
                        blob = open(file_path, 'rb').read()
                        yield str(charter_pseudopath / file_filename), blob
                else:
                    for file_path in glob(f"{charter.path}/*"):
                        file_filename = Path(file_path).name
                        yield str(charter_pseudopath / file_filename), b"NA CH charter file"
        if verbose:
            print(f"Generating fond files...")
        for fond_path in verbose_iterate(fond_paths, verbose=verbose):
            fond_pseudopath = fond_path.relative_to(self.root_path)
            for fond_file_name in glob(f"{fond_path}/*"):
                fond_file_path = Path(fond_file_name)
                if fond_file_path.is_file():
                    with open(fond_file_path, 'rb') as f:
                        fond_file_blob = f.read()
                    if not skip_blobs:
                        yield str(fond_pseudopath / fond_file_path.name), fond_file_blob
                    else:
                        yield str(fond_pseudopath / fond_file_path.name), b"NA CH charter file"
        
        if verbose:
            print(f"Generating archive files...")
        for archive_path in verbose_iterate(archive_paths, verbose=verbose):
            archive_pseudopath = archive_path.relative_to(self.root_path)
            for archive_file_name in glob(f"{archive_path}/*"):
                archive_file_path = Path(archive_file_name)
                if archive_file_path.is_file():
                    with open(archive_file_path, 'rb') as f:
                        archive_file_blob = f.read()
                    if not skip_blobs:
                        yield str(archive_pseudopath / archive_file_path.name), archive_file_blob
                    else:
                        yield str(archive_pseudopath / archive_file_path.name), b"NA CH charter file"


class Archive:
    flags = {
        "AL": "🇦🇱",  # Albania
        "AD": "🇦🇩",  # Andorra
        "AM": "🇦🇲",  # Armenia
        "AT": "🇦🇹",  # Austria
        "AZ": "🇦🇿",  # Azerbaijan
        "BY": "🇧🇾",  # Belarus
        "BE": "🇧🇪",  # Belgium
        "BA": "🇧🇦",  # Bosnia and Herzegovina
        "BG": "🇧🇬",  # Bulgaria
        "HR": "🇭🇷",  # Croatia
        "CY": "🇨🇾",  # Cyprus
        "CZ": "🇨🇿",  # Czechia
        "DK": "🇩🇰",  # Denmark
        "EE": "🇪🇪",  # Estonia
        "FI": "🇫🇮",  # Finland
        "FR": "🇫🇷",  # France
        "GE": "🇬🇪",  # Georgia
        "DE": "🇩🇪",  # Germany
        "GR": "🇬🇷",  # Greece
        "HU": "🇭🇺",  # Hungary
        "IS": "🇮🇸",  # Iceland
        "IE": "🇮🇪",  # Ireland
        "IT": "🇮🇹",  # Italy
        "KZ": "🇰🇿",  # Kazakhstan (transcontinental)
        "XK": "🇽🇰",  # Kosovo (not ISO-official but widely supported)
        "LV": "🇱🇻",  # Latvia
        "LI": "🇱🇮",  # Liechtenstein
        "LT": "🇱🇹",  # Lithuania
        "LU": "🇱🇺",  # Luxembourg
        "MT": "🇲🇹",  # Malta
        "MD": "🇲🇩",  # Moldova
        "MC": "🇲🇨",  # Monaco
        "ME": "🇲🇪",  # Montenegro
        "NL": "🇳🇱",  # Netherlands
        "MK": "🇲🇰",  # North Macedonia
        "NO": "🇳🇴",  # Norway
        "PL": "🇵🇱",  # Poland
        "PT": "🇵🇹",  # Portugal
        "RO": "🇷🇴",  # Romania
        "RU": "🇷🇺",  # Russia
        "SM": "🇸🇲",  # San Marino
        "RS": "🇷🇸",  # Serbia
        "SK": "🇸🇰",  # Slovakia
        "SI": "🇸🇮",  # Slovenia
        "ES": "🇪🇸",  # Spain
        "SE": "🇸🇪",  # Sweden
        "CH": "🇨🇭",  # Switzerland
        "TR": "🇹🇷",  # Türkiye
        "UA": "🇺🇦",  # Ukraine
        "GB": "🇬🇧",  # United Kingdom
        "VA": "🇻🇦",  # Vatican City
        "UN": "🇺🇳",  # United Nations
    }

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.name = self.path.name
        self._fond_paths = None
        self._mom_url = None
        self._atom_id = None
        self._eag_xml_str = None
    
    @property
    def mom_url(self) -> str:
        if self._mom_url is None:
            self._mom_url = open(self.path / "AR.url.txt").read().strip()
        return self._mom_url
    
    @property
    def atom_id(self) -> str:
        if self._atom_id is None:
            self._atom_id = open(self.path / "AR.atom_id.txt").read().strip()
        return self._atom_id
    
    @property
    def eag_xml_str(self) -> str:
        if self._eag_xml_str is None:
            self._eag_xml_str = open(self.path / "AR.eag.xml").read().strip()
        return self._eag_xml_str

    @property
    def fond_paths(self) -> list[Path]:
        if self._fond_paths is None:
            self._fond_paths = glob(f"{self.path}/????????????????????????????????")
        return self._fond_paths

    @property
    def fonds(self) -> Iterator['Fond']:
        for fond_path in self.fond_paths:
            yield Fond(fond_path)
    
    @property
    def charter_paths(self) -> list[str]:
        charter_paths = []
        for fond in self.fonds:
            charter_paths.extend(fond.charter_paths)
        return charter_paths
    
    @property
    def charters(self) -> Iterator['Charter']:
        for fond in self.fonds:
            for charter in fond.charters:
                yield charter
    
    @property
    def fsdb_root(self) -> Path:
        return self.path.parent
    
    @property
    def flag_emoji(self) -> str:
        country_code = self.name.split('-')[0].upper()
        return self.flags.get(country_code, '🇺🇳')
    
    def __str__(self):
        return f"{self.flag_emoji} {self.name}\t{len(self.fond_paths)}"


    def __shallow_copy_to(self, new_fsdb_root: Path | str):
        dest_path = Path(new_fsdb_root) / self.name
        open(dest_path / "AR.url.txt", 'w').write(self.mom_url + "\n")
        open(dest_path / "AR.atom_id.txt", 'w').write(self.atom_id + "\n")
        open(dest_path / "AR.eag.xml", 'w').write(self.eag_xml_str + "\n")

    def __copy_children_to(self, new_fsdb_root: Path | str):
        dest_archive_path = Path(new_fsdb_root) / self.name
        for fond in self.fonds:
            dest_fond_path = dest_archive_path / fond.name
            os.makedirs(dest_fond_path, exist_ok=True)
            open(dest_fond_path / "FO.atom_id.txt", 'w').write(fond.atom_id + "\n")
            open(dest_fond_path / "FO.url.txt", 'w').write(fond.mom_url + "\n")
            open(dest_fond_path / "FO.preferences.xml", 'w').write(fond.preferences_xml_str + "\n")
            open(dest_fond_path / "FO.ead.xml", 'w').write(fond.ead_xml_str + "\n")
            for charter in fond.charters:
                charter.copy_to(dest_fond_path)
        return dest_archive_path

    def copy_to(self, new_fsdb_root: Path | str):
        self.__shallow_copy_to(new_fsdb_root)
        self.__copy_children_to(new_fsdb_root)

    def exists(self):
        return self.path.exists() and self.path.is_dir()
    
    def is_valid(self):
        res = self.exists() 
        res = res and self.path.joinpath("AR.url.txt").exists()
        res = res and self.path.joinpath("AR.atom_id.txt").exists()
        res = res and self.path.joinpath("AR.eag.xml").exists()
        return res

class Fond:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.name = self.path.name
        self._charter_paths = None
        self._atom_id = None
        self._mom_url = None
        self._preferences_xml_str = None
        self._ead_xml_str = None

    @property
    def atom_id(self) -> str:
        if self._atom_id is None:
            self._atom_id = open(self.path / "FO.atom_id.txt").read().strip()
        return self._atom_id
    
    @property
    def mom_url(self) -> str:
        if self._mom_url is None:
            self._mom_url = open(self.path / "FO.url.txt").read().strip()
        return self._mom_url
    
    @property
    def preferences_xml_str(self) -> str:
        if self._preferences_xml_str is None:
            try:
                self._preferences_xml_str = open(self.path / "FO.preferences.xml").read().strip()
            except FileNotFoundError:  #  TODO (florian): Remove this when all FO.preferences.xml are in place
                self._preferences_xml_str = "NA FSDB missing FO.preferences.xml"  
        return self._preferences_xml_str
    
    @property
    def ead_xml_str(self) -> str:
        if self._ead_xml_str is None:
            self._ead_xml_str = open(self.path / "FO.ead.xml").read().strip()
        return self._ead_xml_str
    
    @property
    def fsdb_root(self) -> Path:
        return self.path.parent.parent

    @property
    def charter_paths(self) -> list:
        if self._charter_paths is None:
            self._charter_paths = glob(f"{self.path}/????????????????????????????????")
        return self._charter_paths

    @property
    def charters(self) -> Iterator['Charter']:
        for charter_path in self.charter_paths:
            yield Charter(charter_path)
    
    @property
    def archive(self) -> 'Archive':
        return Archive(self.path.parent)

    def exists(self):
        return self.path.exists() and self.path.is_dir()
    
    def is_valid(self):
        res = self.exists()
        res = res and self.md5_id is not None
        res = res and self.mom_url is not None
        res = res and self.preferences_xml_str is not None
        res = res and self.ead_xml_str is not None
        res = res and self.charter_paths is not None
        return res
    
    def __str__(self):
        charter = Charter(self.charter_paths[0]) if len(self.charter_paths) > 0 else None
        atomid = charter.atom_id if charter is not None else "charter//NA"  # TODO (anguelos): make this computable from the fond level
        atomid = atomid.split("charter")[1].split("/")[2]
        return f"{atomid}\t({len(self.charter_paths)} charters)"



class Charter:
    def __init__(self, path: Path | str, validate: bool = True):
        self.path = Path(path)
        self.md5_id = self.path.name
        self._atom_id = None
        self._mom_url = None
        self._cei_str = None
        self._image_paths = None
        self._image_urls = None
        self._guessed_image_order = None
        if validate:
            self.validate()
    
    @property
    def atom_id(self) -> str:
        if self._atom_id is None:
            self._atom_id = open(self.path / "CH.atom_id.txt").read().strip()
        return self._atom_id
    
    @property
    def mom_url(self) -> str:
        if self._mom_url is None:
            self._mom_url = open(self.path / "CH.url.txt").read().strip()
        return self._mom_url
    
    @property
    def cei_str(self) -> str:
        if self._cei_str is None:
            self._cei_str = open(self.path / "CH.cei.xml").read().strip()
        return self._cei_str

    @property
    def original_image_urls(self) -> dict[str, str]:
        if self._image_urls is not None:
            return self._image_urls
        else:
            if Path(self.path / "CH.image_urls.json").exists():
                _image_urls = json.load(open(self.path / "CH.image_urls.json"))
            elif Path(self.path / "image_urls.json").exists():
                _image_urls = json.load(open(self.path / "image_urls.json"))
            else:
                _image_urls = {}
        #  TODO (FSDB): cleanup image_urls.json so that it includes "md5.img.jpg" in the keys instead of just "md5.jpg"
        self._image_urls = {}
        for url_p, url in list(_image_urls.items()):
            url_p = Path(url_p).name
            if ".img." not in url_p:
                img_md5 = url_p.split(".")[0]
                new_p = [p for p in self.image_paths if os.path.basename(p).startswith(img_md5)]  #  verifying it was found on the filesystem
                if len(new_p) == 1:
                    self._image_urls[new_p[0]] = url
                else:
                    raise ValueError(f"Image path for md5 {img_md5} from image_urls.json not found or multiple found in charter {self.path}.")
            else:
                self._image_urls[url_p] = url
        return self._image_urls

    @property
    def image_paths(self) -> list[str]:
        # Assuming image paths are stored in a file named "CH.images.txt"
        if self._image_paths is None:
            self._image_paths = glob(f"{self.path}/[a-f0-9]" + "*.img.*")
        return self._image_paths

    @property
    def image_ids(self) -> list[str]:
        # Assuming image paths are stored in a file named "CH.images.txt"
        return [os.path.basename(p).split(".img.")[0] for p in self.image_paths]
    
    @property
    def recto_image(self) -> str| None:
        if len(self.original_image_urls) == 0:
            return None
        return list(self.original_image_urls.keys())[0]

    @property
    def archival_signature(self) -> str:
        return self.atom_id.split('charter')[-1]
    
    @property
    def fsdb_root(self) -> str:
        return os.path.dirname(os.path.dirname(self.path))
    
    @property
    def fond(self) -> 'Fond':
        return Fond(self.path.parent)
    
    @property
    def fond_id(self) -> str:
        return self.path.parent.name
    
    @property
    def archive_id(self) -> str:
        return self.path.parent.parent.name
    
    @property
    def archive(self) -> 'Archive':
        return self.fond.archive

    def __guess_image_order__(self) -> list[tuple[str, str, str]]:
        """
        Ugly heuristics to guess the correct image order based on the original image URLs.
        1. If there is only one image, return it.
        2. If there are multiple images, look for patterns in the URLs that indicate order
           (e.g., '_r.', 'r.', '_v.', 'v.').
        3. If no clear pattern is found, sort by image surface area (width * height). # This is quite slower as it opens each image.


        :return: List of tuples (image_path, image_id, image_url) in guessed order.
        :rtype: List[Tuple[str, str, str]]
        
        """
        if self._guessed_image_order is not None:
            return self._guessed_image_order
        
        if len(self.image_paths) == 0:
            self._guessed_image_order = []
            return self._guessed_image_order

        if len(self.image_paths) == 1:
            p = self.image_paths[0]
            id = os.path.basename(p).split(".img.")[0]
            self._guessed_image_order = [(self.path / p, id, self.original_image_urls[p])]
            return self._guessed_image_order

        fitness_path_url_id = []
        for p, url in self.original_image_urls.items():
            fitness = 0
            low_p = url.split("/")[-1].lower()
            fitness += len(re.findall('_r[\\.-]', low_p)) * 2
            fitness += len(re.findall('r[\\.-]', low_p)) * 2
            fitness += len(re.findall('_v[\\.-]', low_p)) * 1
            fitness += len(re.findall('v[\\.-]', low_p)) * 1
            fitness += len(re.findall('r', low_p)) * .03
            fitness += len(re.findall('v', low_p)) * .01
            id = p.split("/")[-1].split(".")[0]
            fitness_path_url_id.append((fitness, p, url, id))
        fitness_path_url_id.sort(reverse=True)
        if fitness_path_url_id[0][0] >= 2:
            self._guessed_image_order = [(self.path / p, id, url) for fitness, p, url, id in fitness_path_url_id]
            return self._guessed_image_order
        else:
            for n in range(len(fitness_path_url_id)):
                fitness, p, url, id = fitness_path_url_id[n]
                sz = Image.open(self.path / p).size
                surface = sz[0] * sz[1]
                fitness_path_url_id[n] = (surface, p, url, id)
            fitness_path_url_id.sort(reverse=True)
            ordered_urls = [(self.path / p, id, url) for surface, p, url, id in fitness_path_url_id]
            self._guessed_image_order = ordered_urls
        return self._guessed_image_order

    @property
    def guessed_imagepath_order(self) -> list[str]:
        #print("Guessed image path order:", "\n".join([f"{repr(p)}" for p in self.__guess_image_order__()]), file=sys.stderr)
        return [p for p, id, url in self.__guess_image_order__()]
    
    @property
    def guessed_imageid_order(self) -> list[str]:
        #print("Guessed image ID order:", "\n".join([f"{repr(p)}" for p in self.__guess_image_order__()]), file=sys.stderr)
        return [id for p, id, url in self.__guess_image_order__()]

    def __str__(self) -> str:
        return f"{self.md5_id}\t({self.archival_signature})"

    def exists(self) -> bool:
        return self.path.exists() and self.path.is_dir()
    
    def clone(self, new_path: Path | str | None = None) -> 'Charter':
        res = Charter(self.path)
        res._atom_id = self.atom_id
        res._cei_str = self._cei_str
        res._image_urls = self.original_image_urls
    
    def is_valid(self) -> bool:
        res = self.exists()
        res = res and self.md5_id is not None
        res = res and self.mom_url is not None
        res = res and self.cei_str is not None
        res = res and self.image_paths is not None
        res = res and self.original_image_urls is not None
        res = res and len(self.original_image_urls) == len(self.image_paths)
        return res
    
    def validate(self) -> None:
        if not self.exists():
            raise ValueError(f"Charter path {self.path} does not exist.")
        if self.md5_id is None:
            raise ValueError(f"Charter at {self.path} has no md5_id.")
        if self.mom_url is None:
            raise ValueError(f"Charter at {self.path} has no mom_url.")
        if self.cei_str is None:
            raise ValueError(f"Charter at {self.path} has no cei_str.")
        if self.image_paths is None:
            raise ValueError(f"Charter at {self.path} has no image_paths.")
        if self.original_image_urls is None:
            raise ValueError(f"Charter at {self.path} has no original_image_urls.")
        if len(self.original_image_urls) != len(self.image_paths):
            raise ValueError(f"Charter at {self.path} has different number of original_image_urls and image_paths.")
