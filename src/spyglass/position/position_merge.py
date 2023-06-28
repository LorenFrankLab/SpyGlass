import functools as ft
import os
from pathlib import Path
from typing import Dict

import datajoint as dj
import numpy as np
import pandas as pd
from tqdm import tqdm as tqdm

from ..common.common_position import IntervalPositionInfo as CommonPos
from ..utils.dj_merge_tables import _Merge
from .v1.dlc_utils import check_videofile, get_video_path, make_video
from .v1.position_dlc_pose_estimation import DLCPoseEstimationSelection
from .v1.position_dlc_selection import DLCPosV1
from .v1.position_trodes_position import TrodesPosV1

schema = dj.schema("position_merge")


@schema
class PositionOutput(_Merge):
    """
    Table to identify source of Position Information from upstream options
    (e.g. DLC, Trodes, etc...) To add another upstream option, a new Part table
    should be added in the same syntax as DLCPos and TrodesPos.
    """

    definition = """
    merge_id : uuid
    ---
    source: varchar(32)
    ---
    """

    class DLCPosV1(dj.Part):
        """
        Table to pass-through upstream DLC Pose Estimation information
        """

        definition = """
        -> PositionOutput
        ---
        -> DLCPosV1
        """

    class TrodesPosV1(dj.Part):
        """
        Table to pass-through upstream Trodes Position Tracking information
        """

        definition = """
        -> PositionOutput
        ---
        -> TrodesPosV1
        """

    class CommonPos(dj.Part):
        """
        Table to pass-through upstream Trodes Position Tracking information
        """

        definition = """
        -> PositionOutput
        ---
        -> CommonPos
        """

    def fetch1_dataframe(self):
        nwb_data = self.fetch_nwb()[0]
        index = pd.Index(
            np.asarray(nwb_data["position"].get_spatial_series().timestamps),
            name="time",
        )
        if (
            "video_frame_ind"
            in nwb_data["velocity"].fields["time_series"].keys()
        ):
            COLUMNS = [
                "video_frame_ind",
                "position_x",
                "position_y",
                "orientation",
                "velocity_x",
                "velocity_y",
                "speed",
            ]
            return pd.DataFrame(
                np.concatenate(
                    (
                        np.asarray(
                            nwb_data["velocity"]
                            .get_timeseries("video_frame_ind")
                            .data,
                            dtype=int,
                        )[:, np.newaxis],
                        np.asarray(
                            nwb_data["position"].get_spatial_series().data
                        ),
                        np.asarray(
                            nwb_data["orientation"].get_spatial_series().data
                        )[:, np.newaxis],
                        np.asarray(
                            nwb_data["velocity"].get_timeseries("velocity").data
                        ),
                    ),
                    axis=1,
                ),
                columns=COLUMNS,
                index=index,
            )
        else:
            COLUMNS = [
                "position_x",
                "position_y",
                "orientation",
                "velocity_x",
                "velocity_y",
                "speed",
            ]
            return pd.DataFrame(
                np.concatenate(
                    (
                        np.asarray(
                            nwb_data["position"].get_spatial_series().data
                        ),
                        np.asarray(
                            nwb_data["orientation"].get_spatial_series().data
                        )[:, np.newaxis],
                        np.asarray(nwb_data["velocity"].get_timeseries().data),
                    ),
                    axis=1,
                ),
                columns=COLUMNS,
                index=index,
            )


@schema
class PositionVideoSelection(dj.Manual):
    definition = """
    nwb_file_name           : varchar(255)                 # name of the NWB file
    interval_list_name      : varchar(200)                 # descriptive name of this interval list
    plot_id                 : int
    plot                    : varchar(40) # Which position info to overlay on video file
    ---
    output_dir              : varchar(255)                 # directory where to save output video
    """

    def insert1(self, key, **kwargs):
        key["plot_id"] = self.get_plotid(key)
        super().insert1(key, **kwargs)

    def get_plotid(self, key):
        fields = list(self.primary_key)
        temp_key = {k: val for k, val in key.items() if k in fields}
        plot_id = temp_key.get("plot_id", None)
        if plot_id is None:
            plot_id = (
                dj.U().aggr(self & temp_key, n="max(plot_id)").fetch1("n") or 0
            ) + 1
        else:
            id = (self & temp_key).fetch("plot_id")
            if len(id) > 0:
                plot_id = max(id) + 1
            else:
                plot_id = max(0, plot_id)
        return plot_id


@schema
class PositionVideo(dj.Computed):
    """Creates a video of the computed head position and orientation as well as
    the original LED positions overlaid on the video of the animal.

    Use for debugging the effect of position extraction parameters."""

    definition = """
    -> PositionVideoSelection
    ---
    """

    def make(self, key):
        raise NotImplementedError("work in progress -DPG")
        assert key["plot"] in ["DLC", "Trodes", "Common", "All"]
        M_TO_CM = 100
        output_dir = (PositionVideoSelection & key).fetch1(
            "output_dir",
        )

        print("Loading position data...")
        # raw_position_df = (
        #     RawPosition()
        #     & {
        #         "nwb_file_name": key["nwb_file_name"],
        #         "interval_list_name": key["interval_list_name"],
        #     }
        # ).fetch1_dataframe()
        query = {
            "nwb_file_name": key["nwb_file_name"],
            "interval_list_name": key["interval_list_name"],
        }
        if key["plot"] == "DLC":
            pos_df_key = (
                PositionOutput()
                .merge_restrict(
                    {
                        **query,
                        "source": "DLCV1",
                    }
                )
                .fetch1(as_dict=True)
            )
            pos_df = (PositionOutput & pos_df_key).fetch1_dataframe()
        elif key["plot"] == "Trodes":
            pos_df = (
                PositionOutput()
                & {
                    **query,
                    "source": "TrodesV1",
                }
            ).fetch1_dataframe()
        elif key["plot"] == "Common":
            pos_df = (
                PositionOutput()
                & {
                    **query,
                    "source": "CommonV1",
                }
            ).fetch1_dataframe()
        elif key["plot"] == "All":
            # Check which entries exist in PositionOutput
            merge_dict = {}
            if (
                len(
                    PositionOutput()
                    & {
                        **query,
                        "source": "DLCV1",
                    }
                )
                > 0
            ):
                dlc_df = (
                    (
                        PositionOutput()
                        & {
                            **query,
                            "source": "DLCV1",
                        }
                    )
                    .fetch1_dataframe()
                    .drop(columns=["velocity_x", "velocity_y", "speed"])
                )
                merge_dict["DLC"] = dlc_df
            if (
                len(
                    PositionOutput()
                    & {
                        **query,
                        "source": "TrodesV1",
                    }
                )
                > 0
            ):
                trodes_df = (
                    (
                        PositionOutput()
                        & {
                            **query,
                            "source": "TrodesV1",
                        }
                    )
                    .fetch1_dataframe()
                    .drop(columns=["velocity_x", "velocity_y", "speed"])
                )
                merge_dict["Trodes"] = trodes_df
            if (
                len(
                    PositionOutput()
                    & {
                        **query,
                        "source": "CommonV1",
                    }
                )
                > 0
            ):
                common_df = (
                    (
                        PositionOutput()
                        & {
                            **query,
                            "source": "CommonV1",
                        }
                    )
                    .fetch1_dataframe()
                    .drop(columns=["velocity_x", "velocity_y", "speed"])
                )
                merge_dict["Common"] = common_df
            pos_df = ft.reduce(
                lambda left, right,: pd.merge(
                    left[1],
                    right[1],
                    left_index=True,
                    right_index=True,
                    suffixes=[f"_{left[0]}", f"_{right[0]}"],
                ),
                merge_dict.items(),
            )
        print("Loading video data...")
        epoch = (
            int(
                key["interval_list_name"]
                .replace("pos ", "")
                .replace(" valid times", "")
            )
            + 1
        )

        (
            video_path,
            video_filename,
            meters_per_pixel,
            video_time,
        ) = get_video_path(
            {"nwb_file_name": key["nwb_file_name"], "epoch": epoch}
        )
        video_dir = os.path.dirname(video_path) + "/"
        video_frame_col_name = [
            col for col in pos_df.columns if "video_frame_ind" in col
        ]
        video_frame_inds = (
            pos_df[video_frame_col_name[0]].astype(int).to_numpy()
        )
        if key["plot"] in ["DLC", "All"]:
            temp_key = (PositionOutput.DLCPosV1 & key).fetch1("KEY")
            video_path = (DLCPoseEstimationSelection & temp_key).fetch1(
                "video_path"
            )
        else:
            video_path = check_videofile(
                video_dir, key["output_dir"], video_filename
            )[0]

        nwb_base_filename = key["nwb_file_name"].replace(".nwb", "")
        output_video_filename = Path(
            f"{Path(output_dir).as_posix()}/{nwb_base_filename}{epoch:02d}_"
            f"{key['plot']}_pos_overlay.mp4"
        ).as_posix()

        # centroids = {'red': np.asarray(raw_position_df[['xloc', 'yloc']]),
        #              'green':  np.asarray(raw_position_df[['xloc2', 'yloc2']])}
        position_mean_dict = {}
        if key["plot"] in ["DLC", "Trodes", "Common"]:
            position_mean_dict[key["plot"]]["position"] = np.asarray(
                pos_df[["position_x", "position_y"]]
            )
            position_mean_dict[key["plot"]]["orientation"] = np.asarray(
                pos_df[["orientation"]]
            )
        elif key["plot"] == "All":
            position_mean_dict = {
                source: {
                    "position": np.asarray(
                        pos_df[[f"position_x_{source}", f"position_y_{source}"]]
                    ),
                    "orientation": np.asarray(
                        pos_df[[f"orientation_{source}"]]
                    ),
                }
                for source in merge_dict.keys()
            }
        position_time = np.asarray(pos_df.index)
        cm_per_pixel = meters_per_pixel * M_TO_CM
        print("Making video...")

        make_video(
            video_path,
            video_frame_inds,
            position_mean_dict,
            video_time,
            position_time,
            processor="opencv",
            output_video_filename=output_video_filename,
            cm_to_pixels=cm_per_pixel,
            disable_progressbar=False,
        )
        self.insert1(key)
