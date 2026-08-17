"""
Raspberry Pi edge node.

Responsibilities:
  1. Poll one or more grid meters through `MeterInterface` at a fixed cadence.
  2. Package readings into a `MeasurementBatch`.
  3. Publish upstream to the SOGNO-style message bus (MQTT).
  4. If the network/broker is unreachable, buffer batches locally in SQLite
     so nothing is lost, and flush the backlog once connectivity returns —
     this is the "edge resilience" requirement for field-deployed Pi nodes
     on unreliable substation networks.

Run standalone:  python -m edge.edge_node --simulate
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from common.config import edge_settings
from common.messaging import Publisher
from common.schemas import MeasurementBatch
from edge.sensor_interface import MeterInterface, ModbusMeterInterface, SimulatedMeterInterface

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class LocalBuffer:
    """SQLite-backed durable queue for measurement batches taken while offline."""

    def __init__(self, path: str, max_rows: int):
        self._path = path
        self._max_rows = max_rows
        with closing(sqlite3.connect(self._path)) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )
            conn.commit()

    def push(self, payload: dict) -> None:
        with closing(sqlite3.connect(self._path)) as conn:
            conn.execute(
                "INSERT INTO buffer (payload, created_at) VALUES (?, ?)",
                (json.dumps(payload, default=str), time.time()),
            )
            conn.execute(
                """DELETE FROM buffer WHERE id IN (
                    SELECT id FROM buffer ORDER BY id ASC
                    LIMIT MAX(0, (SELECT COUNT(*) FROM buffer) - ?)
                )""",
                (self._max_rows,),
            )
            conn.commit()

    def pending(self, limit: int = 500) -> list[tuple[int, dict]]:
        with closing(sqlite3.connect(self._path)) as conn:
            rows = conn.execute(
                "SELECT id, payload FROM buffer ORDER BY id ASC LIMIT ?", (limit,)
            ).fetchall()
        return [(row_id, json.loads(payload)) for row_id, payload in rows]

    def remove(self, row_ids: list[int]) -> None:
        if not row_ids:
            return
        with closing(sqlite3.connect(self._path)) as conn:
            conn.executemany("DELETE FROM buffer WHERE id = ?", [(i,) for i in row_ids])
            conn.commit()

    def depth(self) -> int:
        with closing(sqlite3.connect(self._path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM buffer").fetchone()[0]


class EdgeNode:
    """Orchestrates acquisition -> buffering -> publishing for one Pi node."""

    def __init__(self, meter: MeterInterface, node_id: str, bus_id: str, element_id: str):
        self.meter = meter
        self.node_id = node_id
        self.bus_id = bus_id
        self.element_id = element_id
        self.publisher = Publisher(
            host=edge_settings.mqtt_broker_host,
            port=edge_settings.mqtt_broker_port,
            client_id=f"{node_id}-publisher",
            max_retries=edge_settings.max_retries,
            backoff_base_s=edge_settings.retry_backoff_base_s,
            connect_timeout_s=edge_settings.connect_timeout_s,
        )
        self.buffer = LocalBuffer(edge_settings.local_buffer_path, edge_settings.max_buffer_rows)
        self._running = False

    def acquire_once(self) -> MeasurementBatch:
        readings = self.meter.read_all(self.bus_id, self.element_id)
        for r in readings:
            r.node_id = self.node_id
        return MeasurementBatch(node_id=self.node_id, measurements=readings)

    def publish_or_buffer(self, batch: MeasurementBatch) -> None:
        payload = batch.model_dump()
        ok = self.publisher.publish(edge_settings.mqtt_topic, payload, qos=edge_settings.mqtt_qos)
        if not ok:
            logger.warning("Publish failed, buffering batch locally (depth=%d)", self.buffer.depth())
            self.buffer.push(payload)

    def flush_buffer(self) -> None:
        """Drain any locally buffered batches once the bus is reachable again."""
        pending = self.buffer.pending()
        if not pending:
            return
        flushed_ids = []
        for row_id, payload in pending:
            if self.publisher.publish(edge_settings.mqtt_topic, payload, qos=edge_settings.mqtt_qos):
                flushed_ids.append(row_id)
            else:
                break  # bus still down, stop trying and keep the rest queued
        self.buffer.remove(flushed_ids)
        if flushed_ids:
            logger.info("Flushed %d buffered batches upstream", len(flushed_ids))

    def run_forever(self, poll_interval_s: float | None = None) -> None:
        interval = poll_interval_s or edge_settings.poll_interval_s
        self.meter.connect()
        self.publisher.connect()
        self._running = True

        def _stop(*_args):
            self._running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        logger.info("Edge node %s started (poll every %.2fs)", self.node_id, interval)
        while self._running:
            cycle_start = time.monotonic()
            try:
                batch = self.acquire_once()
                self.publish_or_buffer(batch)
                self.flush_buffer()
            except Exception as exc:  # noqa: BLE001 - never let the node die
                logger.exception("Acquisition cycle failed: %s", exc)
            elapsed = time.monotonic() - cycle_start
            time.sleep(max(0.0, interval - elapsed))

        self.meter.close()
        self.publisher.close()
        logger.info("Edge node %s stopped", self.node_id)


def build_meter(simulate: bool) -> MeterInterface:
    if simulate:
        return SimulatedMeterInterface()
    return ModbusMeterInterface(
        host=edge_settings.modbus_host,
        port=edge_settings.modbus_port,
        unit_id=edge_settings.modbus_unit_id,
        timeout_s=edge_settings.connect_timeout_s,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Raspberry Pi grid-edge node")
    parser.add_argument("--simulate", action="store_true", help="use a synthetic meter instead of real Modbus hardware")
    parser.add_argument("--bus-id", default="BUS_1")
    parser.add_argument("--element-id", default="METER_1")
    parser.add_argument("--once", action="store_true", help="acquire a single batch and exit (for testing)")
    args = parser.parse_args()

    Path(edge_settings.local_buffer_path).parent.mkdir(parents=True, exist_ok=True)
    node = EdgeNode(build_meter(args.simulate), edge_settings.node_id, args.bus_id, args.element_id)

    if args.once:
        node.meter.connect()
        batch = node.acquire_once()
        print(batch.model_dump_json(indent=2))
        node.meter.close()
        return

    node.run_forever()


if __name__ == "__main__":
    main()
