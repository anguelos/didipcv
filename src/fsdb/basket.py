from typing import List, Set, Union, Optional
import sys
import requests
import re
import os


def handle_ids(arguments: Set[str], raise_on_invalid: bool = True, id_validation: Optional[Union[dict, set]] = None) -> Union[None, List[str]]:
    """
    Process a set of IDs and return a list of valid IDs.

    This function handles various input formats for IDs, including reading from stdin, 
    TSV files, basket files, or Google Sheets URLs. It validates the IDs based on 
    optional criteria such as length and a provided validation set.

    Parameters
    ----------
    arguments : Set[str]
        A set of input arguments. This can be {"stdin"}, {file paths}, or {URLs} or a set of strings containing the IDs (typically md5sums).
        If a a file is passed, it should be named .tsv or .basket file containing the IDs in tsv format, "stdin" will try to parse tsv values from standard input.
        If a Google Sheets URL is passed, IDs will be fetched from the sheet.
    raise_on_invalid : bool, optional
        Whether to raise an exception for invalid IDs. Defaults to True.
        If id_validation is provided, IDs not in that set will be considered invalid regardless of their format.
    id_validation : dict or set, optional
        A set or dictionary of valid IDs for validation. If provided, IDs not in this 
        set will be considered invalid.

    Returns
    -------
    Union[None, List[str]]
        A list of valid IDs, or None if no arguments are provided.

    Raises
    ------
    ValueError
        If an invalid ID is encountered and `raise_on_invalid` is True.

    Notes
    -----
    - If `arguments` contains "stdin", IDs are read from standard input.
    - If `arguments` contains a file path ending in `.tsv` or `.basket`, IDs are read 
      from the file.
    - If `arguments` contains a Google Sheets URL, IDs are fetched from the sheet.

    Examples
    --------
    >>> handle_ids({"stdin"})
    # Reads IDs from standard input.

    >>> handle_ids({"file.tsv"})
    # Reads IDs from a TSV file.

    >>> handle_ids({"https://docs.google.com/spreadsheets/d/.../edit?gid=0"})
    # Fetches IDs from a Google Sheets URL.

    """
    if len(arguments) == 1:
        id_name = list(arguments)[0]
        if id_name == "stdin":
            new_charter_ids = []
            for line in sys.stdin:
                for charter_id in line.strip().split():
                    if id_validation is not None and charter_id not in id_validation:
                        raise ValueError(f"Invalid charter id: {charter_id}")
                    elif len(charter_id) != 32 and raise_on_invalid:  # assuming md5 ids are 32 characters long
                        raise ValueError(f"Invalid charter id length: {charter_id}")
                    elif len(charter_id) == 0:
                        continue  # skip empty entries are tolerated
                    else:
                        new_charter_ids.append(charter_id)
            return new_charter_ids
        if id_name.endswith(".tsv") or id_name.endswith(".basket"):
            tsv_content = open(id_name).read()
            new_charter_ids = []
            for line in tsv_content.splitlines():
                for charter_id in line.strip().split():
                    if id_validation is not None and charter_id not in id_validation:
                        raise ValueError(f"Invalid charter id: {charter_id}")
                    elif len(charter_id) != 32 and raise_on_invalid:  # assuming md5 ids are 32 characters long
                        raise ValueError(f"Invalid charter id length: {charter_id}")
                    elif len(charter_id) == 0:
                        continue  # skip empty entries are tolerated
                    else:
                        new_charter_ids.append(charter_id)
            return new_charter_ids

        elif id_name.startswith("https://docs.google.com/spreadsheets"):
            doc_id = id_name.split("/")[5]
            sheet_id = re.findall(r"gid=([0-9]+)", id_name)
            assert len(set(sheet_id)) <= 1, "Multiple sheet ids found in the URL."
            sheet_id = list(set(sheet_id))[0] if len(sheet_id) > 0 else "0"
            tsv_url = f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=tsv&gid={sheet_id}"
            resp = requests.get(tsv_url)
            resp.raise_for_status()
            tsv_content = resp.text
            new_charter_ids = []
            for line in tsv_content.splitlines():
                for charter_id in line.strip().split():
                    if id_validation is not None and charter_id not in id_validation:
                        raise ValueError(f"Invalid charter id: {charter_id}")
                    elif len(charter_id) != 32 and raise_on_invalid:  # assuming md5 ids are 32 characters long
                        raise ValueError(f"Invalid charter id length: {charter_id}")
                    elif len(charter_id) == 0:
                        continue  # skip empty entries are tolerated
                    else:
                        new_charter_ids.append(charter_id)
            return new_charter_ids
        else:
            if id_validation is not None:
                if id_name not in id_validation:
                    raise ValueError(f"Invalid charter id: {id_name}")
            elif len(id_name) != 32 and raise_on_invalid:  # assuming md5
                raise ValueError(f"Invalid charter id length: {id_name}")
            else:
                return [id_name]
    elif len(arguments) == 0:
        return None
    else:
        return list(arguments)

