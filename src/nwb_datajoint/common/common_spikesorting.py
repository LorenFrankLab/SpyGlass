from copy import Error
import json
import os
import pathlib
import re
import tempfile
import time
from pathlib import Path
import shutil

import datajoint as dj
import kachery_client as kc
import numpy as np
import pandas as pd
import pynwb
import scipy.stats as stats
import sortingview as sv
import spikeinterface as si
import spikeinterface.extractors as se
import spikeinterface.sorters as ss
import spikeinterface.toolkit as st
# from mountainsort4.mdaio_impl import readmda

from .common_device import Probe
from .common_lab import LabMember, LabTeam
from .common_ephys import Electrode, ElectrodeGroup, Raw
from .common_interval import (IntervalList, SortInterval,
                              interval_list_excludes_ind,
                              interval_list_intersect)
from .common_nwbfile import AnalysisNwbfile, Nwbfile
from .common_session import Session
from .dj_helper_fn import dj_replace, fetch_nwb
from .nwb_helper_fn import get_valid_intervals
from .sortingview_utils import add_to_sortingview_workspace, set_workspace_permission

si.set_global_tmp_folder('/stelmo/nwb/tmp')

class Timer:
    """
    Timer context manager for measuring time taken by each sorting step
    """

    def __init__(self, *, label='', verbose=False):
        self._label = label
        self._start_time = None
        self._stop_time = None
        self._verbose = verbose

    def elapsed(self):
        if self._stop_time is None:
            return time.time() - self._start_time
        else:
            return self._stop_time - self._start_time

    def __enter__(self):
        self._start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self._stop_time = time.time()
        if self._verbose:
            print(f"Elapsed time for {self._label}: {self.elapsed()} sec")

schema = dj.schema('common_spikesorting')

@schema
class SortGroup(dj.Manual):
    definition = """
    # Table for holding the set of electrodes that will be sorted together
    -> Session
    sort_group_id : int  # identifier for a group of electrodes
    ---
    sort_reference_electrode_id = -1 : int  # the electrode to use for reference. -1: no reference, -2: common median
    """

    class SortGroupElectrode(dj.Part):
        definition = """
        -> master
        -> Electrode
        """

    def set_group_by_shank(self, nwb_file_name, references=None):
        """
        Adds sort group entries in SortGroup table based on shank
        Assigns groups to all non-bad channel electrodes based on their shank:
        - Electrodes from probes with 1 shank (e.g. tetrodes) are placed in a
            single group
        - Electrodes from probes with multiple shanks (e.g. polymer probes) are
            placed in one group per shank

        Parameters
        ----------
        nwb_file_name : str
            the name of the NWB file whose electrodes should be put into sorting groups
        references : dict
            Optional. If passed, used to set references. Otherwise, references set using
            original reference electrodes from config. Keys: electrode groups. Values: reference electrode.
        """
        # delete any current groups
        (SortGroup & {'nwb_file_name': nwb_file_name}).delete()
        # get the electrodes from this NWB file
        electrodes = (Electrode() & {'nwb_file_name': nwb_file_name} & {
                      'bad_channel': 'False'}).fetch()
        e_groups = list(np.unique(electrodes['electrode_group_name']))
        e_groups.sort(key=int)  # sort electrode groups numerically
        sort_group = 0
        sg_key = dict()
        sge_key = dict()
        sg_key['nwb_file_name'] = sge_key['nwb_file_name'] = nwb_file_name
        for e_group in e_groups:
            # for each electrode group, get a list of the unique shank numbers
            shank_list = np.unique(
                electrodes['probe_shank'][electrodes['electrode_group_name'] == e_group])
            sge_key['electrode_group_name'] = e_group
            # get the indices of all electrodes in this group / shank and set their sorting group
            for shank in shank_list:
                sg_key['sort_group_id'] = sge_key['sort_group_id'] = sort_group
                # specify reference electrode. Use 'references' if passed, otherwise use reference from config
                if not references:
                    shank_elect_ref = electrodes['original_reference_electrode'][np.logical_and(electrodes['electrode_group_name'] == e_group,
                                                                                            electrodes['probe_shank'] == shank)]
                    if np.max(shank_elect_ref) == np.min(shank_elect_ref):
                        sg_key['sort_reference_electrode_id'] = shank_elect_ref[0]
                    else:
                        ValueError(
                            f'Error in electrode group {e_group}: reference electrodes are not all the same')
                else:
                    if e_group not in references.keys():
                        raise Exception(f"electrode group {e_group} not a key in references, so cannot set reference")
                    else:
                        sg_key['sort_reference_electrode_id'] = references[e_group]
                self.insert1(sg_key)

                shank_elect = electrodes['electrode_id'][np.logical_and(electrodes['electrode_group_name'] == e_group,
                                                                        electrodes['probe_shank'] == shank)]
                for elect in shank_elect:
                    sge_key['electrode_id'] = elect
                    self.SortGroupElectrode().insert1(sge_key)
                sort_group += 1

    def set_group_by_electrode_group(self, nwb_file_name):
        '''
        :param: nwb_file_name - the name of the nwb whose electrodes should be put into sorting groups
        :return: None
        Assign groups to all non-bad channel electrodes based on their electrode group and sets the reference for each group
        to the reference for the first channel of the group.
        '''
        # delete any current groups
        (SortGroup & {'nwb_file_name': nwb_file_name}).delete()
        # get the electrodes from this NWB file
        electrodes = (Electrode() & {'nwb_file_name': nwb_file_name} & {
                      'bad_channel': 'False'}).fetch()
        e_groups = np.unique(electrodes['electrode_group_name'])
        sg_key = dict()
        sge_key = dict()
        sg_key['nwb_file_name'] = sge_key['nwb_file_name'] = nwb_file_name
        sort_group = 0
        for e_group in e_groups:
            sge_key['electrode_group_name'] = e_group
            sg_key['sort_group_id'] = sge_key['sort_group_id'] = sort_group
            # get the list of references and make sure they are all the same
            shank_elect_ref = electrodes['original_reference_electrode'][electrodes['electrode_group_name'] == e_group]
            if np.max(shank_elect_ref) == np.min(shank_elect_ref):
                sg_key['sort_reference_electrode_id'] = shank_elect_ref[0]
            else:
                ValueError(
                    f'Error in electrode group {e_group}: reference electrodes are not all the same')
            self.insert1(sg_key)

            shank_elect = electrodes['electrode_id'][electrodes['electrode_group_name'] == e_group]
            for elect in shank_elect:
                sge_key['electrode_id'] = elect
                self.SortGroupElectrode().insert1(sge_key)
            sort_group += 1

    def set_reference_from_list(self, nwb_file_name, sort_group_ref_list):
        '''
        Set the reference electrode from a list containing sort groups and reference electrodes
        :param: sort_group_ref_list - 2D array or list where each row is [sort_group_id reference_electrode]
        :param: nwb_file_name - The name of the NWB file whose electrodes' references should be updated
        :return: Null
        '''
        key = dict()
        key['nwb_file_name'] = nwb_file_name
        sort_group_list = (SortGroup() & key).fetch1()
        for sort_group in sort_group_list:
            key['sort_group_id'] = sort_group
            self.insert(dj_replace(sort_group_list, sort_group_ref_list,
                                    'sort_group_id', 'sort_reference_electrode_id'),
                                             replace="True")

    def get_geometry(self, sort_group_id, nwb_file_name):
        """
        Returns a list with the x,y coordinates of the electrodes in the sort group
        for use with the SpikeInterface package. Converts z locations to y where appropriate
        :param sort_group_id: the id of the sort group
        :param nwb_file_name: the name of the nwb file for the session you wish to use
        :param prb_file_name: the name of the output prb file
        :return: geometry: list of coordinate pairs, one per electrode
        """

        # create the channel_groups dictiorary
        channel_group = dict()
        key = dict()
        key['nwb_file_name'] = nwb_file_name
        sort_group_list = (SortGroup() & key).fetch('sort_group_id')
        max_group = int(np.max(np.asarray(sort_group_list)))
        electrodes = (Electrode() & key).fetch()

        key['sort_group_id'] = sort_group_id
        sort_group_electrodes = (SortGroup.SortGroupElectrode() & key).fetch()
        electrode_group_name = sort_group_electrodes['electrode_group_name'][0]
        probe_type = (ElectrodeGroup & {'nwb_file_name': nwb_file_name,
                                        'electrode_group_name': electrode_group_name}).fetch1('probe_type')
        channel_group[sort_group_id] = dict()
        channel_group[sort_group_id]['channels'] = sort_group_electrodes['electrode_id'].tolist()

        label = list()
        n_chan = len(channel_group[sort_group_id]['channels'])

        geometry = np.zeros((n_chan, 2), dtype='float')
        tmp_geom = np.zeros((n_chan, 3), dtype='float')
        for i, electrode_id in enumerate(channel_group[sort_group_id]['channels']):
            # get the relative x and y locations of this channel from the probe table
            probe_electrode = int(
                electrodes['probe_electrode'][electrodes['electrode_id'] == electrode_id])
            rel_x, rel_y, rel_z = (Probe().Electrode() & {'probe_type': probe_type,
                                                          'probe_electrode': probe_electrode}).fetch('rel_x', 'rel_y', 'rel_z')
            # TODO: Fix this HACK when we can use probeinterface:
            rel_x = float(rel_x)
            rel_y = float(rel_y)
            rel_z = float(rel_z)
            tmp_geom[i, :] = [rel_x, rel_y, rel_z]

        # figure out which columns have coordinates
        n_found = 0
        for i in range(3):
            if np.any(np.nonzero(tmp_geom[:, i])):
                if n_found < 2:
                    geometry[:, n_found] = tmp_geom[:, i]
                    n_found += 1
                else:
                    Warning(
                        f'Relative electrode locations have three coordinates; only two are currenlty supported')
        return np.ndarray.tolist(geometry)


@schema
class SpikeSorter(dj.Manual):
    definition = """
    # Table that holds the list of spike sorters avaialbe through spikeinterface
    sorter_name: varchar(80) # the name of the spike sorting algorithm
    """

    def insert_from_spikeinterface(self):
        '''
        Add each of the sorters from spikeinterface.sorters
        :return: None
        '''
        sorters = ss.available_sorters()
        for sorter in sorters:
            self.insert1({'sorter_name': sorter}, skip_duplicates="True")


@schema
class SpikeSorterParameters(dj.Manual):
    definition = """
    -> SpikeSorter
    parameter_set_name: varchar(80) # label for this set of parameters
    ---
    parameter_dict: blob # dictionary of parameter names and values
    filter_parameter_dict: blob # dictionary of filter parameter names and
    """

    def insert_from_spikeinterface(self):
        '''
        Add each of the default parameter dictionaries from spikeinterface.sorters
        :return: None
        '''
        # set up the default filter parameters
        frequency_min = 300  # high pass filter value
        frequency_max = 6000  # low pass filter value
        filter_width = 1000  # the number of coefficients in the filter
        filter_chunk_size = 2000000  # the size of the chunk for the filtering

        sort_param_dict = dict()
        sort_param_dict['parameter_set_name'] = 'default'
        sort_param_dict['filter_parameter_dict'] = {'frequency_min': frequency_min,
                                                    'frequency_max': frequency_max,
                                                    'filter_width': filter_width,
                                                    'filter_chunk_size': filter_chunk_size}
        sorters = ss.available_sorters()
        for sorter in sorters:
            if len((SpikeSorter() & {'sorter_name': sorter}).fetch()):
                sort_param_dict['sorter_name'] = sorter
                sort_param_dict['parameter_dict'] = ss.get_default_params(
                    sorter)
                self.insert1(sort_param_dict, skip_duplicates=True)
            else:
                print(
                    f'Error in SpikeSorterParameter: sorter {sorter} not in SpikeSorter schema')
                continue


@schema
class SpikeSortingWaveformParameters(dj.Manual):
    definition = """
    waveform_parameters_name: varchar(80) # the name for this set of waveform extraction parameters
    ---
    waveform_parameter_dict: blob # a dictionary containing the SpikeInterface waveform parameters
    """


@schema
class SpikeSortingMetrics(dj.Manual):
    definition = """
    # Table for holding the parameters for computing quality metrics
    cluster_metrics_list_name: varchar(80) # the name for this list of cluster metrics
    ---
    metric_dict: blob            # dict of SpikeInterface metrics with True / False elements to indicate whether a given metric should be computed.
    metric_parameter_dict: blob  # dict of parameters for the metrics
    """

    def get_metric_dict(self):
        """Get the current list of metrics from spike interface and create a
        dictionary with all False elemnets.
        Users should set the desired set of metrics to be true and insert a new
        entry for that set.
        """
        metrics_list = st.validation.get_quality_metrics_list()
        metric_dict = {metric: False for metric in metrics_list}
        return metric_dict

    def get_metric_parameter_dict(self):
        """
        Get params for the metrics specified in the metric dict

        Parameters
        ----------
        metric_dict: dict
          a dictionary in which a key is the name of a quality metric and the value
          is a boolean
        """
        # TODO replace with call to spiketoolkit when available
        metric_params_dict = {'isi_threshold': 0.003,                 # Interspike interval threshold in s for ISI metric (default 0.003)
                              # SNR mode: median absolute deviation ('mad) or standard deviation ('std') (default 'mad')
                              'snr_mode': 'mad',
                              # length of data to use for noise estimation (default 10.0)
                              'snr_noise_duration': 10.0,
                              # Maximum number of spikes to compute templates for SNR from (default 1000)
                              'max_spikes_per_unit_for_snr': 1000,
                              # Use 'mean' or 'median' to compute templates
                              'template_mode': 'mean',
                              # direction of the maximum channel peak: 'both', 'neg', or 'pos' (default 'both')
                              'max_channel_peak': 'both',
                              # Maximum number of spikes to compute templates for noise overlap from (default 1000)
                              'max_spikes_per_unit_for_noise_overlap': 1000,
                              # Number of features to use for PCA for noise overlap
                              'noise_overlap_num_features': 5,
                              # Number of nearest neighbors for noise overlap
                              'noise_overlap_num_knn': 1,
                              # length of period in s for evaluating drift (default 60 s)
                              'drift_metrics_interval_s': 60,
                              # Minimum number of spikes in an interval for evaluation of drift (default 10)
                              'drift_metrics_min_spikes_per_interval': 10,
                              # Max spikes to be used for silhouette metric
                              'max_spikes_for_silhouette': 1000,
                              # Number of channels to be used for the PC extraction and comparison (default 7)
                              'num_channels_to_compare': 7,
                              'max_spikes_per_cluster': 1000,         # Max spikes to be used from each unit
                              # Max spikes to be used for nearest-neighbors calculation
                              'max_spikes_for_nn': 1000,
                              # number of nearest clusters to use for nearest neighbor calculation (default 4)
                              'n_neighbors': 4,
                              # number of parallel jobs (default 96 in spiketoolkit, changed to 24)
                              'n_jobs': 24,
                              # If True, waveforms are saved as memmap object (recommended for long recordings with many channels)
                              'memmap': False,
                              'max_spikes_per_unit': 2000,            # Max spikes to use for computing waveform
                              'seed': 47,                             # Random seed for reproducibility
                              'verbose': True}                        # If nonzero (True), will be verbose in metric computation
        return metric_params_dict

    def get_default_metrics_entry(self):
        """
        Re-inserts the entry for Frank lab default parameters
        (run in case it gets accidentally deleted)
        """
        cluster_metrics_list_name = 'franklab_default_cluster_metrics'
        metric_dict = self.get_metric_dict()
        metric_dict['firing_rate'] = True
        metric_dict['nn_hit_rate'] = True
        metric_dict['noise_overlap'] = True
        metric_parameter_dict = self.get_metric_parameter_dict()
        self.insert1([cluster_metrics_list_name, metric_dict,
                      metric_parameter_dict], replace=True)

    @staticmethod
    def selected_metrics_list(metric_dict):
        return [metric for metric in metric_dict.keys() if metric_dict[metric]]

    def validate_metrics_list(self, key):
        """ Checks whether metrics_list contains only valid metric names

        :param key: key for metrics to validate
        :type key: dict
        :return: True or False
        :rtype: boolean
        """
        # TODO: get list of valid metrics from spiketoolkit when available
        valid_metrics = self.get_metric_dict()
        metric_dict = (self & key).fetch1('metric_dict')
        valid = True
        for metric in metric_dict:
            if not metric in valid_metrics.keys():
                print(
                    f'Error: {metric} not in list of valid metrics: {valid_metrics}')
                valid = False
        return valid

    def compute_metrics(self, cluster_metrics_list_name, recording, sorting):
        """
        Use spikeinterface to compute the list of selected metrics for a sorting

        Parameters
        ----------
        cluster_metrics_list_name: str
        recording: spikeinterface RecordingExtractor
        sorting: spikeinterface SortingExtractor

        Returns
        -------
        metrics: pandas.dataframe
        """
        m = (self & {'cluster_metrics_list_name': cluster_metrics_list_name}).fetch1()

        return st.qualitymetrics.compute_quality_metrics(sorting=sorting,
                                                         recording=recording,
                                                         metric_names=self.selected_metrics_list(
                                                         m['metric_dict']),
                                                         as_dataframe=True,
                                                         **m['metric_parameter_dict'])


@schema
class SpikeSortingArtifactParameters(dj.Manual):
    definition = """
    # Table for holding parameters related to artifact detection
    artifact_param_name: varchar(200) #name for this set of parameters
    ---
    parameter_dict: BLOB    # dictionary of parameters for get_no_artifact_times() function
    """

    def get_no_artifact_times(self, recording, zscore_thresh=-1.0, amplitude_thresh=-1.0,
                              proportion_above_thresh=1.0, zero_window_len=1.0, skip: bool=True):
        """returns an interval list of valid times, excluding detected artifacts found in data within recording extractor.
        Artifacts are defined as periods where the absolute amplitude of the signal exceeds one
        or both specified thresholds on the proportion of channels specified, with the period extended
        by the zero_window/2 samples on each side
        Threshold values <0 are ignored.

        :param recording: recording extractor
        :type recording: SpikeInterface recording extractor object
        :param zscore_thresh: Stdev threshold for exclusion, defaults to -1.0
        :type zscore_thresh: float, optional
        :param amplitude_thresh: Amplitude threshold for exclusion, defaults to -1.0
        :type amplitude_thresh: float, optional
        :param proportion_above_thresh:
        :type float, optional
        :param zero_window_len: the width of the window in milliseconds to zero out (window/2 on each side of threshold crossing)
        :type int, optional
        :return: [array of valid times]
        :type: [numpy array]
        """

        # if no thresholds were specified, we return an array with the timestamps of the first and last samples
        if zscore_thresh <= 0 and amplitude_thresh <= 0:
            return np.asarray([[recording._timestamps[0], recording._timestamps[recording.get_num_frames()]]])

        half_window_points = np.round(
            recording.get_sampling_frequency() * 1000 * zero_window_len / 2)
        nelect_above = np.round(proportion_above_thresh * data.shape[0])
        # get the data traces
        data = recording.get_traces()

        # compute the number of electrodes that have to be above threshold based on the number of rows of data
        nelect_above = np.round(
            proportion_above_thresh * len(recording.get_channel_ids()))

        # apply the amplitude threshold
        above_a = np.abs(data) > amplitude_thresh

        # zscore the data and get the absolute value for thresholding
        dataz = np.abs(stats.zscore(data, axis=1))
        above_z = dataz > zscore_thresh

        above_both = np.ravel(np.argwhere(
            np.sum(np.logical_and(above_z, above_a), axis=0) >= nelect_above))
        valid_timestamps = recording._timestamps
        # for each above threshold point, set the timestamps on either side of it to -1
        for a in above_both:
            valid_timestamps[a - half_window_points:a +
                             half_window_points] = -1

        # use get_valid_intervals to find all of the resulting valid times.
        return get_valid_intervals(valid_timestamps[valid_timestamps != -1], recording.get_sampling_frequency(), 1.5, 0.001)


@schema
class SpikeSortingParameters(dj.Manual):
    definition = """
    # Table for holding parameters for each spike sorting run
    -> SortGroup
    -> SpikeSorterParameters
    -> SortInterval
    ---
    -> SpikeSortingArtifactParameters
    -> SpikeSortingMetrics
    -> IntervalList
    -> LabTeam
    import_path = '': varchar(200) # optional path to previous curated sorting output
    """


@schema
class SpikeSorting(dj.Computed):
    definition = """
    # Table for holding spike sorting runs
    -> SpikeSortingParameters
    ---
    -> AnalysisNwbfile
    units_object_id: varchar(40)           # Object ID for the units in NWB file
    time_of_sort=0: int                    # This is when the sort was done
    curation_feed_uri='': varchar(1000)    # Labbox-ephys feed for curation
    sorting_id='none': varchar(20)         # the id of the sorting that was added
    """

    def make(self, key):
        """
        Runs spike sorting on the data and parameters specified by the
        SpikeSortingParameter table and inserts a new entry to SpikeSorting table.

        Parameters
        ----------
        key: dict
            primary keys from SpikeSortingParameters
        """
        team_name = (SpikeSortingParameters & key).fetch1('team_name')
        key['analysis_file_name'] = AnalysisNwbfile().create(key['nwb_file_name'])

        sort_interval = (SortInterval & {'nwb_file_name': key['nwb_file_name'],
                                         'sort_interval_name': key['sort_interval_name']}).fetch1('sort_interval')
        sort_interval_valid_times = self.get_sort_interval_valid_times(key)

        # TODO: finish `import_sorted_data` function below

        with Timer(label='getting filtered recording extractor', verbose=True):
            recording = self.get_filtered_recording_extractor(key)
            recording_timestamps = recording._timestamps

        # get the artifact detection parameters and apply artifact detection to zero out artifacts
        artifact_key = (SpikeSortingParameters & key).fetch1('artifact_param_name')
        artifact_param_dict = (SpikeSortingArtifactParameters & {'artifact_param_name': artifact_key}).fetch1('parameter_dict')
        if not artifact_param_dict['skip']:
            no_artifact_valid_times = SpikeSortingArtifactParameters.get_no_artifact_times(recording, **artifact_param_dict)
            # update the sort interval valid times to exclude the artifacts
            sort_interval_valid_times = interval_list_intersect(
                sort_interval_valid_times, no_artifact_valid_times)
            # exclude the invalid times
            artifact_frames = interval_list_excludes_ind(sort_interval_valid_times, recording_timestamps)
            recording = st.remove_artifacts(recording, artifact_frames,
                                            ms_before=0, ms_after=0)
        
        # Save filtered recording to binary
        recording = recording.save()
        
        # Save filtered recording to NWB
        # metadata = {}
        # metadata['Ecephys'] = {'ElectricalSeries': {'name': 'ElectricalSeries',
        #                                             'description': key['nwb_file_name'] +
        #                                             '_' + key['sort_interval_name'] + 
        #                                             '_' + str(key['sort_group_id'])}}
        # se.NwbRecordingExtractor.write_recording(recording, save_path=recording_h5_path,
        #                                          buffer_mb=10000, overwrite=True, metadata=metadata,
        #                                          es_key='ElectricalSeries')
        
        recording_h5_path, sorting_h5_path = self.get_extractor_save_path(key, type='h5v1')
        
        # Save filtered recording to h5 recording
        recording = sv.LabboxEphysRecordingExtractor.store_recording_link_h5(recording, recording_h5_path)

        # whiten the extractor for sorting and metric calculations
        print('\nWhitening recording...')
        with Timer(label=f'whitening', verbose=True):
            filter_params = (SpikeSorterParameters & {'sorter_name': key['sorter_name'],
                                                      'parameter_set_name': key['parameter_set_name']}).fetch1('filter_parameter_dict')
            recording = st.preprocessing.whiten(recording, seed=0, chunk_size=filter_params['filter_chunk_size'])

        print(f'\nRunning spike sorting on {key}...')
        sorter_parameters = (SpikeSorterParameters & {'sorter_name': key['sorter_name'],
                                                    'parameter_set_name': key['parameter_set_name']}).fetch1()

        sorting = ss.run_sorter(key['sorter_name'], recording,
                                output_folder=os.getenv('SORTING_TEMP_DIR', None),
                                **sorter_parameters['parameter_dict'])

        key['time_of_sort'] = int(time.time())

        # Save sorting as NWB or binary
        # se.NwbSortingExtractor.write_sorting(
        #     sorting, save_path=extractor_nwb_path)
        # sorting = sorting.save(folder=analysis_path+'_sorting')

        # Save sorting as H5
        sorting = sv.LabboxEphysSortingExtractor.store_sorting_link_h5(sorting, sorting_h5_path)

        cluster_metrics_list_name = (SpikeSortingParameters & key).fetch1(
                'cluster_metrics_list_name')
        # TODO: change using new spikeinterface
        with Timer(label='computing quality metrics', verbose=True):
            metrics = SpikeSortingMetrics().compute_metrics(cluster_metrics_list_name, recording, sorting)

        print('\nSaving sorting results...')
        units = dict()
        units_valid_times = dict()
        units_sort_interval = dict()
        unit_ids = sorting.get_unit_ids()
        for unit_id in unit_ids:
            spike_times_in_samples = sorting.get_unit_spike_train(
                unit_id=unit_id)
            units[unit_id] = recording_timestamps[spike_times_in_samples]
            units_valid_times[unit_id] = sort_interval_valid_times
            units_sort_interval[unit_id] = [sort_interval]

        units_object_id, _ = AnalysisNwbfile().add_units(key['analysis_file_name'],
                                                         units, units_valid_times,
                                                         units_sort_interval,
                                                         metrics=metrics)

        AnalysisNwbfile().add(key['nwb_file_name'], key['analysis_file_name'])
        key['units_object_id'] = units_object_id

        print('\nGenerating feed for curation...')
        workspace_name = key['analysis_file_name']
        recording_label = key['nwb_file_name'] + '_' + \
            key['sort_interval_name'] + '_' + str(key['sort_group_id'])
        sorting_label = key['sorter_name'] + '_' + key['parameter_set_name'] + '_' \
                        + cluster_metrics_list_name

        workspace_uri, sorting_id = add_to_sortingview_workspace(workspace_name, recording_label, 
                                                                 sorting_label, recording, sorting, 
                                                                 analysis_nwb_path=None,
                                                                 metrics=metrics)

        key['sorting_id'] = sorting_id      
        key['curation_feed_uri'] = workspace_uri
        
        # Give permission to workspace based on Google account
        team_members = (LabTeam.LabTeamMember & {'team_name': team_name}).fetch('lab_member_name')
        set_workspace_permission(workspace_name, team_members)
        
        self.insert1(key)
        print('\nDone - entry inserted to table.')

    @staticmethod
    def get_extractor_save_path(key: dict, type: str='h5v1'):
        """
        Returns the paths for recording and sorting extractors to be saved

        Parameters
        ----------
        key: dict
            Key from SpikeSorting table
        type: str, optional
            Type of extractor. Currently 'h5v1' or 'nwb" are supported. Defaults to 'h5v1'.
        """
        supported_types = ['h5v1', 'nwb', 'folder']
        if type not in supported_types:
            raise Error(f'extractor type {type} not in supported types {supported_types}')
        
        # Path to files that will hold recording and sorting extractors
        extractor_base_name = key['nwb_file_name'] \
            + '_' + key['sort_interval_name'] \
            + '_' + str(key['sort_group_id']) \
            + '_' + key['sorter_name'] \
            + '_' + key['parameter_set_name']
        analysis_path = str(Path(os.environ['SPIKE_SORTING_STORAGE_DIR'])
                            / key['analysis_file_name'])

        if not os.path.isdir(analysis_path):
            os.mkdir(analysis_path)
        full_path = str(Path(analysis_path) / extractor_base_name)
        
        if type == 'h5v1': 
            recording_path = full_path + '_recording.' + type
            sorting_path = full_path + '_sorting.' + type
        elif type == 'nwb':
            recording_path = full_path + '.' + type
            sorting_path = recording_path
        elif type == 'folder':
            recording_path = full_path + '_recording'
            sorting_path = full_path + '_sorting'
            
        return recording_path, sorting_path

    def delete(self):
        """
        Extends the delete method of base class to implement permission checking
        """
        current_user_name = dj.config['database.user']
        entries = self.fetch()
        permission_bool = np.zeros((len(entries),))
        print(f'Attempting to delete {len(entries)} entries, checking permission...')
    
        for entry_idx in range(len(entries)):
            # check the team name for the entry, then look up the members in that team, then get their datajoint user names
            team_name = (SpikeSortingParameters & (SpikeSortingParameters & entries[entry_idx]).proj()).fetch1()['team_name']
            lab_member_name_list = (LabTeam.LabTeamMember & {'team_name': team_name}).fetch('lab_member_name')
            datajoint_user_names = []
            for lab_member_name in lab_member_name_list:
                datajoint_user_names.append((LabMember.LabMemberInfo & {'lab_member_name': lab_member_name}).fetch1('datajoint_user_name'))
            permission_bool[entry_idx] = current_user_name in datajoint_user_names
        if np.sum(permission_bool)==len(entries):
            print('Permission to delete all specified entries granted.')
            super().delete()
        else:
            raise Exception('You do not have permission to delete all specified entries. Not deleting anything.')
    
    def get_stored_recording_sorting(self, key):
        """Retrieves the stored recording and sorting extractors given the key to a SpikeSorting

        Args:
            key (dict): key to retrieve one SpikeSorting entry
        """
        # TODO write this function

    def fetch_nwb(self, *attrs, **kwargs):
        return fetch_nwb(self, (AnalysisNwbfile, 'analysis_file_abs_path'), *attrs, **kwargs)

    def get_sort_interval_valid_times(self, key):
        """
        Identifies the intersection between sort interval specified by the user
        and the valid times (times for which neural data exist)

        Parameters
        ----------
        key: dict
            specifies a (partially filled) entry of SpikeSorting table

        Returns
        -------
        sort_interval_valid_times: ndarray of tuples
            (start, end) times for valid stretches of the sorting interval
        """
        sort_interval = (SortInterval & {'nwb_file_name': key['nwb_file_name'],
                                         'sort_interval_name': key['sort_interval_name']}).fetch1('sort_interval')
        interval_list_name = (SpikeSortingParameters &
                              key).fetch1('interval_list_name')
        valid_times = (IntervalList & {'nwb_file_name': key['nwb_file_name'],
                                       'interval_list_name': interval_list_name}).fetch1('valid_times')
        sort_interval_valid_times = interval_list_intersect(
            sort_interval, valid_times)
        return sort_interval_valid_times

    def get_filtered_recording_extractor(self, key: dict):
        """
        Generates a RecordingExtractor object based on parameters in key.
        (1) Loads the inserted NWB file as a NwbRecordingExtractor
        (2) Applies referencing and bandpass filtering

        Parameters
        ----------
        key: dict

        Returns
        -------
        recording: spikeinterface.extractors.RecordingExtractor
        """
        
        with Timer(label='filtered recording extractor setup', verbose=True):
            nwb_file_abs_path = Nwbfile().get_abs_path(key['nwb_file_name'])
            with pynwb.NWBHDF5IO(nwb_file_abs_path, 'r', load_namespaces=True) as io:
                nwbfile = io.read()
                timestamps = nwbfile.acquisition['e-series'].timestamps[:]

            sort_interval = (SortInterval & {'nwb_file_name': key['nwb_file_name'],
                                             'sort_interval_name': key['sort_interval_name']}).fetch1('sort_interval')

            sort_indices = np.searchsorted(timestamps, np.ravel(sort_interval))
            assert sort_indices[1] - sort_indices[0] > 1000, f'Error in get_recording_extractor: sort indices {sort_indices} are not valid'

            electrode_ids = (SortGroup.SortGroupElectrode & {'nwb_file_name': key['nwb_file_name'],
                                                             'sort_group_id': key['sort_group_id']}).fetch('electrode_id')
            electrode_group_name = (SortGroup.SortGroupElectrode & {'nwb_file_name': key['nwb_file_name'],
                                                                    'sort_group_id': key['sort_group_id']}).fetch('electrode_group_name')
            electrode_group_name = np.int(electrode_group_name[0])
            
        with Timer(label='NWB recording extractor create from file', verbose=True):
            recording = se.read_nwb_recording(Nwbfile.get_abs_path(key['nwb_file_name']),
                                              electrical_series_name='e-series')

        sort_reference_electrode_id = (SortGroup & {'nwb_file_name': key['nwb_file_name'],
                                                    'sort_group_id': key['sort_group_id']}).fetch('sort_reference_electrode_id')
        sort_reference_electrode_id = np.int(sort_reference_electrode_id)
        
        # make a list of the channels in the sort group and the reference channel if it exists
        channel_ids = electrode_ids.tolist()
        if sort_reference_electrode_id >= 0:            
            channel_ids.append(sort_reference_electrode_id)

        # slice recording in frames and channels
        recording = recording.frame_slice(start_frame=sort_indices[0], end_frame=sort_indices[1])
        recording = recording.channel_slice(channel_ids=channel_ids)

        # Save as binary and reload (for speed)
        # NOTE: omitted as this does not speed things up anymore in si v0.90
        # recording = recording.save()
        
        if sort_reference_electrode_id >= 0:
            recording = st.preprocessing.common_reference(recording, reference='single',
                                                          ref_channels=sort_reference_electrode_id)
            # now restrict it to just the electrode IDs in the sort group
            recording = recording.channel_slice(channel_ids=electrode_ids.tolist())
        elif sort_reference_electrode_id == -2:
            recording = st.preprocessing.common_reference(recording, reference='median')

        filter_params = (SpikeSorterParameters & {'sorter_name': key['sorter_name'],
                                                  'parameter_set_name': key['parameter_set_name']}).fetch1('filter_parameter_dict')
        recording = st.preprocessing.bandpass_filter(recording, freq_min=filter_params['frequency_min'],
                                                     freq_max=filter_params['frequency_max'])

        # TODO: change this with spikeinterface.probe
        # recording.set_channel_locations(SortGroup().get_geometry(key['sort_group_id'], key['nwb_file_name']))

        # set timestamps
        # TODO: change this once spikeextractors is updated
        recording._timestamps = timestamps[sort_indices[0]:sort_indices[1]]

        return recording

    @staticmethod
    def get_recording_timestamps(key):
        """Returns the timestamps for the specified SpikeSorting entry

        Args:
            key (dict): the SpikeSorting key
        Returns:
            timestamps (numpy array)
        """
        nwb_file_abs_path = Nwbfile().get_abs_path(key['nwb_file_name'])
        # TODO fix to work with any electrical series object
        with pynwb.NWBHDF5IO(nwb_file_abs_path, 'r', load_namespaces=True) as io:
            nwbfile = io.read()
            timestamps = nwbfile.acquisition['e-series'].timestamps[:]

        sort_interval = (SortInterval & {'nwb_file_name': key['nwb_file_name'],
                                         'sort_interval_name': key['sort_interval_name']}).fetch1('sort_interval')

        sort_indices = np.searchsorted(timestamps, np.ravel(sort_interval))
        timestamps = timestamps[sort_indices[0]:sort_indices[1]]
        return timestamps

    def get_sorting_extractor(self, key, sort_interval):
        # TODO: replace with spikeinterface call if possible
        """Generates a numpy sorting extractor given a key that retrieves a SpikeSorting and a specified sort interval

        :param key: key for a single SpikeSorting
        :type key: dict
        :param sort_interval: [start_time, end_time]
        :type sort_interval: numpy array
        :return: a spikeextractors sorting extractor with the sorting information
        """
        # get the units object from the NWB file that the data are stored in.
        units = (SpikeSorting & key).fetch_nwb()[0]['units'].to_dataframe()
        unit_timestamps = []
        unit_labels = []

        raw_data_obj = (Raw() & {'nwb_file_name': key['nwb_file_name']}).fetch_nwb()[
            0]['raw']
        # get the indices of the data to use. Note that spike_extractors has a time_to_frame function,
        # but it seems to set the time of the first sample to 0, which will not match our intervals
        timestamps = np.asarray(raw_data_obj.timestamps)
        sort_indices = np.searchsorted(timestamps, np.ravel(sort_interval))

        unit_timestamps_list = []
        # TODO: do something more efficient here; note that searching for maching sort_intervals within pandas doesn't seem to work
        for index, unit in units.iterrows():
            if np.ndarray.all(np.ravel(unit['sort_interval']) == sort_interval):
                # unit_timestamps.extend(unit['spike_times'])
                unit_frames = np.searchsorted(
                    timestamps, unit['spike_times']) - sort_indices[0]
                unit_timestamps.extend(unit_frames)
                # unit_timestamps_list.append(unit_frames)
                unit_labels.extend([index] * len(unit['spike_times']))

        output = se.NumpySortingExtractor()
        output.set_times_labels(times=np.asarray(
            unit_timestamps), labels=np.asarray(unit_labels))
        return output

    # TODO: write a function to import sorted data
    def import_sorted_data():
        # Check if spikesorting has already been run on this dataset;
        # if import_path is not empty, that means there exists a previous spikesorting run
        import_path = (SpikeSortingParameters() & key).fetch1('import_path')
        if import_path != '':
            sort_path = Path(import_path)
            assert sort_path.exists(
            ), f'Error: import_path {import_path} does not exist when attempting to import {(SpikeSortingParameters() & key).fetch1()}'
            # the following assumes very specific file names from the franklab, change as needed
            firings_path = sort_path / 'firings_processed.mda'
            assert firings_path.exists(
            ), f'Error: {firings_path} does not exist when attempting to import {(SpikeSortingParameters() & key).fetch1()}'
            # The firings has three rows, the electrode where the peak was detected, the sample count, and the cluster ID
            firings = readmda(str(firings_path))
            # get the clips
            clips_path = sort_path / 'clips.mda'
            assert clips_path.exists(
            ), f'Error: {clips_path} does not exist when attempting to import {(SpikeSortingParameters() & key).fetch1()}'
            clips = readmda(str(clips_path))
            # get the timestamps corresponding to this sort interval
            # TODO: make sure this works on previously sorted data
            timestamps = timestamps[np.logical_and(
                timestamps >= sort_interval[0], timestamps <= sort_interval[1])]
            # get the valid times for the sort_interval
            sort_interval_valid_times = interval_list_intersect(
                np.array([sort_interval]), valid_times)

            # get a list of the cluster numbers
            unit_ids = np.unique(firings[2, :])
            for index, unit_id in enumerate(unit_ids):
                unit_indices = np.ravel(np.argwhere(firings[2, :] == unit_id))
                units[unit_id] = timestamps[firings[1, unit_indices]]
                units_templates[unit_id] = np.mean(
                    clips[:, :, unit_indices], axis=2)
                units_valid_times[unit_id] = sort_interval_valid_times
                units_sort_interval[unit_id] = [sort_interval]

            # TODO: Process metrics and store in Units table.
            metrics_path = (sort_path / 'metrics_processed.json').exists()
            assert metrics_path.exists(
            ), f'Error: {metrics_path} does not exist when attempting to import {(SpikeSortingParameters() & key).fetch1()}'
            metrics_processed = json.load(metrics_path)

    def nightly_cleanup(self):
        """Clean up spike sorting directories that are not in the SpikeSorting table. 
        This should be run after AnalysisNwbFile().nightly_cleanup()

        :return: None
        """
        # get a list of the files in the spike sorting storage directory
        dir_names = next(os.walk(os.environ['SPIKE_SORTING_STORAGE_DIR']))[1]
        # now retrieve a list of the currently used analysis nwb files
        analysis_file_names = self.fetch('analysis_file_name')
        for dir in dir_names:
            if not dir in analysis_file_names:
                full_path = str(pathlib.Path(os.environ['SPIKE_SORTING_STORAGE_DIR']) / dir)
                print(f'removing {full_path}')
                shutil.rmtree(str(pathlib.Path(os.environ['SPIKE_SORTING_STORAGE_DIR']) / dir))

@schema
class AutomaticCurationParameters(dj.Manual):
    definition = """
    # Table for holding parameters for automatic aspects of curation
    automatic_curation_param_name: varchar(80)   #name of this parameter set
    ---
    automatic_curation_param_dict: BLOB         #dictionary of variables and values for automatic curation
    """
    @staticmethod
    def get_default_parameters(): 
        """returns a dictionary with the parameters that can be defined 

        Returns:
            [dict]: dictionary of parameters
        """
        param_dict = dict()
        param_dict['delete_duplicate_spikes'] = False
        param_dict['burst_merge'] = False
        param_dict['burst_merge_param'] = dict()
        param_dict['noise_reject'] = False
        param_dict['noise_reject_param'] = dict()
        return param_dict


@schema
class AutomaticCurationSpikeSortingParameters(dj.Manual):
    definition = """
    # Table for holding the output

    -> SpikeSorting
    ---
    -> AutomaticCurationParameters
    -> SpikeSortingMetrics.proj(automatic_curation_cluster_metrics_list_name='cluster_metrics_list_name')
    """


@schema
class AutomaticCurationSpikeSorting(dj.Computed):
    definition = """
    # Table for holding the output of automated curation applied to each spike sorting
    -> AutomaticCurationSpikeSortingParameters
    ---
    -> AnalysisNwbfile
    units_object_id: varchar(40)           # Object ID for the units in NWB file
    curation_feed_uri='': varchar(1000)    # sv / figurl feed for curation; duplicated from SpikeSorting
    sorting_id: varchar(20)                # the sorting id of the new sorting that was added
    automatic_curation_results_dict=NULL: BLOB       #dictionary of outputs from automatic curation
    """

    def make(self, key):
        # LOGIC:
        #1. If requested, create and save new sorting with spikes removed (e.g. 'delete_duplicate_spikes' == True)
        #2. If new metrics specified, compute new metrics
        #3. Using metrics, add labels for burst merge, noise clusters, etc. 

        key['automatic_curation_results_dict'] = dict()
        ss_key = (SpikeSorting & key).fetch1()
        workspace_uri = key['curation_feed_uri'] = ss_key['curation_feed_uri']
        # load the workspace and the sorting
        workspace = sv.load_workspace(ss_key['curation_feed_uri'])
        sorting_id = ss_key['sorting_id']
        # check to see if there are multiple sortings, and if so, get just the first one
        if sorting_id == 'none':
            print(f'AutomaticCurationSpikeSorting: no sorting_id in SpikeSorting, using the first sorting.')            
            sorting_id = workspace.sorting_ids[0]
        sorting = workspace.get_sorting_extractor(sorting_id)
        recording_id = workspace.recording_ids[0]
        recording = workspace.get_recording_extractor(recording_id)

        auto_curate_param_name = (AutomaticCurationSpikeSortingParameters & key).fetch1('automatic_curation_param_name')
        acpd = (AutomaticCurationParameters & {'automatic_curation_param_name': auto_curate_param_name}).fetch1('automatic_curation_param_dict')
        # check for defined automatic curation keys / parameters
        
        #1. Create and save new sorting if requested
        sorting_modified = False
        metrics_modified = False
        analysis_file_created = False
        if 'delete_duplicate_spikes' in acpd:
            if acpd['delete_duplicate_spikes']:
                print('deleting duplicate spikes')
                # look up the detection interval 
                param_dict = (SpikeSorterParameters & key).fetch1('parameter_dict')
                if 'detect_interval' not in param_dict:
                    Warning(f'delete_duplicate_spikes enabled, but detect_interval is not specified in the spike sorter parameters {key["parameter_set_name"]}; skipping')
                else:
                    unit_samples = np.empty((0,),dtype='int64')
                    unit_labels = np.empty((0,),dtype='int64')
                    for unit in sorting.get_unit_ids():
                        tmp_samples = sorting.get_unit_spike_train(unit)
                        invalid = np.where(np.diff(tmp_samples) < param_dict['detect_interval'])[0]
                        tmp_samples = np.delete(tmp_samples, invalid)    
                        print(f'Unit {unit}: {len(invalid)} spikes deleted')
                        tmp_labels = np.asarray([unit]*len(tmp_samples))
                        unit_samples = np.hstack((unit_samples, tmp_samples))
                        unit_labels = np.hstack((unit_labels, tmp_labels))
                    # sort the times and labels
                    sort_ind = np.argsort(unit_samples)
                    unit_samples = unit_samples[sort_ind]
                    unit_labels = unit_labels[sort_ind]
                    # create a numpy sorting extractor
                    new_sorting = se.NumpySortingExtractor()
                    new_sorting.set_times_labels(times=unit_samples, labels=unit_labels.tolist())
                    new_sorting.set_sampling_frequency((Raw & key).fetch1('sampling_rate'))
                    sorting_modified = True

        # 2. check to see if there are updated metrics
        auto_curate_metrics_list = (AutomaticCurationSpikeSortingParameters() & key).fetch1('automatic_curation_cluster_metrics_list_name')
        orig_metrics_list = (SpikeSortingParameters() & key).fetch1('cluster_metrics_list_name')
        orig_units = (SpikeSorting & key).fetch_nwb()[0]['units'].to_dataframe()

        if sorting_modified or auto_curate_metrics_list != orig_metrics_list:
            #We need to recalculate the metrics.
            #First, whiten the recording     
            with Timer(label=f'whitening and computing new quality metrics', verbose=True):
                filter_params = (SpikeSorterParameters & {'sorter_name': key['sorter_name'],
                                                        'parameter_set_name': key['parameter_set_name']}).fetch1('filter_parameter_dict')
                recording = st.preprocessing.whiten(
                    recording, seed=0, chunk_size=filter_params['filter_chunk_size'])

                tmpfile = tempfile.NamedTemporaryFile(dir='/stelmo/nwb/tmp')
                metrics_recording = se.CacheRecordingExtractor(recording, save_path=tmpfile.name, chunk_mb=10000)
                cluster_metrics_list_name = (AutomaticCurationSpikeSortingParameters & key).fetch1('automatic_curation_cluster_metrics_list_name')
                metrics = SpikeSortingMetrics().compute_metrics(cluster_metrics_list_name, metrics_recording, sorting)  
                
            sorting_label = key['sorter_name'] + '_' + key['parameter_set_name'] + '_' + cluster_metrics_list_name
            
            # create the new AnalysisNwbfile for the new sorting / metrics
            key['analysis_file_name'] = AnalysisNwbfile().create(key['nwb_file_name'])

            if not sorting_modified:
                # add a duplicate sorting with the new metrics
                sorting_id = workspace.add_sorting(sorting=sorting, recording_id=recording_id, 
                                            label=sorting_label)
            else:     
                #  store new sorting extractor. 
                r_path, s_path = SpikeSorting.get_extractor_save_path(key, type='h5v1') 
                sorting_tmp = sv.LabboxEphysSortingExtractor.store_sorting_link_h5(new_sorting, s_path)
                # add to workspace
                sorting_label = key['sorter_name'] + '_' + key['parameter_set_name'] + '_' + cluster_metrics_list_name
                sorting_id = workspace.add_sorting(sorting=sorting_tmp, recording_id=recording_id, label=sorting_label)

             # Set external metrics that will appear in the units table
            external_metrics = [{'name': metric, 'label': metric, 'tooltip': metric,
                                'data': metrics[metric].to_dict()} for metric in metrics.columns]
            # change unit id to string
            for metric_ind in range(len(external_metrics)):
                for old_unit_id in metrics.index:
                    external_metrics[metric_ind]['data'][str(
                        old_unit_id)] = external_metrics[metric_ind]['data'].pop(old_unit_id)
                    # change nan to none so that json can handle it
                    if np.isnan(external_metrics[metric_ind]['data'][str(old_unit_id)]):
                        external_metrics[metric_ind]['data'][str(old_unit_id)] = None
            
            workspace.set_unit_metrics_for_sorting(
                        sorting_id=sorting_id, metrics=external_metrics)    

             # Save the units and their updated metrics
            # load the AnalysisNWBFile from the original sort to get the sort_interval_valid times and the sort_interval
            sort_interval = orig_units.iloc[1]['sort_interval']
            sort_interval_valid_times = orig_units.iloc[1]['obs_intervals']

            # add the units with the metrics and labels to the file.
            print('\nSaving new metrics...')
            timestamps = SpikeSorting.get_recording_timestamps(ss_key)
            units = dict()
            units_valid_times = dict()
            units_sort_interval = dict()
            unit_ids = sorting.get_unit_ids()
            unit_times_list = []
            for unit_id in unit_ids:
                # Note that we take the units from the sorting because it may have been changed above.
                spike_times_in_samples = sorting.get_unit_spike_train(unit_id=unit_id)
                units[unit_id] = timestamps[spike_times_in_samples]
                unit_times_list.append(units[unit_id])
                units_valid_times[unit_id] = sort_interval_valid_times
                units_sort_interval[unit_id] = [sort_interval]

            units_object_id, _ = AnalysisNwbfile().add_units(key['analysis_file_name'],
                                                            units, units_valid_times,
                                                            units_sort_interval,
                                                            metrics=metrics)
            # add the analysis file to the table
            AnalysisNwbfile().add(key['nwb_file_name'], key['analysis_file_name'])
            key['units_object_id'] = units_object_id
            # add the spike_times to the metrics for subsequent labeling  
            metrics['spike_times'] = unit_times_list

        else:
            key['analysis_file_name'] = ss_key['analysis_file_name']
            key['units_object_id'] = ss_key['units_object_id']
            metrics = orig_units
        
        key['sorting_id'] = sorting_id

        #3. add labels as requested for burst merges, noise, etc.
        # initialize the labels dictionary
        labels = dict()
        labels['mergeGroups'] = []
        # format: labels['mergeGroups'] = [[1, 2, 5], [3, 4]] would merge units 1,2,and 5 and, separately, 3 and4
        labels['labelsByUnit'] = dict()
        # format: labels['labelsByUnit'] = {1:'accept', 2:'noise,reject'}] would label unit 1 as 'accept' and unit 2 as 'noise' and 'reject'
        
        # List of available functions for spike waveform extraction: 
        # https://spikeinterface.readthedocs.io/en/0.13.0/api.html#module-spiketoolkit.postprocessing
        if 'burst_merge' in acpd:
            if acpd['burst_merge']:
                # get the burst_merge parameters
                burst_merge_param = acpd['burst_merge_param']
                #TODO: add burst merge code
        if 'noise_reject' in acpd:
            if acpd['noise_reject']:
                # get the noise rejection parameters
                noise_reject_param = acpd['noise_reject_param']
                #TODO write noise/ rejection code

        print(f'To curate the modified spike sorting, go to https://sortingview.vercel.app/workspace?workspace={key["curation_feed_uri"]}&channel=franklab')
                
        self.insert1(key)

    def fetch_nwb(self, *attrs, **kwargs):
        return fetch_nwb(self, (AnalysisNwbfile, 'analysis_file_abs_path'), *attrs, **kwargs)

    def delete(self):
        """
        Extends the delete method of base class to implement permission checking
        """
        current_user_name = dj.config['database.user']
        entries = self.fetch()
        permission_bool = np.zeros((len(entries),))
        print(f'Attempting to delete {len(entries)} entries, checking permission...')
    
        for entry_idx in range(len(entries)):
            # check the team name for the entry, then look up the members in that team, then get their datajoint user names
            team_name = (SpikeSortingParameters & (SpikeSortingParameters & entries[entry_idx]).proj()).fetch1()['team_name']
            lab_member_name_list = (LabTeam.LabTeamMember & {'team_name': team_name}).fetch('lab_member_name')
            datajoint_user_names = []
            for lab_member_name in lab_member_name_list:
                datajoint_user_names.append((LabMember.LabMemberInfo & {'lab_member_name': lab_member_name}).fetch1('datajoint_user_name'))
            permission_bool[entry_idx] = current_user_name in datajoint_user_names

        if np.sum(permission_bool)==len(entries):
            print('Permission to delete all specified entries granted.')
            # delete the sortings from the workspaces
            #for entry_idx in range(len(entries)):
                #print(entries[entry_idx])
                #TODO FIX:
                #key = (self & (self & entries[entry_idx]).proj())
                # workspace_uri = key['curation_feed_uri'] 
                #print(key)
                # # load the workspace and the sorting
                # workspace = sv.load_workspace(workspace_uri)
                # sorting_id = key['sorting_id']
                # workspace.delete_sorting(sorting_id)
            super().delete()
        else:
            raise Exception('You do not have permission to delete all specified entries. Not deleting anything.')
@schema 
class CuratedSpikeSortingParameters(dj.Manual):
    definition = """
    -> AutomaticCurationSpikeSorting
    ---
    -> SpikeSortingMetrics.proj(final_cluster_metrics_list_name='cluster_metrics_list_name')
    """

@schema
class CuratedSpikeSorting(dj.Computed):
    definition = """
    # Table for holding the output of fully curated spike sorting
    -> CuratedSpikeSortingParameters
    ---
    -> AnalysisNwbfile    # New analysis NWB file to hold unit info
    units_object_id: varchar(40)           # Object ID for the units in NWB file
    """

    class Unit(dj.Part):
        definition = """
        # Table for holding sorted units
        -> master
        unit_id: int            # ID for each unit
        ---
        label='' :              varchar(80)      # optional label for each unit
        noise_overlap=-1 :      float    # noise overlap metric for each unit
        nn_hit_rate=-1:         float  # isolation score metric for each unit
        isi_violation=-1:       float # ISI violation score for each unit
        firing_rate=-1:         float   # firing rate
        num_spikes=-1:          int          # total number of spikes
        """

    def make(self, key):
        # define the list of properties. TODO: get this from table definition.
        unit_properties = ['label', 'nn_hit_rate', 'noise_overlap',
                           'isi_violation', 'firing_rate', 'num_spikes']

        # Creating the curated units table involves 4 steps:
        # 1. Merging units labeled for merge
        # 2. Recalculate metrics
        # 3. Inserting accepted units into new analysis NWB file and into the Curated Units table.

        # 1. Merge
        # We can get the new curated soring from the workspace.
        workspace_uri = (SpikeSorting & key).fetch1('curation_feed_uri')
        workspace = sv.load_workspace(workspace_uri=workspace_uri)
        target_sorting_id = (AutomaticCurationSpikeSorting & key).fetch1('sorting_id')
        if not target_sorting_id in workspace.sorting_ids:
            Warning(f'AutomaticCurationSpikeSorting sorting_id {target_sorting_id} not found in workspace; skipping')
            return

        #sorting = workspace.get_curated_sorting_extractor(workspace.sorting_ids[0])
        # There should be two sortings, corresponding to the 
        sorting = workspace.get_curated_sorting_extractor(target_sorting_id)

        # Get labels
        labels = workspace.get_sorting_curation(target_sorting_id)

        # turn labels to list of str, only including accepted units.
        accepted_units = []
        unit_labels = labels['labelsByUnit']
        for idx, unitId in enumerate(unit_labels):
            if 'accept' in unit_labels[unitId]:
                accepted_units.append(unitId)            

        # remove non-primary merged units
        if labels['mergeGroups']:
            for m in labels['mergeGroups']:
                if set(m[1:]).issubset(accepted_units):
                    for cell in m[1:]:
                        accepted_units.remove(cell)

        # get the labels for the accepted units
        labels_concat = []
        for unitId in accepted_units:
            label_concat = ','.join(unit_labels[unitId])
            labels_concat.append(label_concat)

        print(f'Found {len(accepted_units)} accepted units')

        # exit out if there are no labels or no accepted units
        if len(unit_labels) == 0 or len(accepted_units) == 0:
            print(f'{key}: no curation found or no accepted units')
            return

        # 2. Recalucate metrics for curated units to account for merges
        # get the recording extractor
        with Timer(label=f'Whitening and recomputing metrics', verbose=True):
            recording = workspace.get_recording_extractor(
                workspace.recording_ids[0])
            tmpfile = tempfile.NamedTemporaryFile(dir='/stelmo/nwb/tmp')
            recording = se.CacheRecordingExtractor(
                                recording, save_path=tmpfile.name, chunk_mb=10000)
            # whiten the recording
            filter_params = (SpikeSorterParameters & {'sorter_name': key['sorter_name'],
                                                    'parameter_set_name': key['parameter_set_name']}).fetch1('filter_parameter_dict')
            recording = st.preprocessing.whiten(
                recording, seed=0, chunk_size=filter_params['filter_chunk_size'])
            cluster_metrics_list_name = (CuratedSpikeSortingParameters & key).fetch1('final_cluster_metrics_list_name')
            metrics = SpikeSortingMetrics().compute_metrics(cluster_metrics_list_name, recording, sorting)

        # Limit the metrics to accepted units
        metrics = metrics.loc[accepted_units]

        # 3. Save the accepted, merged units and their metrics
        # load the AnalysisNWBFile from the original sort to get the sort_interval_valid times and the sort_interval
        orig_units = (SpikeSorting & key).fetch_nwb()[
            0]['units'].to_dataframe()
        sort_interval = orig_units.iloc[1]['sort_interval']
        sort_interval_valid_times = orig_units.iloc[1]['obs_intervals']

        # add the units with the metrics and labels to the file.
        print('\nSaving curated sorting results...')
        timestamps = SpikeSorting.get_recording_timestamps(key)
        units = dict()
        units_valid_times = dict()
        units_sort_interval = dict()
        unit_ids = sorting.get_unit_ids()
        for unit_id in unit_ids:
            if unit_id in accepted_units:
                spike_times_in_samples = sorting.get_unit_spike_train(
                    unit_id=unit_id)
                units[unit_id] = timestamps[spike_times_in_samples]
                units_valid_times[unit_id] = sort_interval_valid_times
                units_sort_interval[unit_id] = [sort_interval]

        # Create a new analysis NWB file
        key['analysis_file_name'] = AnalysisNwbfile().create(key['nwb_file_name'])

        units_object_id, _ = AnalysisNwbfile().add_units(key['analysis_file_name'],
                                                         units, units_valid_times,
                                                         units_sort_interval,
                                                         metrics=metrics, labels=labels_concat)
        # add the analysis file to the table
        AnalysisNwbfile().add(key['nwb_file_name'], key['analysis_file_name'])
        key['units_object_id'] = units_object_id

        # Insert entry to CuratedSpikeSorting table
        self.insert1(key)

        # Remove the non primary key entries.
        del key['units_object_id']
        del key['analysis_file_name']

        units_table = (CuratedSpikeSorting & key).fetch_nwb()[0]['units'].to_dataframe()

        # Add entries to CuratedSpikeSorting.Units table
        print('\nAdding to dj Unit table...')
        unit_key = key
        for unit_num, unit in units_table.iterrows():
            unit_key['unit_id'] = unit_num
            for property in unit_properties:
                if property in unit:
                    unit_key[property] = unit[property]
            CuratedSpikeSorting.Unit.insert1(unit_key)

        print('Done with dj Unit table.')

    def delete(self):
        """
        Extends the delete method of base class to implement permission checking
        """
        current_user_name = dj.config['database.user']
        entries = self.fetch()
        permission_bool = np.zeros((len(entries),))
        print(f'Attempting to delete {len(entries)} entries, checking permission...')
    
        for entry_idx in range(len(entries)):
            # check the team name for the entry, then look up the members in that team, then get their datajoint user names
            team_name = (SpikeSortingParameters & (SpikeSortingParameters & entries[entry_idx]).proj()).fetch1()['team_name']
            lab_member_name_list = (LabTeam.LabTeamMember & {'team_name': team_name}).fetch('lab_member_name')
            datajoint_user_names = []
            for lab_member_name in lab_member_name_list:
                datajoint_user_names.append((LabMember.LabMemberInfo & {'lab_member_name': lab_member_name}).fetch1('datajoint_user_name'))
            permission_bool[entry_idx] = current_user_name in datajoint_user_names
        if np.sum(permission_bool)==len(entries):
            print('Permission to delete all specified entries granted.')
            super().delete()
        else:
            raise Exception('You do not have permission to delete all specified entries. Not deleting anything.')
        
    def fetch_nwb(self, *attrs, **kwargs):
        return fetch_nwb(self, (AnalysisNwbfile, 'analysis_file_abs_path'), *attrs, **kwargs)

    def delete_extractors(self, key):
        """Delete directories with sorting and recording extractors that are no longer needed

        :param key: key to curated sortings where the extractors can be removed
        :type key: dict
        """
        # get a list of the files in the spike sorting storage directory
        dir_names = next(os.walk(os.environ['SPIKE_SORTING_STORAGE_DIR']))[1]
        # now retrieve a list of the currently used analysis nwb files
        analysis_file_names = (self & key).fetch('analysis_file_name')
        delete_list = []
        for dir in dir_names:
            if not dir in analysis_file_names:
                delete_list.append(dir)
                print(f'Adding {dir} to delete list')
        delete = input('Delete all listed directories (y/n)? ')
        if delete == 'y' or delete == 'Y':
            for dir in delete_list:
                shutil.rmtree(dir)
            return
        print('No files deleted')
    # def delete(self, key)
@schema
class UnitInclusionParameters(dj.Manual):
    definition = """
    unit_inclusion_param_name: varchar(80) # the name of the list of thresholds for unit inclusion
    ---
    max_noise_overlap=1:        float   # noise overlap threshold (include below) 
    min_nn_hit_rate=-1:         float   # isolation score threshold (include above)
    max_isi_violation=100:      float   # ISI violation threshold
    min_firing_rate=0:          float   # minimum firing rate threshold
    max_firing_rate=100000:     float   # maximum fring rate thershold
    min_num_spikes=0:           int     # minimum total number of spikes
    exclude_label_list=NULL:    BLOB    # list of labels to EXCLUDE
    """
    
    def get_included_units(self, curated_sorting_key, unit_inclusion_key):
        """given a reference to a set of curated sorting units and a specific unit inclusion parameter list, returns 
        the units that should be included

        :param curated_sorting_key: key to entries in CuratedSpikeSorting.Unit table
        :type curated_sorting_key: dict
        :param unit_inclusion_key: key to a single unit inclusion parameter set
        :type unit_inclusion_key: dict
        """

        curated_sortings = (CuratedSpikeSorting() & curated_sorting_key).fetch()
        inclusion_key = (UnitInclusionParameters & unit_inclusion_key).fetch1()
        units = (CuratedSpikeSorting().Unit() & curated_sortings &
                                               f'noise_overlap <= {inclusion_key["max_noise_overlap"]}' &
                                               f'nn_hit_rate >= {inclusion_key["min_nn_hit_rate"]}' &
                                               f'isi_violation <= {inclusion_key["max_isi_violation"]}' &
                                               f'firing_rate >= {inclusion_key["min_firing_rate"]}' &
                                               f'firing_rate <= {inclusion_key["max_firing_rate"]}' &
                                               f'num_spikes >= {inclusion_key["min_num_spikes"]}').fetch()
        #now exclude by label if it is specified
        if inclusion_key['exclude_label_list'] is not None:
            included_units = []
            for unit in units:
                labels = unit['label'].split(',')
                exclude = False
                for label in labels:
                    if label in inclusion_key['exclude_label_list']:
                        exclude = True
                if not exclude:
                    included_units.append(unit)   
            return included_units
        else:
            return units
