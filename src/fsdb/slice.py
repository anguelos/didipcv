from typing import BinaryIO, Literal, Tuple, Union
import tarfile
import io
from pathlib import Path
from glob import glob

from .fsdb import NamedBlobs, verbose_iterate
from fsdb.fsdb import FSDB
from .basket import handle_ids
import zipfile


def write_tar_gz(file_gen, stream: BinaryIO, mode: Literal["w:gz", "w"] = "w:gz", verbose: bool = False) -> None:
    # Big buffer drastically reduces write-call count (and BytesIO realloc churn)
    buffered = io.BufferedWriter(stream, buffer_size=1024 * 1024)  # 1 MiB (tune if you want)

    try:
        #with tarfile.open(mode="w:gz", fileobj=buffered) as tar:
        with tarfile.open(mode=mode, fileobj=buffered) as tar:
            if verbose:
                file_gen = verbose_iterate(file_gen, desc="Adding files to tar.gz")

            for rel_path, content in file_gen:
                if isinstance(content, str):
                    content_bytes = content.encode("utf-8")
                else:
                    content_bytes = content

                info = tarfile.TarInfo(name=rel_path)
                info.size = len(content_bytes)
                tar.addfile(info, io.BytesIO(content_bytes))
    finally:
        # ensure gzip footer + buffered bytes are pushed into the underlying stream
        buffered.flush()
        # avoid closing underlying `stream` (keeps behavior like your original)
        try:
            buffered.detach()
        except Exception:
            pass



def write_zip(file_gen: NamedBlobs, stream: BinaryIO, verbose: bool = False) -> None:
    """
    Write a zip archive built from `file_gen` into `stream`.

    `file_gen` yields (relative_path, content) where content is str or bytes.
    `stream` is any binary file-like object with .write() (BytesIO, open(..., 'wb'), etc.).
    """

    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as zipf:
        if verbose:
            file_gen = verbose_iterate(file_gen, desc="Adding files to zip")
        for rel_path, content in file_gen:
            # Normalize to bytes
            if isinstance(content, str):
                content_bytes = content.encode("utf-8")
            else:
                content_bytes = content
            zipf.writestr(rel_path, content_bytes)


def main_slice_fsdb_cli():
    import fargv
    import sys
    import ddp_util
    default_config = ddp_util.config()

    def write_with_parents(path: Union[str, Path], data: Union[bytes, str]):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            mode = 'wb'
        else:
            mode = 'w'
        open(p, mode).write(data)

    p = {
        "fsdb_root": Path(default_config['FSDB_ROOT']),
        "output": "fsdb_slice.tar.gz",
        "archive_ids": set([]),
        "fond_ids": set([]),
        "charter_ids": set([]),
        "whole_fsdb": False,
        "scope": ("fsdb", "fsdb_noimg", "fsdb_and_apps"),
        "tolerate_missing": True,
        "verbose": False,
    }
    args, _ = fargv.fargv(p)
    args.charter_ids = handle_ids(args.charter_ids)
    args.fond_ids = handle_ids(args.fond_ids)
    args.archive_ids = handle_ids(args.archive_ids)
        
    fsdb = FSDB(args.fsdb_root)
    if args.output.endswith(".tar.gz"):
        assert Path(args.output).exists() == False, f"Output file {args.output} already exists."
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, 'wb') as out_f:
            write_tar_gz(
                fsdb.generate_files_names_and_blobs(
                    charter_ids=args.charter_ids,
                    fond_ids=args.fond_ids,
                    archive_ids=args.archive_ids,
                    whole_fsdb=args.whole_fsdb,
                    scope=args.scope,
                    verbose=args.verbose,
                    tolerate_missing=args.tolerate_missing
                ),
                out_f
            )
        if args.verbose:
            print(f"Wrote tar.gz to {args.output}", file=sys.stderr)
        return
    elif Path(args.output).is_dir():
        if len(glob(f"{args.output}/*")) > 0:
            raise ValueError(f"Output directory {args.output} is not empty.")
        for n, (file_name, blob) in enumerate(fsdb.generate_files_names_and_blobs(
            charter_ids=args.charter_ids,
            fond_ids=args.fond_ids,
            archive_ids=args.archive_ids,
            whole_fsdb=args.whole_fsdb,
            scope=args.scope,
            verbose=args.verbose
        )):
            if args.verbose:
                print(f"{n:<5}:{file_name}, {len(blob)}", file=sys.stderr)
            write_with_parents(Path(args.output) / file_name, blob)


if __name__ == "__main__":
    main_slice_fsdb_cli()
