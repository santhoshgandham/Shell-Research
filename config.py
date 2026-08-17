"""
Centralized configuration for every service in the stack.

All tunables come from environment variables (12-factor style) so the same
container image can run on a Raspberry Pi edge node, a dev laptop, or a
Kubernetes pod without code changes.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class EdgeSettings(BaseSettings):
    """Configuration for a single Raspberry Pi edge node."""

    model_config = SettingsConfigDict(env_prefix="EDGE_")

    node_id: str = "edge-rpi-01"
    poll_interval_s: float = 1.0          # measurement acquisition cadence
    modbus_host: str = "127.0.0.1"
    modbus_port: int = 502
    modbus_unit_id: int = 1
    mqtt_broker_host: str = "mqtt-broker"
    mqtt_broker_port: int = 1883
    mqtt_topic: str = "grid/measurements"
    mqtt_qos: int = 1
    local_buffer_path: str = "edge_buffer.sqlite"   # offline resilience
    max_buffer_rows: int = 100_000
    connect_timeout_s: float = 3.0
    max_retries: int = 5
    retry_backoff_base_s: float = 0.5


class GridStateSettings(BaseSettings):
    """Configuration for the NTP + State Estimation microservice."""

    model_config = SettingsConfigDict(env_prefix="GRIDSTATE_")

    mqtt_broker_host: str = "mqtt-broker"
    mqtt_broker_port: int = 1883
    mqtt_topic: str = "grid/measurements"
    se_max_iterations: int = 25
    se_tolerance: float = 1e-5
    se_bad_data_threshold: float = 3.0     # normalized residual threshold
    base_mva: float = 100.0


class ForecastSettings(BaseSettings):
    """Configuration for the probabilistic load-forecasting microservice."""

    model_config = SettingsConfigDict(env_prefix="FORECAST_")

    lookback_steps: int = 24               # hours of history fed to encoder
    horizon_steps: int = 6                 # hours ahead to forecast (seq2seq)
    mc_dropout_samples: int = 100
    confidence_level: float = 0.95
    optuna_n_trials: int = 30
    optuna_epochs_per_trial: int = 10
    final_train_epochs: int = 20
    model_artifact_path: str = "artifacts/best_seq2seq_model.keras"
    study_artifact_path: str = "artifacts/optuna_study.pkl"
    random_seed: int = 42


edge_settings = EdgeSettings()
grid_state_settings = GridStateSettings()
forecast_settings = ForecastSettings()
