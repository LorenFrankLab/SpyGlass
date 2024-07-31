import numpy as np
import xarray as xr

from spyglass.utils import logger

try:
    from replay_trajectory_classification.classifier import (
        _DEFAULT_CLUSTERLESS_MODEL_KWARGS,
        _DEFAULT_CONTINUOUS_TRANSITIONS,
        _DEFAULT_ENVIRONMENT,
        _DEFAULT_SORTED_SPIKES_MODEL_KWARGS,
    )
    from replay_trajectory_classification.discrete_state_transitions import (
        DiagonalDiscrete,
    )
    from replay_trajectory_classification.initial_conditions import (
        UniformInitialConditions,
    )
except (ImportError, ModuleNotFoundError) as e:
    (
        _DEFAULT_CLUSTERLESS_MODEL_KWARGS,
        _DEFAULT_CONTINUOUS_TRANSITIONS,
        _DEFAULT_ENVIRONMENT,
        _DEFAULT_SORTED_SPIKES_MODEL_KWARGS,
        DiagonalDiscrete,
        UniformInitialConditions,
    ) = [None] * 6
    logger.warning(e)


def discretize_and_trim(series: xr.DataArray, ndims=2) -> xr.DataArray:
    """Discretizes a continuous series and trims the zeros.

    Parameters
    ----------
    series : xr.DataArray, shape (n_time, n_position_bins)
        Continuous series to be discretized
    ndims : int, optional
        Number of dimensions of the series. Default is 2 for 1D series (time,
        position), 3 for 2D series (time, y_position, x_position)

    Returns
    -------
    discretized : xr.DataArray, shape (n_time, n_position_bins)
        Discretized and trimmed series
    """
    index = (
        ["time", "position"]
        if ndims == 2
        else ["time", "y_position", "x_position"]
    )
    discretized = np.multiply(series, 255).astype(np.uint8)  # type: ignore
    stacked = discretized.stack(unified_index=index)
    return stacked.where(stacked > 0, drop=True).astype(np.uint8)


def make_default_decoding_params(clusterless=False, use_gpu=False):
    """Default parameters for decoding

    Returns
    -------
    classifier_parameters : dict
    fit_parameters : dict
    predict_parameters : dict
    """

    classifier_params = dict(
        environments=[_DEFAULT_ENVIRONMENT],
        observation_models=None,
        continuous_transition_types=_DEFAULT_CONTINUOUS_TRANSITIONS,
        discrete_transition_type=DiagonalDiscrete(0.98),
        initial_conditions_type=UniformInitialConditions(),
        infer_track_interior=True,
    )

    if clusterless:
        clusterless_algorithm = (
            "multiunit_likelihood_integer_gpu"
            if use_gpu
            else "multiunit_likelihood_integer"
        )
        clusterless_algorithm_params = (
            {
                "mark_std": 24.0,
                "position_std": 6.0,
            }
            if use_gpu
            else _DEFAULT_CLUSTERLESS_MODEL_KWARGS
        )
        classifier_params.update(
            dict(
                clusterless_algorithm=clusterless_algorithm,
                clusterless_algorithm_params=clusterless_algorithm_params,
            )
        )
    else:
        extra_params = (
            dict(
                sorted_spikes_algorithm="spiking_likelihood_kde",
                sorted_spikes_algorithm_params=_DEFAULT_SORTED_SPIKES_MODEL_KWARGS,
            )
            if use_gpu
            else dict(knot_spacing=10, spike_model_penalty=1e1)
        )
        classifier_params.update(extra_params)

    predict_params = {
        "is_compute_acausal": True,
        "use_gpu": use_gpu,
        "state_names": ["Continuous", "Fragmented"],
    }
    fit_params = dict()

    return dict(
        classifier_params_name=(
            "default_decoding_gpu" if use_gpu else "default_decoding_cpu"
        ),
        classifier_params=classifier_params,
        fit_params=fit_params,
        predict_params=predict_params,
    )
