import os
import shutil
from pathlib import Path

import datajoint as dj
import fsspec
import h5py
import pynwb
from fsspec.implementations.cached import CachingFileSystem

from spyglass.common.common_usage import Export, ExportSelection
from spyglass.settings import export_dir, raw_dir
from spyglass.utils import SpyglassMixin, logger
from spyglass.utils.sql_helper_fn import SQLDumpHelper

try:
    import dandi.download
    import dandi.organize
    import dandi.upload
    import dandi.validate
    from dandi.consts import known_instances
    from dandi.dandiapi import DandiAPIClient
    from dandi.metadata.nwb import get_metadata
    from dandi.organize import CopyMode, FileOperationMode, OrganizeInvalid
    from dandi.pynwb_utils import nwb_has_external_links
    from dandi.validate_types import Severity

except (ImportError, ModuleNotFoundError) as e:
    (
        dandi,
        known_instances,
        DandiAPIClient,
        get_metadata,
        OrganizeInvalid,
        CopyMode,
        FileOperationMode,
        Severity,
        nwb_has_external_links,
    ) = [None] * 9
    logger.warning(e)


schema = dj.schema("common_dandi")


@schema
class DandiPath(SpyglassMixin, dj.Manual):
    definition = """
    -> Export.File
    ---
    dandiset_id: varchar(16)
    filename: varchar(255)
    dandi_path: varchar(255)
    dandi_instance = "dandi": varchar(32)
    """

    def fetch_file_from_dandi(self, key: dict):
        dandiset_id, dandi_path, dandi_instance = (self & key).fetch1(
            "dandiset_id", "dandi_path", "dandi_instance"
        )
        dandiset_id = str(dandiset_id)
        # get the s3 url from Dandi
        with DandiAPIClient(
            dandi_instance=known_instances[dandi_instance],
        ) as client:
            asset = client.get_dandiset(dandiset_id).get_asset_by_path(
                dandi_path
            )
            s3_url = asset.get_content_url(follow_redirects=1, strip_query=True)

        # stream the file from s3
        # first, create a virtual filesystem based on the http protocol
        fs = fsspec.filesystem("http")

        # create a cache to save downloaded data to disk (optional)
        fsspec_file = CachingFileSystem(
            fs=fs,
            cache_storage=f"{export_dir}/nwb-cache",  # Local folder for cache
        )

        # Open and return the file
        fs_file = fsspec_file.open(s3_url, "rb")
        io = pynwb.NWBHDF5IO(file=h5py.File(fs_file))
        nwbfile = io.read()
        return (io, nwbfile)

    def compile_dandiset(
        self,
        key: dict,
        dandiset_id: str,
        dandi_api_key: str = None,
        dandi_instance: str = "dandi",
        skip_raw_files: bool = False,
    ):
        """Compile a Dandiset from the export.
        Parameters
        ----------
        key : dict
            ExportSelection key
        dandiset_id : str
            Dandiset ID generated by the user on the dadndi server
        dandi_api_key : str, optional
            API key for the dandi server. Optional if the environment variable
            DANDI_API_KEY is set.
        dandi_instance : str, optional
            What instance of Dandi the dandiset is on. Defaults to dev server.
        skip_raw_files : bool, optional
            Dev tool to skip raw files in the export. Defaults to False.
        """
        key = (Export & key).fetch1("KEY")
        paper_id = (Export & key).fetch1("paper_id")
        if self & key:
            raise ValueError(
                "Adding new files to an existing dandiset is not permitted. "
                + f"Please rerun after deleting existing entries for {key}"
            )

        # make a temp dir with symbolic links to the export files
        source_files = (Export.File() & key).fetch("file_path")
        paper_dir = f"{export_dir}/{paper_id}"
        os.makedirs(paper_dir, exist_ok=True)
        destination_dir = f"{paper_dir}/dandiset_{paper_id}"
        dandiset_dir = f"{paper_dir}/{dandiset_id}"

        # check if pre-existing directories for dandi export exist.
        # Remove if so to continue
        for dandi_dir in destination_dir, dandiset_dir:
            if os.path.exists(dandi_dir):
                if (
                    dj.utils.user_choice(
                        "Pre-existing dandi export dir exist."
                        + f"Delete existing export folder: {dandi_dir}",
                        default="no",
                    )
                    == "yes"
                ):
                    shutil.rmtree(dandi_dir)
                    continue
                raise RuntimeError(
                    "Directory must be removed prior to dandi export to ensure "
                    + f"dandi-compatability: {dandi_dir}"
                )

        os.makedirs(destination_dir, exist_ok=False)
        for file in source_files:
            if not os.path.exists(
                f"{destination_dir}/{os.path.basename(file)}"
            ):
                if skip_raw_files and raw_dir in file:
                    continue
                # copy the file if it has external links so can be safely edited
                if nwb_has_external_links(file):
                    shutil.copy(
                        file, f"{destination_dir}/{os.path.basename(file)}"
                    )
                else:
                    os.symlink(
                        file, f"{destination_dir}/{os.path.basename(file)}"
                    )

        # validate the dandiset
        validate_dandiset(destination_dir, ignore_external_files=True)

        # given dandiset_id, download the dandiset to the export_dir
        url = (
            f"{known_instances[dandi_instance].gui}"
            + f"/dandiset/{dandiset_id}/draft"
        )
        dandi.download.download(url, output_dir=paper_dir)

        # organize the files in the dandiset directory
        dandi.organize.organize(
            destination_dir,
            dandiset_dir,
            update_external_file_paths=True,
            invalid=OrganizeInvalid.FAIL,
            media_files_mode=CopyMode.SYMLINK,
            files_mode=FileOperationMode.COPY,
        )

        # get the dandi name translations
        translations = translate_name_to_dandi(destination_dir)

        # upload the dandiset to the dandi server
        if dandi_api_key:
            os.environ["DANDI_API_KEY"] = dandi_api_key
        dandi.upload.upload(
            [dandiset_dir],
            dandi_instance=dandi_instance,
        )
        logger.info(f"Dandiset {dandiset_id} uploaded")
        # insert the translations into the dandi table
        translations = [
            {
                **(
                    Export.File() & key & f"file_path LIKE '%{t['filename']}'"
                ).fetch1(),
                **t,
                "dandiset_id": dandiset_id,
                "dandi_instance": dandi_instance,
            }
            for t in translations
        ]
        self.insert(translations, ignore_extra_fields=True)

    def write_mysqldump(self, export_key: dict):
        """Write a MySQL dump script to the paper directory for DandiPath."""
        key = (Export & export_key).fetch1("KEY")
        paper_id = (Export & key).fetch1("paper_id")
        spyglass_version = (ExportSelection & key).fetch(
            "spyglass_version", limit=1
        )[0]

        self.compare_versions(
            spyglass_version,
            msg="Must use same Spyglass version for export and Dandi",
        )

        sql_dump = SQLDumpHelper(
            paper_id=paper_id,
            docker_id=None,
            spyglass_version=spyglass_version,
        )
        sql_dump.write_mysqldump([self & key], file_suffix="_dandi")


def _get_metadata(path):
    # taken from definition within dandi.organize.organize
    try:
        meta = get_metadata(path)
    except Exception as exc:
        meta = {}
        raise RuntimeError("Failed to get metadata for %s: %s", path, exc)
    meta["path"] = path
    return meta


def translate_name_to_dandi(folder, dandiset_dir: str = None):
    """Uses dandi.organize to translate filenames to dandi paths

    NOTE: The name for a given file depends on all files in the folder

    Parameters
    ----------
    folder : str
        location of files to be translated
    danidset_dir : str
        location of organized dandiset directory. If provided, will use this to
        lookup the dandi_path for each file in the folder

    Returns
    -------
    dict
        dictionary of filename to dandi_path translations
    """
    if dandiset_dir is not None:
        return lookup_dandi_translation(folder, dandiset_dir)

    files = Path(folder).glob("*")
    metadata = list(map(_get_metadata, files))
    metadata, skip_invalid = dandi.organize.filter_invalid_metadata_rows(
        metadata
    )
    metadata = dandi.organize.create_unique_filenames_from_metadata(
        metadata, required_fields=None
    )
    return [
        {"filename": Path(file["path"]).name, "dandi_path": file["dandi_path"]}
        for file in metadata
    ]


def lookup_dandi_translation(source_dir: str, dandiset_dir: str):
    """Get the dandi_path for each nwb file in the source_dir from
    the organized dandi directory

    Parameters
    ----------
    source_dir : str
        location of the source files
    dandiset_dir : str
        location of the organized dandiset directory

    Returns
    -------
    dict
        dictionary of filename to dandi_path translations
    """
    # get the obj_id and dandipath for each nwb file in the dandiset
    dandi_name_dict = {}
    for dandi_file in Path(dandiset_dir).rglob("*.nwb"):
        dandi_path = dandi_file.relative_to(dandiset_dir).as_posix()
        with pynwb.NWBHDF5IO(dandi_file, "r") as io:
            nwb = io.read()
            dandi_name_dict[nwb.object_id] = dandi_path
    # for each file in the source_dir, lookup the dandipath based on the obj_id
    name_translation = {}
    for file in Path(source_dir).glob("*"):
        with pynwb.NWBHDF5IO(file, "r") as io:
            nwb = io.read()
            dandi_path = dandi_name_dict[nwb.object_id]
            name_translation[file.name] = dandi_path
    return name_translation


def validate_dandiset(
    folder, min_severity="ERROR", ignore_external_files=False
):
    """Validate the dandiset directory

    Parameters
    ----------
    folder : str
        location of dandiset to be validated
    min_severity : str
        minimum severity level for errors to be reported, threshold for failed
        Dandi upload is "ERROR"
    ignore_external_files : bool
        whether to ignore external file errors. Used if validating
        before the organize step
    """
    validator_result = dandi.validate.validate(folder)
    min_severity_value = Severity[min_severity].value

    filtered_results = [
        i
        for i in validator_result
        if i.severity is not None and i.severity.value >= min_severity_value
    ]

    if ignore_external_files:
        # ignore external file errors. will be resolved during organize step
        filtered_results = [
            i
            for i in filtered_results
            if not i.message.startswith("Path is not inside")
        ]

    if filtered_results:
        raise ValueError(
            "Validation failed\n\t"
            + "\n\t".join(
                [
                    f"{result.severity}: {result.message} in {result.path}"
                    for result in filtered_results
                ]
            )
        )
