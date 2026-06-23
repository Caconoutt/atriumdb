"""Config-driven AtriumDB SDK connection.

The SDK is only touched at init / resync. Mode and params come from the environment so
no connection detail (and certainly no measure_id) is ever hardcoded.

Environment variables:
    ATRIUMDB_MODE              "local" | "api"
    # local mode
    ATRIUMDB_DATASET_LOCATION  path to the dataset
    ATRIUMDB_METADATA_TYPE     "sqlite" | "mariadb"
    ATRIUMDB_CONNECTION_PARAMS JSON dict of connection params (mariadb)
    # api mode
    ATRIUMDB_API_URL           remote API url
    ATRIUMDB_TOKEN             bearer token
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class AtriumConfig:
    mode: str = "local"
    dataset_location: str | None = None
    metadata_type: str | None = None
    connection_params: dict | None = None
    api_url: str | None = None
    token: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AtriumConfig":
        env = os.environ if env is None else env
        raw_params = env.get("ATRIUMDB_CONNECTION_PARAMS")
        return cls(
            mode=env.get("ATRIUMDB_MODE", "local").strip().lower(),
            dataset_location=env.get("ATRIUMDB_DATASET_LOCATION"),
            metadata_type=env.get("ATRIUMDB_METADATA_TYPE"),
            connection_params=json.loads(raw_params) if raw_params else None,
            api_url=env.get("ATRIUMDB_API_URL"),
            token=env.get("ATRIUMDB_TOKEN"),
        )


def connect_sdk(config: AtriumConfig):
    """Open one AtriumSDK handle per process from config. Import is local so the rest of
    the module (and the request-time path) does not require atriumdb to be installed."""
    from atriumdb import AtriumSDK

    if config.mode == "api":
        if not config.api_url:
            raise ValueError("ATRIUMDB_API_URL is required in api mode")
        return AtriumSDK(api_url=config.api_url, token=config.token)

    if config.mode == "local":
        if not config.dataset_location:
            raise ValueError("ATRIUMDB_DATASET_LOCATION is required in local mode")
        return AtriumSDK(
            dataset_location=config.dataset_location,
            metadata_connection_type=config.metadata_type,
            connection_params=config.connection_params,
        )

    raise ValueError(f"Unknown ATRIUMDB_MODE: {config.mode!r}")
