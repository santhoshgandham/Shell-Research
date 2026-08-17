"""
Shared message contracts between the edge nodes, the grid-state microservices
(NTP / SE), and the forecasting microservice. Using pydantic models gives us
validation + JSON (de)serialization for free at every hop of the pipeline,
which matters when the same payload crosses a Raspberry Pi -> MQTT ->
SOGNO-style microservice boundary.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MeasurementType(str, Enum):
    VOLTAGE_MAG = "voltage_magnitude"      # per-unit or kV
    VOLTAGE_ANGLE = "voltage_angle"        # degrees
    ACTIVE_POWER = "active_power"          # MW
    REACTIVE_POWER = "reactive_power"      # MVAr
    CURRENT_MAG = "current_magnitude"      # A
    BREAKER_STATUS = "breaker_status"      # 0 = open, 1 = closed


class RawMeasurement(BaseModel):
    """A single scalar reading acquired by an edge node from grid hardware."""

    node_id: str
    bus_id: str
    element_id: str                # meter / breaker / transformer tag
    measurement_type: MeasurementType
    value: float
    unit: str
    timestamp: float = Field(default_factory=time.time)
    quality: float = Field(default=1.0, ge=0.0, le=1.0)  # sensor confidence
    std_dev: float = Field(default=0.01, gt=0.0)          # for WLS weighting

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf check
            raise ValueError("measurement value must be finite")
        return v


class MeasurementBatch(BaseModel):
    """What an edge node actually publishes on each poll cycle."""

    node_id: str
    batch_timestamp: float = Field(default_factory=time.time)
    measurements: list[RawMeasurement]


class TopologyEdge(BaseModel):
    from_bus: str
    to_bus: str
    element_id: str
    energized: bool
    impedance_pu: complex = 0j

    model_config = {"arbitrary_types_allowed": True}


class TopologySnapshot(BaseModel):
    """Output of the Network Topology Processor."""

    timestamp: float = Field(default_factory=time.time)
    buses: list[str]
    edges: list[TopologyEdge]
    islands: list[list[str]]       # connected components (energized islands)
    reference_bus: Optional[str] = None


class BusStateEstimate(BaseModel):
    bus_id: str
    voltage_pu: float
    angle_rad: float
    voltage_std: float
    angle_std: float


class StateEstimationResult(BaseModel):
    timestamp: float = Field(default_factory=time.time)
    converged: bool
    iterations: int
    objective_value: float
    bus_states: list[BusStateEstimate]
    bad_data_flags: list[str]      # measurement ids flagged as bad data


class ForecastPoint(BaseModel):
    step_ahead: int
    timestamp: float
    mean: float
    lower_95: float
    upper_95: float


class ForecastResponse(BaseModel):
    generated_at: float = Field(default_factory=time.time)
    horizon_steps: int
    points: list[ForecastPoint]
    model_version: str
    mae_backtest: Optional[float] = None
