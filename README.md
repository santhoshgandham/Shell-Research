# Shell Grid Research — Edge-to-Cloud Smart Grid Automation

Reference implementation for the internship/research work described in the
original repo: Raspberry-Pi edge acquisition, Network Topology Processing (NTP),
State Estimation (SE) against a SOGNO-style microservice bus, and a
microservice-based probabilistic LSTM seq2seq load-forecasting pipeline
(Optuna-tuned, SHAP-explainable).

This rewrites the original single-notebook (`ugrc.py`) export into a modular,
testable, deployable codebase.

```
shell-grid-research/
├── common/                  # shared config, schemas, messaging utils
│   ├── config.py
│   ├── schemas.py
│   └── messaging.py
├── edge/                    # Raspberry Pi edge node ↔ grid hardware
│   ├── sensor_interface.py  # Modbus/DNP3-style HW abstraction layer
│   └── edge_node.py         # polling loop, local buffering, MQTT publish
├── grid_state/               # real-time grid-state computation
│   ├── network_topology_processor.py
│   └── state_estimation.py
├── forecasting/               # probabilistic load forecasting
│   ├── preprocessing.py
│   ├── seq2seq_model.py
│   ├── train_optuna.py
│   └── explainability.py
├── services/                  # microservice entrypoints
│   ├── forecast_service.py   # FastAPI: /forecast /explain /health
│   └── grid_state_service.py # FastAPI: /topology /state
├── docker/
│   └── docker-compose.yml
├── k8s/
│   ├── forecast-deployment.yaml
│   ├── grid-state-deployment.yaml
│   └── mqtt-broker-deployment.yaml
└── tests/
    ├── test_network_topology_processor.py
    ├── test_state_estimation.py
    └── test_forecasting_pipeline.py
```

## How the pieces map to the project

| Bullet | Module(s) |
|---|---|
| Raspberry Pi edge nodes + grid hardware for measurement acquisition | `edge/sensor_interface.py`, `edge/edge_node.py` |
| Network Topology Processor & State Estimation for real-time grid state | `grid_state/network_topology_processor.py`, `grid_state/state_estimation.py` |
| Hardware–software interfaces (devices ↔ edge nodes ↔ SOGNO services) | `common/messaging.py`, `services/grid_state_service.py` |
| Microservice LSTM seq2seq + Optuna, +5.7% MAE | `forecasting/*`, `services/forecast_service.py` |

## Data-flow

```
Grid meters (V, I, P, Q, breaker status)
        │ Modbus/DNP3
        ▼
sensor_interface.py  ──►  edge_node.py (Raspberry Pi)
        │ buffers locally (SQLite) if offline
        ▼ MQTT / Kafka topic: grid/measurements
common/messaging.py  ──►  SOGNO-style microservices
        │
        ├──► network_topology_processor.py ──► topology (buses, islands)
        │            │
        │            ▼
        └──► state_estimation.py (WLS) ──► bus voltages/angles, bad-data flags
                     │
                     ▼
           services/grid_state_service.py (FastAPI, /state)

Historical load + weather + calendar features
        ▼
forecasting/preprocessing.py ──► forecasting/seq2seq_model.py
        │                                │
        ▼                                ▼
forecasting/train_optuna.py     forecasting/explainability.py
        │ (HPO, +5.7% MAE)              │ (SHAP, MC-Dropout 95% CI)
        ▼                                ▼
             services/forecast_service.py (FastAPI, /forecast /explain)
```

## Quickstart

```bash
pip install -r requirements.txt

# 1. Simulate an edge node reading synthetic meters and publishing to MQTT
python -m edge.edge_node --simulate

# 2. Run topology + state estimation once against buffered measurements
python -m grid_state.network_topology_processor --demo
python -m grid_state.state_estimation --demo

# 3. Train the forecasting model with Optuna HPO (30 trials)
python -m forecasting.train_optuna --data data/synthetic_power_data.csv --trials 30

# 4. Serve forecasts + SHAP explanations as a microservice
uvicorn services.forecast_service:app --reload --port 8001

# 5. Serve grid state as a microservice
uvicorn services.grid_state_service:app --reload --port 8002
```

## Kubernetes

`docker/docker-compose.yml` is for local dev; `k8s/*.yaml` are minimal
Deployment + Service manifests for the same three components (MQTT broker,
grid-state service, forecast service), matching the "prototyping deployment
on Kubernetes" scope of the original internship work.
