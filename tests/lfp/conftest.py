import copy

import numpy as np
import pytest


@pytest.fixture(scope="session")
def lfp(common):
    from spyglass import lfp

    return lfp


@pytest.fixture(scope="session")
def lfp_band(lfp):
    return lfp.analysis.v1


@pytest.fixture(scope="session")
def lfp_constants(common, mini_copy_name):
    n_delay = 9
    lfp_electrode_group_name = "test"
    orig_interval_list_name, orig_valid_times = (
        common.IntervalList & "interval_list_name LIKE '01_%'"
    ).fetch("interval_list_name", "valid_times")[0]
    new_interval_list_name = orig_interval_list_name + f"_first{n_delay}"
    new_interval_list_key = {
        "nwb_file_name": mini_copy_name,
        "interval_list_name": new_interval_list_name,
        "valid_times": np.asarray(
            [[orig_valid_times[0, 0], orig_valid_times[0, 0] + n_delay]]
        ),
    }

    yield dict(
        lfp_electrode_ids=[0],
        lfp_electrode_group_name=lfp_electrode_group_name,
        lfp_eg_key={
            "nwb_file_name": mini_copy_name,
            "lfp_electrode_group_name": lfp_electrode_group_name,
        },
        n_delay=n_delay,
        orig_interval_list_name="01_s1",
        orig_valid_times=orig_valid_times,
        interval_list_name=new_interval_list_name,
        interval_key=new_interval_list_key,
        filter1_name="LFP 0-400 Hz",
        filter_sampling_rate=30_000,
        filter2_name="Theta 5-11 Hz",
        lfp_band_electrode_ids=[0],  # assumes we've filtered these electrodes
        lfp_band_sampling_rate=100,  # desired sampling rate
    )


@pytest.fixture(scope="session")
def add_electrode_group(common, mini_copy_name, lfp_constants):
    common.FirFilterParameters().create_standard_filters()
    lfp.lfp_electrode.LFPElectrodeGroup.create_lfp_electrode_group(
        nwb_file_name=mini_copy_name,
        group_name=lfp_constants.get("lfp_electrode_group_name"),
        electrode_list=lfp_constants.get("lfp_electrode_ids"),
    )


@pytest.fixture(scope="session")
def add_interval(common, lfp_constants):
    common.IntervalList.insert1(
        lfp_constants.get("interval_key"), skip_duplicates=True
    )
    yield lfp_constants.get("interval_list_name")


@pytest.fixture(scope="session")
def add_selection(lfp, common, add_interval, lfp_constants):
    lfp_s_key = {
        **lfp_constants.get("lfp_eg_key"),
        "target_interval_list_name": add_interval,
        "filter_name": lfp_constants.get("filter1_name"),
        "filter_sampling_rate": lfp_constants.get("filter_sampling_rate"),
    }
    lfp.v1.LFPSelection.insert1(lfp_s_key, skip_duplicates=True)
    yield lfp_s_key


@pytest.fixture(scope="session")
def lfp_s_key(add_selection):
    yield add_selection


@pytest.fixture(scope="session")
def populate_lfp(lfp, add_selection):
    lfp.v1.LFPV1().populate(add_selection)
    yield {"merge_id": (lfp.LFPOutput.LFPV1() & lfp_s_key).fetch1("merge_id")}


@pytest.fixture(scope="session")
def lfp_merge_key(populate_lfp):
    yield populate_lfp


@pytest.fixture(scope="session")
def lfp_band_sampling_rate(lfp, lfp_merge_key):
    yield lfp.LFPOutput.merge_get_parent(lfp_merge_key).fetch1(
        "lfp_sampling_rate"
    )


@pytest.fixture(scope="session")
def add_band_filter(common, lfp_constants, lfp_band_sampling_rate):
    common.FirFilterParameters().add_filter(
        lfp_constants.get("filter2_name"),
        lfp_band_sampling_rate,
        "bandpass",
        [4, 5, 11, 12],
        "theta filter for 1 Khz data",
    )
    yield lfp_constants.get("filter2_name")


@pytest.fixture(scope="session")
def add_band_selection(
    lfp_band,
    mini_copy_name,
    lfp_merge_key,
    add_interval,
    lfp_constants,
    add_band_filter,
):
    lfp_band.LFPBandSelection().set_lfp_band_electrodes(
        nwb_file_name=mini_copy_name,
        lfp_merge_id=lfp_merge_key.get("merge_id"),
        electrode_list=lfp_constants.get("lfp_band_electrode_ids"),
        filter_name=add_band_filter,
        interval_list_name=add_interval,
        reference_electrode_list=[-1],
        lfp_band_sampling_rate=lfp_constants.get("lfp_band_sampling_rate"),
    )
    yield (lfp_band.LFPBandSelection().fetch1("KEY") & lfp_merge_key).fetch1(
        "KEY"
    )


@pytest.fixture(scope="session")
def lfp_band_key(add_band_selection):
    yield add_band_selection


@pytest.fixture(scope="session")
def populate_lfp_band(lfp_band, add_band_selection):
    lfp_band.LFPBandV1().populate(add_band_selection)
    yield


@pytest.fixture(scope="session")
def mini_eseries(common, mini_copy_name):
    yield (common.Raw() & {"nwb_file_name": mini_copy_name}).fetch_nwb()[0][
        "raw"
    ]
