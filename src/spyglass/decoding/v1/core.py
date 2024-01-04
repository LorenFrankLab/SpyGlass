import datajoint as dj
from non_local_detector import (
    ContFragClusterlessClassifier,
    ContFragSortedSpikesClassifier,
    NonLocalClusterlessDetector,
    NonLocalSortedSpikesDetector,
)

from spyglass.common.common_session import Session  # noqa: F401
from spyglass.decoding.v1.dj_decoder_conversion import (
    convert_classes_to_dict,
    restore_classes,
)
from spyglass.position.position_merge import PositionOutput  # noqa: F401
from spyglass.utils import SpyglassMixin

schema = dj.schema("decoding_core_v1")


@schema
class DecodingParameters(SpyglassMixin, dj.Lookup):
    """Parameters for decoding the animal's mental position and some category of interest"""

    definition = """
    decoding_param_name : varchar(80)  # a name for this set of parameters
    ---
    decoding_params : BLOB             # initialization parameters for model
    decoding_kwargs : BLOB             # additional keyword arguments
    """

    contents = [
        {
            "decoding_param_name": "contfrag_clusterless",
            "decoding_params": ContFragClusterlessClassifier(),
            "decoding_kwargs": dict(),
        },
        {
            "decoding_param_name": "nonlocal_clusterless",
            "decoding_params": NonLocalClusterlessDetector(),
            "decoding_kwargs": dict(),
        },
        {
            "decoding_param_name": "contfrag_sorted",
            "decoding_params": ContFragSortedSpikesClassifier(),
            "decoding_kwargs": dict(),
        },
        {
            "decoding_param_name": "nonlocal_sorted",
            "decoding_params": NonLocalSortedSpikesDetector(),
            "decoding_kwargs": dict(),
        },
    ]

    @classmethod
    def insert_default(cls):
        cls.insert(cls.contents, skip_duplicates=True)

    def insert(self, rows, *args, **kwargs):
        for row in rows:
            row["decoding_params"] = convert_classes_to_dict(
                vars(row["decoding_params"])
            )
        super().insert(rows, *args, **kwargs)

    def fetch(self, *args, **kwargs):
        rows = super().fetch(*args, **kwargs)
        if len(rows) > 0 and len(rows[0]) > 1:
            content = []
            for row in rows:
                (
                    decoding_param_name,
                    decoding_params,
                    decoding_kwargs,
                ) = row
                content.append(
                    (
                        decoding_param_name,
                        restore_classes(decoding_params),
                        decoding_kwargs,
                    )
                )
        else:
            content = rows
        return content

    def fetch1(self, *args, **kwargs):
        row = super().fetch1(*args, **kwargs)
        row["decoding_params"] = restore_classes(row["decoding_params"])
        return row


@schema
class PositionGroup(SpyglassMixin, dj.Manual):
    definition = """
    -> Session
    position_group_name: varchar(80)
    ----
    position_variables = NULL: longblob # list of position variables to decode
    """

    class Position(SpyglassMixin, dj.Part):
        definition = """
        -> PositionGroup
        -> PositionOutput.proj(pos_merge_id='merge_id')
        """

    def create_group(
        self,
        nwb_file_name: str,
        group_name: str,
        keys: list[dict],
        position_variables: list[str] = ["position_x", "position_y"],
    ):
        group_key = {
            "nwb_file_name": nwb_file_name,
            "position_group_name": group_name,
        }
        self.insert1(
            {
                **group_key,
                "position_variables": position_variables,
            },
            skip_duplicates=True,
        )
        for key in keys:
            self.Position.insert1(
                {
                    **key,
                    **group_key,
                },
                skip_duplicates=True,
            )
