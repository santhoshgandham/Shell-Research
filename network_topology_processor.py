"""
Network Topology Processor (NTP).

Real-time grid topology changes constantly as breakers/switches open and
close. The NTP's job is to turn the *static* bus-section/switch model plus
*live* breaker-status telemetry into the *dynamic* bus-branch model that
State Estimation actually runs on: which bus-sections are electrically
merged, which are isolated, and which energized "islands" exist right now.

Approach: model the substation as a graph where bus-sections are nodes and
switching devices are edges. An edge is "closed" (conducting) or "open"
(non-conducting) based on the latest breaker-status measurement. Zero-
impedance closed edges get contracted (merged into a single electrical bus);
open edges are dropped. Connected components of the resulting graph are the
energized islands.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

import networkx as nx

from common.schemas import MeasurementType, RawMeasurement, TopologyEdge, TopologySnapshot

logger = logging.getLogger(__name__)


@dataclass
class SwitchingDevice:
    """Static model of a breaker/switch connecting two bus-sections."""

    element_id: str
    from_bus: str
    to_bus: str
    impedance_pu: complex = 0j     # 0 for an ideal breaker/switch


@dataclass
class NetworkModel:
    """Static (rarely-changing) description of the substation/feeder wiring."""

    buses: list[str]
    switches: list[SwitchingDevice]
    branches: list[SwitchingDevice] = field(default_factory=list)  # non-switchable lines/xfmrs


class NetworkTopologyProcessor:
    """Combines the static NetworkModel with live breaker-status telemetry."""

    def __init__(self, model: NetworkModel):
        self.model = model
        self._breaker_status: dict[str, bool] = {sw.element_id: True for sw in model.switches}

    def ingest_measurement(self, m: RawMeasurement) -> None:
        """Update live breaker state from a telemetry point."""
        if m.measurement_type != MeasurementType.BREAKER_STATUS:
            return
        self._breaker_status[m.element_id] = bool(round(m.value))

    def ingest_batch(self, measurements: list[RawMeasurement]) -> None:
        for m in measurements:
            self.ingest_measurement(m)

    def build_graph(self) -> nx.Graph:
        """Raw section graph: every switch/branch present, closed or not."""
        g = nx.Graph()
        g.add_nodes_from(self.model.buses)
        for sw in self.model.switches:
            closed = self._breaker_status.get(sw.element_id, True)
            g.add_edge(sw.from_bus, sw.to_bus, element_id=sw.element_id,
                       energized=closed, impedance_pu=sw.impedance_pu, kind="switch")
        for br in self.model.branches:
            g.add_edge(br.from_bus, br.to_bus, element_id=br.element_id,
                       energized=True, impedance_pu=br.impedance_pu, kind="branch")
        return g

    def process(self, reference_bus: str | None = None) -> TopologySnapshot:
        """Produce the current TopologySnapshot: energized edges + islands.

        Bus-section merging: closed, zero-impedance switches are contracted
        so downstream State Estimation sees one electrical bus rather than
        two bus-sections joined by an ideal breaker.
        """
        g = self.build_graph()
        merged = nx.Graph()
        merged.add_nodes_from(g.nodes)

        union_find = {n: n for n in g.nodes}

        def find(n):
            while union_find[n] != n:
                union_find[n] = union_find[union_find[n]]
                n = union_find[n]
            return n

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                union_find[ra] = rb

        # First pass: contract closed, (near-)zero-impedance switches
        for u, v, data in g.edges(data=True):
            if data["energized"] and abs(data["impedance_pu"]) < 1e-9:
                union(u, v)

        # Second pass: build the merged electrical-bus graph
        for u, v, data in g.edges(data=True):
            if not data["energized"]:
                continue
            ru, rv = find(u), find(v)
            if ru == rv:
                continue  # already the same electrical bus
            merged.add_edge(ru, rv, **data)

        islands = [sorted(c) for c in nx.connected_components(merged) if len(c) >= 1]
        islands.sort(key=lambda c: (-len(c), c[0]))

        edges = [
            TopologyEdge(
                from_bus=u, to_bus=v,
                element_id=data["element_id"],
                energized=data["energized"],
                impedance_pu=data["impedance_pu"],
            )
            for u, v, data in merged.edges(data=True)
        ]

        resolved_ref = None
        if reference_bus is not None:
            resolved_ref = sorted({find(n) for n in g.nodes}, key=len)[0] if reference_bus not in merged else reference_bus
            for island in islands:
                if reference_bus in island or find(reference_bus) in island:
                    resolved_ref = island[0]
                    break

        return TopologySnapshot(
            buses=sorted(merged.nodes),
            edges=edges,
            islands=islands,
            reference_bus=resolved_ref,
        )

    def electrical_bus_map(self) -> dict[str, str]:
        """Map every raw bus-section id -> the merged electrical bus id it belongs to."""
        g = self.build_graph()
        union_find = {n: n for n in g.nodes}

        def find(n):
            while union_find[n] != n:
                union_find[n] = union_find[union_find[n]]
                n = union_find[n]
            return n

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                union_find[ra] = rb

        for u, v, data in g.edges(data=True):
            if data["energized"] and abs(data["impedance_pu"]) < 1e-9:
                union(u, v)
        return {n: find(n) for n in g.nodes}


def _demo_model() -> NetworkModel:
    """A small illustrative feeder: 5 bus-sections, one normally-open tie switch."""
    return NetworkModel(
        buses=["BS1", "BS2", "BS3", "BS4", "BS5"],
        switches=[
            SwitchingDevice("SW_12", "BS1", "BS2", impedance_pu=0j),
            SwitchingDevice("SW_23", "BS2", "BS3", impedance_pu=0j),
            SwitchingDevice("SW_TIE_45", "BS4", "BS5", impedance_pu=0j),  # normally open
        ],
        branches=[
            SwitchingDevice("LINE_34", "BS3", "BS4", impedance_pu=0.01 + 0.05j),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Network Topology Processor demo")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if not args.demo:
        parser.print_help()
        return

    logging.basicConfig(level=logging.INFO)
    ntp = NetworkTopologyProcessor(_demo_model())

    print("--- All switches closed except normally-open tie SW_TIE_45 ---")
    ntp.ingest_measurement(RawMeasurement(
        node_id="edge-demo", bus_id="BS4", element_id="SW_TIE_45",
        measurement_type=MeasurementType.BREAKER_STATUS, value=0, unit="bool",
    ))
    snapshot = ntp.process(reference_bus="BS1")
    print(snapshot.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
