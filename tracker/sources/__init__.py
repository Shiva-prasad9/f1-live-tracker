"""Data sources. Every source yields normalised messages:

    (t_epoch_seconds, topic, data, is_initial_state)

so the parser and estimator never know where the data came from.
"""
from typing import Any

Message = tuple[float, str, Any, bool]
