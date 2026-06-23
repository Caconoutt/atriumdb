"""AtriumDB measure-resolution layer for the dashboard backend.

Public surface:
    load_seed_rows           -- read the committed seed file
    initialize_measure_mapping -- build the id lookup at init (calls the SDK)
    resync_atriumdb_ids      -- refresh cached ids after an AtriumDB reload
    resolve_for_retrieval    -- request-time: dashboard reference -> AtriumDB id(s)
    resolve_measure_id       -- low-level triple -> id helper
"""
from .config import AtriumConfig, connect_sdk
from .db import DashboardDB, MeasureRecord
from .models import InitReport, ResolvedMeasure, SeedRow
from .resolver import (
    DEFAULT_SEED,
    initialize_measure_mapping,
    load_seed_rows,
    resolve_for_retrieval,
    resolve_measure_id,
    resync_atriumdb_ids,
)

__all__ = [
    "AtriumConfig", "connect_sdk", "DashboardDB", "MeasureRecord",
    "InitReport", "ResolvedMeasure", "SeedRow", "DEFAULT_SEED",
    "initialize_measure_mapping", "load_seed_rows", "resolve_for_retrieval",
    "resolve_measure_id", "resync_atriumdb_ids",
]
