from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from backend.app.core.config import DB_PATH
from backend.app.storage.live_map import list_source_health
from backend.app.storage.sqlite import connect, initialize_database

SOURCE_CATALOG = [
    {
        "source_id": "fred",
        "name": "FRED",
        "type": "macro_market",
        "status": "active",
        "datasets": ["macro_observations"],
        "purpose": "Indices, petroleo, dolar amplio y tasas diarias sin claves privadas.",
    },
    {
        "source_id": "binance",
        "name": "Binance",
        "type": "market",
        "status": "active",
        "datasets": ["market_snapshots", "market_candles", "market_aggregate_trades", "order_book_snapshots", "order_book_levels", "order_book_deltas", "microstructure_collector_runs"],
        "purpose": "Precios, candles, aggTrades y snapshots publicos de profundidad crypto.",
    },
    {
        "source_id": "ccxt_public",
        "name": "CCXT Public Adapters",
        "type": "exchange_market",
        "status": "active",
        "datasets": ["exchange_source_health"],
        "purpose": "Mercados, ticker, order book y OHLCV publicos normalizados sin credenciales.",
    },
    {
        "source_id": "alternative_me",
        "name": "Alternative.me",
        "type": "sentiment",
        "status": "active",
        "datasets": ["market_snapshots"],
        "purpose": "Fear & Greed Index para contexto de sentimiento.",
    },
    {
        "source_id": "usgs",
        "name": "USGS",
        "type": "world_event",
        "status": "active",
        "datasets": ["live_events", "live_source_health"],
        "purpose": "Terremotos en GeoJSON normalizados como eventos.",
    },
    {
        "source_id": "gdacs",
        "name": "GDACS",
        "type": "world_event",
        "status": "active",
        "datasets": ["live_events", "live_source_health"],
        "purpose": "Alertas globales de desastre.",
    },
    {
        "source_id": "gdelt",
        "name": "GDELT",
        "type": "news",
        "status": "degraded_possible",
        "datasets": ["live_events", "live_source_health"],
        "purpose": "Noticias geolocalizadas y eventos de mercado. Puede rate-limitear.",
    },
    {
        "source_id": "sqlite",
        "name": "SQLite Local",
        "type": "storage",
        "status": "active",
        "datasets": [
            "all_local_tables",
            "bots",
            "bot_versions",
            "backtest_runs",
            "backtest_trades",
            "backtest_equity",
            "risk_limits",
            "risk_state",
            "risk_policies",
            "risk_policy_versions",
            "risk_audit_log",
            "risk_validation_log",
            "simulated_accounts",
            "simulated_orders",
            "simulated_positions",
            "simulated_fills",
            "simulated_ledger",
            "paper_bot_runtime_state",
            "paper_bot_runtime_events",
            "paper_bot_circuit_breakers",
            "paper_bot_circuit_events",
            "paper_bot_equity_snapshots",
            "paper_bot_sessions",
            "paper_bot_session_events",
            "exchange_source_health",
            "execution_intents",
            "liquidity_sweep_evaluations",
            "market_aggregate_trades",
            "order_book_snapshots",
            "order_book_levels",
            "order_book_deltas",
            "microstructure_collector_runs",
            "chart_indicator_presets",
            "chart_indicator_preset_versions",
            "spot_portfolios",
            "spot_holdings",
            "spot_transactions",
            "spot_cash_flows",
            "spot_portfolio_ledger",
            "spot_equity_snapshots",
            "spot_dca_plans",
            "spot_dca_executions",
            "spot_allocation_policies",
            "spot_allocation_policy_versions",
            "spot_rebalance_runs",
        ],
        "purpose": "Base local para cache, auditoria y datasets analiticos.",
    },
]

DATASET_CATALOG = [
    {
        "dataset_id": "macro_observations",
        "table": "macro_observations",
        "label": "Macro Observations",
        "category": "macro_market",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Series diarias macro normalizadas desde FRED para indices, commodities, FX y tasas.",
    },
    {
        "dataset_id": "market_snapshots",
        "table": "market_snapshots",
        "label": "Market Snapshots",
        "category": "market",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Snapshots de precio, cambio 24h, volumen, Fear & Greed y lectura ABRAXAS.",
    },
    {
        "dataset_id": "live_events",
        "table": "live_events",
        "label": "Live World Events",
        "category": "world_intelligence",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Eventos normalizados con coordenadas, severidad, fuente y activos relacionados.",
    },
    {
        "dataset_id": "live_source_health",
        "table": "live_source_health",
        "label": "Live Source Health",
        "category": "data_health",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Estado, latencia, errores y frescura de fuentes del mapa vivo.",
    },
    {
        "dataset_id": "market_candles",
        "table": "market_candles",
        "label": "Market Candles",
        "category": "market",
        "powerbi_ready": True,
        "bot_ready": False,
        "status": "planned",
        "description": "Candles persistidas por symbol/timeframe para estadistica y bots.",
    },
    {
        "dataset_id": "asset_features",
        "table": "asset_features",
        "label": "Asset Features",
        "category": "bot_feature_store",
        "powerbi_ready": True,
        "bot_ready": True,
        "status": "planned",
        "description": "Features numericas listas para bots: retornos, volatilidad, z-score, drawdown, tendencia, volumen y riesgo.",
    },
    {
        "dataset_id": "statistics_runs",
        "table": "statistics_runs",
        "label": "Statistics Runs",
        "category": "analytics",
        "powerbi_ready": True,
        "bot_ready": False,
        "status": "planned",
        "description": "Resultados auditables de estadistica, distribuciones y Monte Carlo.",
    },
    {
        "dataset_id": "regime_snapshots",
        "table": "regime_snapshots",
        "label": "Regime Snapshots",
        "category": "regime_engine",
        "powerbi_ready": True,
        "bot_ready": True,
        "status": "planned",
        "description": "Clasificaciones auditables de regimen, riesgo, sesgo, tendencia y volatilidad.",
    },
    {
        "dataset_id": "bots",
        "table": "bots",
        "label": "Saved Bots",
        "category": "bot_forge",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Bots creados en Bot Forge con estado, activo, timeframe y perfil de riesgo.",
    },
    {
        "dataset_id": "bot_versions",
        "table": "bot_versions",
        "label": "Bot Versions",
        "category": "bot_forge",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Versiones auditables con estrategia normalizada, contrato runtime, fingerprint y estado de validacion.",
    },
    {
        "dataset_id": "backtest_runs",
        "table": "backtest_runs",
        "label": "Backtest Runs",
        "category": "bot_forge",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Resultados persistidos de simulaciones: ROI, drawdown, trades, win rate y profit factor.",
    },
    {
        "dataset_id": "strategy_signal_evaluations",
        "table": "strategy_signal_evaluations",
        "label": "Strategy Signal Evaluations",
        "category": "bot_forge",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Evaluaciones auditables regla por regla sobre features reales; no crean ordenes ni intents.",
    },
    {
        "dataset_id": "liquidity_sweep_evaluations",
        "table": "liquidity_sweep_evaluations",
        "label": "Liquidity Sweep Evaluations",
        "category": "bot_forge",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Observaciones auditables de barridos por vela cerrada, agotamiento y snapshot L2; ejecucion bloqueada.",
    },
    {
        "dataset_id": "market_aggregate_trades",
        "table": "market_aggregate_trades",
        "label": "Market Aggregate Trades",
        "category": "market_microstructure",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "AggTrades publicos de Binance con lado agresor inferido desde buyer-is-maker.",
    },
    {
        "dataset_id": "order_book_snapshots",
        "table": "order_book_snapshots",
        "label": "Order Book Snapshots",
        "category": "market_microstructure",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Cabeceras de snapshots REST y libros reconstruidos desde deltas Binance secuenciados.",
    },
    {
        "dataset_id": "order_book_levels",
        "table": "order_book_levels",
        "label": "Order Book Levels",
        "category": "market_microstructure",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Niveles bid/ask normalizados por snapshot para estudiar muros y profundidad acumulada.",
    },
    {
        "dataset_id": "order_book_deltas",
        "table": "order_book_deltas",
        "label": "Order Book Deltas",
        "category": "market_microstructure",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Eventos L2 publicos de Binance validados por update ID para reconstruccion y replay local acotado.",
    },
    {
        "dataset_id": "microstructure_collector_runs",
        "table": "microstructure_collector_runs",
        "label": "Microstructure Collector Runs",
        "category": "data_health",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Ciclos auditables del colector con reconexiones, gaps, errores y conteos persistidos.",
    },
    {
        "dataset_id": "chart_indicator_presets",
        "table": "chart_indicator_presets",
        "label": "Chart Indicator Presets",
        "category": "chart_workspace",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Workspaces de indicadores configurables por activo y timeframe; cada cambio crea una version auditable.",
    },
    {
        "dataset_id": "chart_indicator_preset_versions",
        "table": "chart_indicator_preset_versions",
        "label": "Chart Indicator Preset Versions",
        "category": "chart_workspace",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Configuraciones JSON inmutables y hasheadas para reproducir cada version del workspace.",
    },
    {
        "dataset_id": "paper_order_proposals",
        "table": "paper_order_proposals",
        "label": "Paper Order Proposals",
        "category": "paper_trading",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Propuestas pendientes derivadas de señales; no son ordenes ni intenciones de ejecucion.",
    },
    {
        "dataset_id": "backtest_trades",
        "table": "backtest_trades",
        "label": "Backtest Trades",
        "category": "bot_forge",
        "powerbi_ready": True,
        "bot_ready": True,
        "preview_columns": [
            "backtest_id",
            "bot_version_id",
            "trade_index",
            "entry_timestamp",
            "exit_timestamp",
            "pnl",
            "return_pct",
            "exit_reason",
        ],
        "description": "Operaciones normalizadas por run con fills, costos, PnL neto y motivo de salida.",
    },
    {
        "dataset_id": "backtest_equity",
        "table": "backtest_equity",
        "label": "Backtest Equity",
        "category": "bot_forge",
        "powerbi_ready": True,
        "bot_ready": True,
        "preview_columns": [
            "backtest_id",
            "bot_version_id",
            "point_index",
            "timestamp",
            "equity",
            "benchmark_equity",
            "drawdown_pct",
            "in_position",
        ],
        "description": "Curva de equity normalizada con benchmark buy & hold y drawdown por punto.",
    },
    {
        "dataset_id": "risk_limits",
        "table": "risk_limits",
        "label": "Risk Limits",
        "category": "risk_engine",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Limites duros persistidos para validar futuras ordenes paper y live.",
    },
    {
        "dataset_id": "risk_state",
        "table": "risk_state",
        "label": "Risk State",
        "category": "risk_engine",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Estado backend del kill switch y motivo del ultimo cambio.",
    },
    {
        "dataset_id": "risk_policies",
        "table": "risk_policies",
        "label": "Risk Policies",
        "category": "risk_engine",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Registro de politicas activas o archivadas por cuenta y bot.",
    },
    {
        "dataset_id": "risk_policy_versions",
        "table": "risk_policy_versions",
        "label": "Risk Policy Versions",
        "category": "risk_engine",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Versiones inmutables de limites por cuenta y bot para reconstruir cada decision.",
    },
    {
        "dataset_id": "risk_audit_log",
        "table": "risk_audit_log",
        "label": "Risk Audit Log",
        "category": "risk_engine",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Historial auditable de cambios de limites y kill switch.",
    },
    {
        "dataset_id": "risk_validation_log",
        "table": "risk_validation_log",
        "label": "Risk Validations",
        "category": "risk_engine",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Decisiones aprobadas o rechazadas para cada intencion de orden, sin ejecutar operaciones.",
    },
    *[
        {"dataset_id": table, "table": table, "label": table.replace("_", " ").title(), "category": "paper_trading", "powerbi_ready": True, "bot_ready": True, "description": description}
        for table, description in [
            ("simulated_accounts", "Estado financiero persistido de la cuenta paper."),
            ("simulated_orders", "Ordenes paper aprobadas o rechazadas con decision de riesgo."),
            ("simulated_positions", "Posiciones paper, costo promedio y PnL realizado."),
            ("simulated_position_allocations", "Asignaciones contables por bot, version y estrategia para PnL y protecciones exactas."),
            ("simulated_fills", "Fills market simulados con precio real persistido y comision."),
            ("simulated_ledger", "Ledger de caja y eventos de la cuenta paper."),
            ("paper_bot_runtime_state", "Estado operativo activo o pausado de cada bot paper."),
            ("paper_bot_runtime_events", "Historial auditable de pausas y reanudaciones por bot."),
            ("paper_bot_circuit_breakers", "Configuracion persistida de protecciones automaticas por bot paper."),
            ("paper_bot_circuit_events", "Disparos auditables de breakers por perdidas consecutivas o rafagas de rechazo."),
            ("paper_bot_equity_snapshots", "Curva patrimonial y drawdown mark-to-market atribuida a cada bot paper."),
            ("paper_bot_sessions", "Sesiones operativas persistidas que fijan version, cadencia y modo paper de cada bot."),
            ("paper_bot_session_events", "Historial append-only de inicio, pausa, reanudacion, bloqueo y cierre de sesiones."),
        ]
    ],
    *[
        {"dataset_id": table, "table": table, "label": table.replace("_", " ").title(), "category": "spot_portfolio", "powerbi_ready": True, "bot_ready": False, "description": description}
        for table, description in [
            ("spot_portfolios", "Cuentas simuladas de acumulacion spot a largo plazo."),
            ("spot_holdings", "Tenencias spot, costo promedio y PnL realizado."),
            ("spot_transactions", "Compras y ventas spot simuladas con precio y timestamp auditables."),
            ("spot_cash_flows", "Aportes y retiros simulados separados del rendimiento de mercado."),
            ("spot_portfolio_ledger", "Ledger append-only de transacciones, caja y reinicios de ciclo."),
            ("spot_equity_snapshots", "Curva patrimonial persistida con fuente, motivo y fingerprint idempotente."),
            ("spot_dca_plans", "Planes de acumulacion spot con frecuencia, presupuesto y limite de asignacion."),
            ("spot_dca_executions", "Intentos DCA ejecutados o rechazados con referencia idempotente y evidencia."),
            ("spot_allocation_policies", "Politicas de asignacion objetivo activas o archivadas por cartera."),
            ("spot_allocation_policy_versions", "Versiones inmutables de pesos objetivo, cash residual y umbral minimo."),
            ("spot_rebalance_runs", "Planes y aplicaciones de rebalanceo simuladas con marks, metricas y ejecucion auditables."),
        ]
    ],
    {
        "dataset_id": "exchange_source_health",
        "table": "exchange_source_health",
        "label": "Exchange Source Health",
        "category": "exchange_adapters",
        "powerbi_ready": True,
        "bot_ready": False,
        "description": "Latencia, errores y ultimo check de cada endpoint CCXT publico.",
    },
    {
        "dataset_id": "execution_intents",
        "table": "execution_intents",
        "label": "Execution Intents",
        "category": "execution_gateway",
        "powerbi_ready": True,
        "bot_ready": True,
        "description": "Contrato auditable previo a Risk y a cualquier adapter backtest, paper o live.",
    },
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_table_stats(connection, table_name: str) -> dict:
    exists = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    if not exists:
        return {
            "exists": False,
            "row_count": 0,
            "last_timestamp": None,
            "columns": [],
        }

    row_count = connection.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()["count"]
    columns = [
        dict(row)
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]
    timestamp_column = first_existing_column(
        columns,
        [
            "timestamp",
            "exit_timestamp",
            "entry_timestamp",
            "published_at",
            "checked_at",
            "fetched_at",
            "open_time",
            "close_time",
            "created_at",
            "updated_at",
            "filled_at",
        ],
    )
    last_timestamp = None
    if timestamp_column:
        result = connection.execute(f"SELECT MAX({timestamp_column}) AS last_timestamp FROM {table_name}").fetchone()
        last_timestamp = result["last_timestamp"] if result else None

    return {
        "exists": True,
        "row_count": int(row_count or 0),
        "last_timestamp": last_timestamp,
        "columns": [
            {
                "name": column["name"],
                "type": column["type"],
                "required": not bool(column["notnull"] == 0),
            }
            for column in columns
        ],
    }


def get_table_stats(table_name: str) -> dict:
    initialize_database()
    with connect() as connection:
        return _get_table_stats(connection, table_name)


def first_existing_column(columns: list[dict], candidates: list[str]) -> str | None:
    names = {column["name"] for column in columns}
    for candidate in candidates:
        if candidate in names:
            return candidate
    return None


def get_data_catalog() -> dict:
    return {
        "generated_at": utc_now_iso(),
        "db_path": str(DB_PATH),
        "sources": SOURCE_CATALOG,
        "datasets": build_datasets(),
        "principle": "APIs externas -> normalizacion -> SQLite/cache -> datasets analiticos -> frontend/bots/PowerBI",
    }


def get_data_sources() -> dict:
    health_by_source = {record["source"].lower(): record for record in list_source_health()}
    sources = []
    for source in SOURCE_CATALOG:
        source_health = health_by_source.get(source["source_id"]) or health_by_source.get(source["name"].lower())
        sources.append(
            {
                **source,
                "health": source_health,
            }
        )
    return {"generated_at": utc_now_iso(), "sources": sources}


def get_data_health() -> dict:
    datasets = build_datasets()
    source_health = list_source_health()
    existing = [dataset for dataset in datasets if dataset["exists"]]
    missing = [dataset for dataset in datasets if not dataset["exists"]]
    stale_or_empty = [
        dataset
        for dataset in existing
        if dataset["row_count"] == 0 or not dataset.get("last_timestamp")
    ]

    return {
        "generated_at": utc_now_iso(),
        "database": {
            "path": str(DB_PATH),
            "exists": DB_PATH.exists(),
            "size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        },
        "summary": {
            "datasets_total": len(datasets),
            "datasets_existing": len(existing),
            "datasets_missing_or_planned": len(missing),
            "datasets_empty_or_without_time": len(stale_or_empty),
            "sources_with_health": len(source_health),
        },
        "sources": source_health,
        "datasets": datasets,
    }


def get_data_datasets() -> dict:
    return {"generated_at": utc_now_iso(), "datasets": build_datasets()}


def get_dataset_definition(dataset_id: str) -> dict:
    for dataset in DATASET_CATALOG:
        if dataset["dataset_id"] == dataset_id:
            return dataset
    raise ValueError(f"Dataset no catalogado: {dataset_id}")


def get_dataset_preview(dataset_id: str, limit: int = 25) -> dict:
    dataset = get_dataset_definition(dataset_id)
    table = dataset["table"]
    stats = get_table_stats(table)
    if not stats["exists"]:
        return {
            "generated_at": utc_now_iso(),
            "dataset": {**dataset, **stats, "status": "missing"},
            "rows": [],
        }

    order_column = first_existing_column(
        stats["columns"],
        [
            "timestamp",
            "exit_timestamp",
            "entry_timestamp",
            "published_at",
            "checked_at",
            "fetched_at",
            "open_time",
            "close_time",
            "created_at",
            "updated_at",
            "filled_at",
            "id",
        ],
    )
    order_sql = f"ORDER BY {order_column} DESC" if order_column else ""
    with connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM {table} {order_sql} LIMIT ?",
            (limit,),
        ).fetchall()

    return {
        "generated_at": utc_now_iso(),
        "dataset": {
            **dataset,
            **stats,
            "status": "ready" if stats["row_count"] else "empty",
        },
        "rows": [dict(row) for row in rows],
    }


def export_dataset_csv(dataset_id: str, limit: int = 5000) -> str:
    preview = get_dataset_preview(dataset_id=dataset_id, limit=limit)
    rows = preview["rows"]
    output = io.StringIO()
    if not rows:
        writer = csv.writer(output)
        writer.writerow(["empty"])
        return output.getvalue()

    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def build_datasets() -> list[dict]:
    initialize_database()
    datasets = []
    with connect() as connection:
        for dataset in DATASET_CATALOG:
            stats = _get_table_stats(connection, dataset["table"])
            status = dataset.get("status")
            if stats["exists"] and stats["row_count"] > 0:
                status = "ready"
            elif stats["exists"]:
                status = "empty"
            elif not status:
                status = "missing"
            datasets.append(
                {
                    **dataset,
                    **stats,
                    "status": status,
                }
            )
    return datasets
