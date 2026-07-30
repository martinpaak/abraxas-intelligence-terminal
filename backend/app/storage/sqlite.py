from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from backend.app.core.config import DB_PATH
from backend.app.strategies.contracts import compile_strategy

LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    change_24h REAL NOT NULL,
    volume_24h REAL NOT NULL,
    fear_greed_value INTEGER NOT NULL,
    fear_greed_label TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    abraxas_reading TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_symbol_time
ON market_snapshots(symbol, timestamp);

CREATE TABLE IF NOT EXISTS macro_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(series_id, observation_date)
);

CREATE INDEX IF NOT EXISTS idx_macro_observations_category_date
ON macro_observations(category, observation_date);

CREATE TABLE IF NOT EXISTS market_candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    close_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    quote_volume REAL NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, timeframe, open_time)
);

CREATE INDEX IF NOT EXISTS idx_market_candles_symbol_time
ON market_candles(symbol, timeframe, open_time);

CREATE INDEX IF NOT EXISTS idx_market_candles_open_time
ON market_candles(open_time);

CREATE TABLE IF NOT EXISTS asset_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    return_1 REAL NOT NULL,
    return_5 REAL NOT NULL,
    return_20 REAL NOT NULL,
    volatility REAL NOT NULL,
    z_score REAL NOT NULL,
    drawdown REAL NOT NULL,
    trend_strength REAL NOT NULL,
    volume_change REAL NOT NULL,
    risk_score REAL NOT NULL,
    regime_label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_asset_features_symbol_time
ON asset_features(symbol, timeframe, timestamp);

CREATE INDEX IF NOT EXISTS idx_asset_features_regime
ON asset_features(regime_label, timestamp);

CREATE TABLE IF NOT EXISTS statistics_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    run_type TEXT NOT NULL,
    input_start INTEGER,
    input_end INTEGER,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_statistics_runs_symbol_time
ON statistics_runs(symbol, timeframe, created_at);

CREATE INDEX IF NOT EXISTS idx_statistics_runs_type_time
ON statistics_runs(run_type, created_at);

CREATE TABLE IF NOT EXISTS regime_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    regime_label TEXT NOT NULL,
    confidence REAL NOT NULL,
    risk_score REAL NOT NULL,
    market_bias TEXT NOT NULL,
    volatility_state TEXT NOT NULL,
    trend_state TEXT NOT NULL,
    drawdown_state TEXT NOT NULL,
    feature_count INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    reading TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_regime_snapshots_symbol_time
ON regime_snapshots(symbol, timeframe, timestamp);

CREATE INDEX IF NOT EXISTS idx_regime_snapshots_label_time
ON regime_snapshots(regime_label, timestamp);

CREATE TABLE IF NOT EXISTS live_events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    lat REAL,
    lon REAL,
    country TEXT,
    url TEXT,
    severity TEXT NOT NULL,
    published_at TEXT NOT NULL,
    freshness_minutes INTEGER NOT NULL,
    related_assets TEXT NOT NULL,
    vector_tags TEXT NOT NULL,
    raw_url TEXT,
    fetched_at TEXT NOT NULL,
    raw_payload TEXT,
    UNIQUE(source, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_live_events_source_url
ON live_events(source, url)
WHERE url IS NOT NULL AND url != '';

CREATE INDEX IF NOT EXISTS idx_live_events_type_time
ON live_events(type, published_at);

CREATE INDEX IF NOT EXISTS idx_live_events_severity_time
ON live_events(severity, published_at);

CREATE TABLE IF NOT EXISTS live_source_health (
    source TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    last_success_at TEXT,
    latency_ms INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    error TEXT,
    max_stale_minutes INTEGER NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    base_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    risk_profile TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bots_status
ON bots(status, updated_at);

CREATE TABLE IF NOT EXISTS bot_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    strategy_json TEXT NOT NULL,
    contract_json TEXT,
    strategy_hash TEXT,
    validation_status TEXT NOT NULL DEFAULT 'legacy',
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
    UNIQUE(bot_id, version)
);

CREATE INDEX IF NOT EXISTS idx_bot_versions_bot
ON bot_versions(bot_id, version);

CREATE TABLE IF NOT EXISTS strategy_signal_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    bot_version_id INTEGER NOT NULL,
    strategy_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    feature_timestamp INTEGER NOT NULL,
    evaluation_key TEXT,
    signal TEXT NOT NULL CHECK(signal IN ('entry_candidate', 'exit_candidate', 'hold')),
    entry_passed INTEGER NOT NULL CHECK(entry_passed IN (0, 1)),
    exit_passed INTEGER NOT NULL CHECK(exit_passed IN (0, 1)),
    conflict INTEGER NOT NULL DEFAULT 0 CHECK(conflict IN (0, 1)),
    trigger_reason TEXT,
    price_timestamp TEXT,
    position_return_pct REAL,
    features_json TEXT NOT NULL,
    trace_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_strategy_signals_bot_time
ON strategy_signal_evaluations(bot_id, evaluated_at);

CREATE TABLE IF NOT EXISTS liquidity_sweep_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_timestamp INTEGER NOT NULL,
    state TEXT NOT NULL,
    direction TEXT NOT NULL,
    order_allowed INTEGER NOT NULL CHECK(order_allowed IN (0, 1)),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_liquidity_sweeps_symbol_time
ON liquidity_sweep_evaluations(symbol, timeframe, candle_timestamp);

CREATE TABLE IF NOT EXISTS market_aggregate_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    aggregate_trade_id INTEGER NOT NULL,
    first_trade_id INTEGER NOT NULL,
    last_trade_id INTEGER NOT NULL,
    event_time INTEGER NOT NULL,
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    quote_quantity REAL NOT NULL,
    buyer_is_maker INTEGER NOT NULL CHECK(buyer_is_maker IN (0, 1)),
    aggressor_side TEXT NOT NULL CHECK(aggressor_side IN ('buy', 'sell')),
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(symbol, aggregate_trade_id)
);

CREATE INDEX IF NOT EXISTS idx_market_agg_trades_symbol_time
ON market_aggregate_trades(symbol, event_time);

CREATE TABLE IF NOT EXISTS order_book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    last_update_id INTEGER,
    best_bid REAL,
    best_ask REAL,
    spread REAL,
    spread_percent REAL,
    mid_price REAL,
    level_count INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_symbol_time
ON order_book_snapshots(symbol, fetched_at);

CREATE TABLE IF NOT EXISTS order_book_levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('bid', 'ask')),
    level_index INTEGER NOT NULL,
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    notional REAL NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES order_book_snapshots(id) ON DELETE CASCADE,
    UNIQUE(snapshot_id, side, level_index)
);

CREATE INDEX IF NOT EXISTS idx_order_book_levels_snapshot_side
ON order_book_levels(snapshot_id, side, level_index);

CREATE TABLE IF NOT EXISTS order_book_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    event_time INTEGER NOT NULL,
    first_update_id INTEGER NOT NULL,
    final_update_id INTEGER NOT NULL,
    bid_changes_json TEXT NOT NULL,
    ask_changes_json TEXT NOT NULL,
    level_change_count INTEGER NOT NULL,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(symbol, final_update_id)
);

CREATE INDEX IF NOT EXISTS idx_order_book_deltas_symbol_time
ON order_book_deltas(symbol, event_time);

CREATE TABLE IF NOT EXISTS microstructure_collector_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbols_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('starting', 'running', 'stopping', 'stopped', 'failed', 'interrupted')),
    messages_received INTEGER NOT NULL DEFAULT 0,
    trades_saved INTEGER NOT NULL DEFAULT 0,
    deltas_saved INTEGER NOT NULL DEFAULT 0,
    snapshots_saved INTEGER NOT NULL DEFAULT 0,
    reconnect_count INTEGER NOT NULL DEFAULT 0,
    sequence_gap_count INTEGER NOT NULL DEFAULT 0,
    last_event_at TEXT,
    last_error TEXT,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_microstructure_collector_runs_status_time
ON microstructure_collector_runs(status, started_at);

CREATE TABLE IF NOT EXISTS chart_indicator_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    active_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_chart_indicator_presets_symbol_timeframe
ON chart_indicator_presets(symbol, timeframe, status);

CREATE TABLE IF NOT EXISTS chart_indicator_preset_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preset_id INTEGER NOT NULL,
    version_number INTEGER NOT NULL,
    indicators_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(preset_id) REFERENCES chart_indicator_presets(id),
    UNIQUE(preset_id, version_number),
    UNIQUE(preset_id, config_hash)
);

CREATE INDEX IF NOT EXISTS idx_chart_indicator_preset_versions_preset
ON chart_indicator_preset_versions(preset_id, version_number);

CREATE TABLE IF NOT EXISTS paper_order_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_evaluation_id INTEGER NOT NULL UNIQUE,
    bot_id INTEGER NOT NULL,
    bot_version_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('buy', 'sell')),
    quantity REAL NOT NULL,
    reference_price REAL NOT NULL,
    proposed_notional REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'submitted', 'dismissed')),
    reason TEXT NOT NULL,
    strategy_hash TEXT,
    price_timestamp TEXT,
    expires_at TEXT,
    allocation_id INTEGER,
    allocation_revision INTEGER,
    trigger_reason TEXT,
    execution_intent_id TEXT,
    risk_validation_id INTEGER,
    result_reference TEXT,
    submitted_at TEXT,
    claim_token TEXT,
    claimed_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(signal_evaluation_id) REFERENCES strategy_signal_evaluations(id),
    FOREIGN KEY(bot_id) REFERENCES bots(id),
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id)
);

CREATE INDEX IF NOT EXISTS idx_paper_proposals_bot_status
ON paper_order_proposals(bot_id, status, created_at);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    bot_version_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    input_start INTEGER,
    input_end INTEGER,
    initial_equity REAL NOT NULL,
    final_equity REAL NOT NULL,
    roi_pct REAL NOT NULL,
    max_drawdown_pct REAL NOT NULL,
    total_trades INTEGER NOT NULL,
    win_rate_pct REAL NOT NULL,
    profit_factor REAL,
    metrics_json TEXT NOT NULL,
    trades_json TEXT NOT NULL,
    equity_curve_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_bot_time
ON backtest_runs(bot_id, created_at);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_symbol_time
ON backtest_runs(symbol, timeframe, created_at);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    bot_version_id INTEGER NOT NULL,
    trade_index INTEGER NOT NULL,
    side TEXT NOT NULL DEFAULT 'long',
    entry_signal_timestamp INTEGER,
    exit_signal_timestamp INTEGER,
    entry_timestamp INTEGER NOT NULL,
    exit_timestamp INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    quantity REAL NOT NULL,
    allocated_equity REAL,
    gross_pnl REAL,
    entry_fee REAL,
    exit_fee REAL,
    fees_paid REAL,
    slippage_cost REAL,
    pnl REAL NOT NULL,
    return_pct REAL NOT NULL,
    bars_held INTEGER,
    exit_reason TEXT,
    forced_exit INTEGER NOT NULL DEFAULT 0 CHECK(forced_exit IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(backtest_id) REFERENCES backtest_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id) ON DELETE CASCADE,
    UNIQUE(backtest_id, trade_index)
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_exit
ON backtest_trades(backtest_id, exit_timestamp);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_bot_version
ON backtest_trades(bot_id, bot_version_id, exit_timestamp);

CREATE TABLE IF NOT EXISTS backtest_equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    bot_version_id INTEGER NOT NULL,
    point_index INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    equity REAL NOT NULL,
    benchmark_equity REAL,
    close REAL NOT NULL,
    drawdown_pct REAL,
    in_position INTEGER NOT NULL CHECK(in_position IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(backtest_id) REFERENCES backtest_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id) ON DELETE CASCADE,
    UNIQUE(backtest_id, point_index)
);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_run_time
ON backtest_equity(backtest_id, timestamp, point_index);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_bot_version
ON backtest_equity(bot_id, bot_version_id, timestamp);

CREATE TABLE IF NOT EXISTS risk_limits (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    max_position_pct REAL NOT NULL CHECK (max_position_pct > 0 AND max_position_pct <= 100),
    max_daily_loss_pct REAL NOT NULL CHECK (max_daily_loss_pct > 0 AND max_daily_loss_pct <= 100),
    max_drawdown_pct REAL NOT NULL CHECK (max_drawdown_pct > 0 AND max_drawdown_pct <= 100),
    cooldown_minutes INTEGER NOT NULL CHECK (cooldown_minutes >= 0),
    symbol_whitelist TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('account', 'bot')),
    scope_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS risk_policy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    max_position_pct REAL NOT NULL CHECK (max_position_pct > 0 AND max_position_pct <= 100),
    max_daily_loss_pct REAL NOT NULL CHECK (max_daily_loss_pct > 0 AND max_daily_loss_pct <= 100),
    max_drawdown_pct REAL NOT NULL CHECK (max_drawdown_pct > 0 AND max_drawdown_pct <= 100),
    cooldown_minutes INTEGER NOT NULL CHECK (cooldown_minutes >= 0),
    symbol_whitelist TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, version),
    FOREIGN KEY(policy_id) REFERENCES risk_policies(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_risk_policies_scope_status
ON risk_policies(scope_type, scope_id, status);

CREATE INDEX IF NOT EXISTS idx_risk_policy_versions_policy_version
ON risk_policy_versions(policy_id, version);

CREATE TABLE IF NOT EXISTS risk_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kill_switch_active INTEGER NOT NULL CHECK (kill_switch_active IN (0, 1)),
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_audit_log_created
ON risk_audit_log(created_at);

CREATE TABLE IF NOT EXISTS risk_validation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    account_id INTEGER,
    bot_id INTEGER,
    policy_fingerprint TEXT,
    policy_resolution_json TEXT,
    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
    request_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_validation_symbol_created
ON risk_validation_log(symbol, created_at);

CREATE TABLE IF NOT EXISTS simulated_accounts (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance REAL NOT NULL,
    cash_balance REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    peak_equity REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spot_portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'USDT',
    initial_cash REAL NOT NULL,
    cash_balance REAL NOT NULL,
    active_cycle INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spot_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    average_cost REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(portfolio_id, symbol),
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id)
);

CREATE TABLE IF NOT EXISTS spot_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    notional REAL NOT NULL,
    fee REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    price_timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    notes TEXT,
    cycle_number INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'manual',
    origin_reference TEXT,
    risk_validation_id INTEGER,
    executed_at TEXT NOT NULL,
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id),
    FOREIGN KEY(risk_validation_id) REFERENCES risk_validation_log(id)
);

CREATE INDEX IF NOT EXISTS idx_spot_transactions_portfolio_time
ON spot_transactions(portfolio_id, executed_at);

CREATE TABLE IF NOT EXISTS spot_cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    cycle_number INTEGER NOT NULL,
    flow_type TEXT NOT NULL CHECK(flow_type IN ('deposit', 'withdrawal')),
    amount REAL NOT NULL,
    cash_delta REAL NOT NULL,
    cash_balance REAL NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id)
);

CREATE TABLE IF NOT EXISTS spot_portfolio_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    cycle_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    reference_id INTEGER,
    symbol TEXT,
    cash_delta REAL NOT NULL DEFAULT 0,
    quantity_delta REAL NOT NULL DEFAULT 0,
    realized_pnl_delta REAL NOT NULL DEFAULT 0,
    cash_balance REAL NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id)
);

CREATE TABLE IF NOT EXISTS spot_equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    cycle_number INTEGER NOT NULL,
    equity REAL NOT NULL,
    cash_balance REAL NOT NULL,
    market_value REAL NOT NULL,
    cost_basis REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    source_timestamp TEXT,
    fingerprint TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id),
    UNIQUE(portfolio_id, cycle_number, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_spot_cash_flows_cycle_time
ON spot_cash_flows(portfolio_id, cycle_number, created_at);

CREATE INDEX IF NOT EXISTS idx_spot_ledger_cycle_time
ON spot_portfolio_ledger(portfolio_id, cycle_number, created_at);

CREATE INDEX IF NOT EXISTS idx_spot_equity_cycle_time
ON spot_equity_snapshots(portfolio_id, cycle_number, recorded_at);

CREATE TABLE IF NOT EXISTS spot_dca_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    budget_amount REAL NOT NULL,
    frequency TEXT NOT NULL CHECK(frequency IN ('weekly', 'monthly')),
    interval_count INTEGER NOT NULL DEFAULT 1,
    allocation_limit_pct REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'archived')),
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id)
);

CREATE TABLE IF NOT EXISTS spot_dca_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    portfolio_id INTEGER NOT NULL,
    cycle_number INTEGER NOT NULL,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('executed', 'rejected')),
    transaction_id INTEGER,
    quantity REAL,
    price REAL,
    notional REAL,
    reason TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES spot_dca_plans(id),
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id),
    FOREIGN KEY(transaction_id) REFERENCES spot_transactions(id),
    UNIQUE(plan_id, scheduled_for)
);

CREATE INDEX IF NOT EXISTS idx_spot_dca_plans_status_due
ON spot_dca_plans(status, next_run_at);

CREATE INDEX IF NOT EXISTS idx_spot_dca_executions_plan_time
ON spot_dca_executions(plan_id, created_at);

CREATE TABLE IF NOT EXISTS spot_allocation_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
    active_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(portfolio_id, name),
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id)
);

CREATE TABLE IF NOT EXISTS spot_allocation_policy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id INTEGER NOT NULL,
    version_number INTEGER NOT NULL,
    targets_json TEXT NOT NULL,
    min_trade_notional REAL NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, version_number),
    UNIQUE(policy_id, config_hash),
    FOREIGN KEY(policy_id) REFERENCES spot_allocation_policies(id)
);

CREATE TABLE IF NOT EXISTS spot_rebalance_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    policy_id INTEGER NOT NULL,
    policy_version_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'applying', 'applied', 'partial')),
    portfolio_state_hash TEXT NOT NULL,
    equity_at_plan REAL NOT NULL,
    cash_at_plan REAL NOT NULL,
    source_timestamp TEXT,
    plan_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    execution_json TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(portfolio_id) REFERENCES spot_portfolios(id),
    FOREIGN KEY(policy_id) REFERENCES spot_allocation_policies(id),
    FOREIGN KEY(policy_version_id) REFERENCES spot_allocation_policy_versions(id)
);

CREATE INDEX IF NOT EXISTS idx_spot_allocation_policies_portfolio
ON spot_allocation_policies(portfolio_id, status, updated_at);

CREATE INDEX IF NOT EXISTS idx_spot_rebalance_runs_policy_time
ON spot_rebalance_runs(policy_id, created_at);

CREATE TABLE IF NOT EXISTS simulated_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    bot_id INTEGER,
    bot_version_id INTEGER,
    strategy_hash TEXT,
    signal_evaluation_id INTEGER,
    proposal_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity REAL NOT NULL,
    status TEXT NOT NULL,
    reference_price REAL NOT NULL,
    fill_price REAL,
    fee REAL,
    risk_validation_id INTEGER NOT NULL,
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES simulated_accounts(id),
    FOREIGN KEY(bot_id) REFERENCES bots(id),
    FOREIGN KEY(risk_validation_id) REFERENCES risk_validation_log(id)
);

CREATE TABLE IF NOT EXISTS simulated_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    average_price REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, symbol),
    FOREIGN KEY(account_id) REFERENCES simulated_accounts(id)
);

CREATE TABLE IF NOT EXISTS simulated_position_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    bot_id INTEGER,
    bot_version_id INTEGER,
    strategy_hash TEXT,
    quantity REAL NOT NULL,
    average_price REAL NOT NULL,
    entry_fee_remaining REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES simulated_accounts(id),
    FOREIGN KEY(bot_id) REFERENCES bots(id),
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id),
    UNIQUE(account_id, symbol, owner_key)
);

CREATE INDEX IF NOT EXISTS idx_position_allocations_bot_symbol
ON simulated_position_allocations(bot_id, bot_version_id, symbol, quantity);

CREATE TABLE IF NOT EXISTS paper_position_protections (
    allocation_id INTEGER PRIMARY KEY,
    stop_loss_price REAL,
    take_profit_price REAL,
    trailing_distance_pct REAL,
    highest_price REAL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(allocation_id) REFERENCES simulated_position_allocations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS simulated_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    filled_at TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES simulated_orders(id),
    FOREIGN KEY(account_id) REFERENCES simulated_accounts(id)
);

CREATE TABLE IF NOT EXISTS simulated_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    reference_id INTEGER,
    symbol TEXT,
    cash_delta REAL NOT NULL,
    realized_pnl_delta REAL NOT NULL,
    cash_balance REAL NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES simulated_accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_simulated_orders_created ON simulated_orders(created_at);
CREATE INDEX IF NOT EXISTS idx_simulated_fills_filled ON simulated_fills(filled_at);
CREATE INDEX IF NOT EXISTS idx_simulated_ledger_created ON simulated_ledger(created_at);

CREATE TABLE IF NOT EXISTS paper_bot_runtime_state (
    bot_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused')),
    reason TEXT NOT NULL,
    paused_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS paper_bot_runtime_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('paused', 'resumed', 'auto_resumed')),
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    paused_until TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_paper_bot_runtime_events_bot_time
ON paper_bot_runtime_events(bot_id, created_at);

CREATE TABLE IF NOT EXISTS paper_bot_circuit_breakers (
    bot_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    max_consecutive_losses INTEGER NOT NULL DEFAULT 3 CHECK (max_consecutive_losses BETWEEN 1 AND 100),
    max_rejections INTEGER NOT NULL DEFAULT 5 CHECK (max_rejections BETWEEN 1 AND 1000),
    rejection_window_minutes INTEGER NOT NULL DEFAULT 15 CHECK (rejection_window_minutes BETWEEN 1 AND 1440),
    max_drawdown_pct REAL NOT NULL DEFAULT 10 CHECK (max_drawdown_pct > 0 AND max_drawdown_pct <= 100),
    pause_minutes INTEGER NOT NULL DEFAULT 60 CHECK (pause_minutes BETWEEN 1 AND 43200),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS paper_bot_circuit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    trigger_code TEXT NOT NULL,
    observed_value REAL NOT NULL,
    threshold_value REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('paused', 'observed')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_paper_bot_circuit_events_bot_time
ON paper_bot_circuit_events(bot_id, created_at);

CREATE TABLE IF NOT EXISTS paper_bot_equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    equity REAL NOT NULL,
    peak_equity REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    pnl REAL NOT NULL,
    open_value REAL NOT NULL,
    capital_basis REAL NOT NULL,
    source_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
    UNIQUE(bot_id, source_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_paper_bot_equity_snapshots_bot_time
ON paper_bot_equity_snapshots(bot_id, created_at);

CREATE TABLE IF NOT EXISTS paper_bot_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    bot_version_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'paused', 'blocked', 'error', 'stopped')),
    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('observe', 'auto_paper')),
    cadence_seconds INTEGER NOT NULL CHECK (cadence_seconds BETWEEN 60 AND 86400),
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    last_signal_evaluation_id INTEGER,
    last_proposal_id INTEGER,
    last_error TEXT,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id),
    FOREIGN KEY(last_signal_evaluation_id) REFERENCES strategy_signal_evaluations(id),
    FOREIGN KEY(last_proposal_id) REFERENCES paper_order_proposals(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_bot_sessions_one_active
ON paper_bot_sessions(bot_id)
WHERE status IN ('running', 'paused', 'blocked', 'error');

CREATE INDEX IF NOT EXISTS idx_paper_bot_sessions_due
ON paper_bot_sessions(status, next_run_at);

CREATE TABLE IF NOT EXISTS paper_bot_session_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    bot_version_id INTEGER NOT NULL,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'skipped', 'error')),
    signal TEXT,
    signal_evaluation_id INTEGER,
    proposal_id INTEGER,
    simulated_order_id INTEGER,
    result_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(session_id) REFERENCES paper_bot_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id),
    FOREIGN KEY(signal_evaluation_id) REFERENCES strategy_signal_evaluations(id),
    FOREIGN KEY(proposal_id) REFERENCES paper_order_proposals(id),
    FOREIGN KEY(simulated_order_id) REFERENCES simulated_orders(id),
    UNIQUE(session_id, scheduled_for)
);

CREATE INDEX IF NOT EXISTS idx_paper_bot_session_runs_session_time
ON paper_bot_session_runs(session_id, scheduled_for);

CREATE TABLE IF NOT EXISTS paper_bot_session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    previous_status TEXT,
    new_status TEXT,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES paper_bot_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_paper_bot_session_events_session_time
ON paper_bot_session_events(session_id, created_at);

CREATE TABLE IF NOT EXISTS exchange_source_health (
    exchange_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    latency_ms INTEGER NOT NULL,
    error TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY(exchange_id, endpoint)
);

CREATE TABLE IF NOT EXISTS execution_intents (
    id TEXT PRIMARY KEY,
    environment TEXT NOT NULL CHECK (environment IN ('backtest', 'paper', 'live')),
    adapter TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('buy', 'sell')),
    order_type TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
    quantity REAL NOT NULL,
    limit_price REAL,
    bot_id INTEGER,
    bot_version_id INTEGER,
    strategy_hash TEXT,
    signal_evaluation_id INTEGER,
    proposal_id INTEGER,
    status TEXT NOT NULL,
    risk_validation_id INTEGER,
    result_reference TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(bot_id) REFERENCES bots(id),
    FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id),
    FOREIGN KEY(signal_evaluation_id) REFERENCES strategy_signal_evaluations(id),
    FOREIGN KEY(proposal_id) REFERENCES paper_order_proposals(id)
);

CREATE INDEX IF NOT EXISTS idx_execution_intents_environment_created
ON execution_intents(environment, created_at);
"""

BACKTEST_INTEGRITY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_backtest_runs_version_bot_insert
BEFORE INSERT ON backtest_runs
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM bot_versions
    WHERE id = NEW.bot_version_id AND bot_id = NEW.bot_id
)
BEGIN
    SELECT RAISE(ABORT, 'backtest version does not belong to bot');
END;

CREATE TRIGGER IF NOT EXISTS trg_backtest_runs_version_bot_update
BEFORE UPDATE OF bot_id, bot_version_id ON backtest_runs
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM bot_versions
    WHERE id = NEW.bot_version_id AND bot_id = NEW.bot_id
)
BEGIN
    SELECT RAISE(ABORT, 'backtest version does not belong to bot');
END;

CREATE TRIGGER IF NOT EXISTS trg_backtest_trades_parent_insert
BEFORE INSERT ON backtest_trades
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM backtest_runs
    WHERE id = NEW.backtest_id
      AND bot_id = NEW.bot_id
      AND bot_version_id = NEW.bot_version_id
)
BEGIN
    SELECT RAISE(ABORT, 'backtest trade does not match parent run');
END;

CREATE TRIGGER IF NOT EXISTS trg_backtest_trades_parent_update
BEFORE UPDATE OF backtest_id, bot_id, bot_version_id ON backtest_trades
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM backtest_runs
    WHERE id = NEW.backtest_id
      AND bot_id = NEW.bot_id
      AND bot_version_id = NEW.bot_version_id
)
BEGIN
    SELECT RAISE(ABORT, 'backtest trade does not match parent run');
END;

CREATE TRIGGER IF NOT EXISTS trg_backtest_equity_parent_insert
BEFORE INSERT ON backtest_equity
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM backtest_runs
    WHERE id = NEW.backtest_id
      AND bot_id = NEW.bot_id
      AND bot_version_id = NEW.bot_version_id
)
BEGIN
    SELECT RAISE(ABORT, 'backtest equity does not match parent run');
END;

CREATE TRIGGER IF NOT EXISTS trg_backtest_equity_parent_update
BEFORE UPDATE OF backtest_id, bot_id, bot_version_id ON backtest_equity
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM backtest_runs
    WHERE id = NEW.backtest_id
      AND bot_id = NEW.bot_id
      AND bot_version_id = NEW.bot_version_id
)
BEGIN
    SELECT RAISE(ABORT, 'backtest equity does not match parent run');
END;
"""

BACKTEST_SCHEMA_VERSION = 2

@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _parse_legacy_list(value: object, run_id: int, field: str) -> list[dict]:
    try:
        parsed = json.loads(str(value)) if value else []
    except (json.JSONDecodeError, TypeError, ValueError):
        LOGGER.warning("Skipping corrupt %s for legacy backtest run %s", field, run_id)
        return []
    if not isinstance(parsed, list):
        LOGGER.warning("Skipping non-list %s for legacy backtest run %s", field, run_id)
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _migrate_nullable_profit_factor(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]: row
        for row in connection.execute("PRAGMA table_info(backtest_runs)").fetchall()
    }
    profit_factor_column = columns.get("profit_factor")
    if not profit_factor_column or not int(profit_factor_column["notnull"]):
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA legacy_alter_table = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE backtest_runs RENAME TO backtest_runs_v1")
        connection.execute(
            """
            CREATE TABLE backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL,
                bot_version_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                input_start INTEGER,
                input_end INTEGER,
                initial_equity REAL NOT NULL,
                final_equity REAL NOT NULL,
                roi_pct REAL NOT NULL,
                max_drawdown_pct REAL NOT NULL,
                total_trades INTEGER NOT NULL,
                win_rate_pct REAL NOT NULL,
                profit_factor REAL,
                metrics_json TEXT NOT NULL,
                trades_json TEXT NOT NULL,
                equity_curve_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE,
                FOREIGN KEY(bot_version_id) REFERENCES bot_versions(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO backtest_runs (
                id, bot_id, bot_version_id, symbol, timeframe, input_start, input_end,
                initial_equity, final_equity, roi_pct, max_drawdown_pct, total_trades,
                win_rate_pct, profit_factor, metrics_json, trades_json,
                equity_curve_json, created_at
            )
            SELECT
                id, bot_id, bot_version_id, symbol, timeframe, input_start, input_end,
                initial_equity, final_equity, roi_pct, max_drawdown_pct, total_trades,
                win_rate_pct, profit_factor, metrics_json, trades_json,
                equity_curve_json, created_at
            FROM backtest_runs_v1
            """
        )
        connection.execute("DROP TABLE backtest_runs_v1")
        connection.execute(
            "CREATE INDEX idx_backtest_runs_bot_time ON backtest_runs(bot_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX idx_backtest_runs_symbol_time ON backtest_runs(symbol, timeframe, created_at)"
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA legacy_alter_table = OFF")
        connection.execute("PRAGMA foreign_keys = ON")


def _backfill_backtest_payloads(connection: sqlite3.Connection) -> None:
    runs = connection.execute(
        """
        SELECT id, bot_id, bot_version_id, trades_json, equity_curve_json, created_at
        FROM backtest_runs
        ORDER BY id
        """
    ).fetchall()
    for run in runs:
        trades = _parse_legacy_list(run["trades_json"], int(run["id"]), "trades_json")
        equity_curve = _parse_legacy_list(run["equity_curve_json"], int(run["id"]), "equity_curve_json")
        trade_rows = []
        for trade_index, trade in enumerate(trades, start=1):
            try:
                forced_exit = bool(trade.get("forced_exit"))
                trade_rows.append(
                    {
                        "backtest_id": int(run["id"]),
                        "bot_id": int(run["bot_id"]),
                        "bot_version_id": int(run["bot_version_id"]),
                        "trade_index": trade_index,
                        "side": str(trade.get("side") or "long"),
                        "entry_timestamp": int(trade["entry_timestamp"]),
                        "exit_timestamp": int(trade["exit_timestamp"]),
                        "entry_price": float(trade["entry_price"]),
                        "exit_price": float(trade["exit_price"]),
                        "quantity": float(trade["quantity"]),
                        "pnl": float(trade.get("pnl") or 0),
                        "return_pct": float(trade.get("return_pct") or 0),
                        "exit_reason": str(
                            trade.get("exit_reason") or ("end_of_data" if forced_exit else "legacy")
                        ),
                        "forced_exit": int(forced_exit),
                        "created_at": run["created_at"],
                    }
                )
            except (KeyError, TypeError, ValueError):
                LOGGER.warning(
                    "Skipping invalid trade %s for legacy backtest run %s",
                    trade_index,
                    run["id"],
                )
        if trade_rows:
            connection.executemany(
                """
                INSERT OR IGNORE INTO backtest_trades (
                    backtest_id, bot_id, bot_version_id, trade_index, side,
                    entry_timestamp, exit_timestamp, entry_price, exit_price,
                    quantity, pnl, return_pct, exit_reason, forced_exit, created_at
                ) VALUES (
                    :backtest_id, :bot_id, :bot_version_id, :trade_index, :side,
                    :entry_timestamp, :exit_timestamp, :entry_price, :exit_price,
                    :quantity, :pnl, :return_pct, :exit_reason, :forced_exit, :created_at
                )
                """,
                trade_rows,
            )

        equity_rows = []
        for point_index, point in enumerate(equity_curve, start=1):
            try:
                equity_rows.append(
                    {
                        "backtest_id": int(run["id"]),
                        "bot_id": int(run["bot_id"]),
                        "bot_version_id": int(run["bot_version_id"]),
                        "point_index": point_index,
                        "timestamp": int(point["timestamp"]),
                        "equity": float(point["equity"]),
                        "benchmark_equity": point.get("benchmark_equity"),
                        "close": float(point["close"]),
                        "drawdown_pct": point.get("drawdown_pct"),
                        "in_position": int(bool(point.get("in_position"))),
                        "created_at": run["created_at"],
                    }
                )
            except (KeyError, TypeError, ValueError):
                LOGGER.warning(
                    "Skipping invalid equity point %s for legacy backtest run %s",
                    point_index,
                    run["id"],
                )
        if equity_rows:
            connection.executemany(
                """
                INSERT OR IGNORE INTO backtest_equity (
                    backtest_id, bot_id, bot_version_id, point_index, timestamp,
                    equity, benchmark_equity, close, drawdown_pct, in_position, created_at
                ) VALUES (
                    :backtest_id, :bot_id, :bot_version_id, :point_index, :timestamp,
                    :equity, :benchmark_equity, :close, :drawdown_pct, :in_position, :created_at
                )
                """,
                equity_rows,
            )


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)
        execution_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(execution_intents)").fetchall()
        }
        if "risk_validation_id" not in execution_columns:
            connection.execute("ALTER TABLE execution_intents ADD COLUMN risk_validation_id INTEGER")
        for name, column_type in {
            "bot_version_id": "INTEGER",
            "strategy_hash": "TEXT",
            "signal_evaluation_id": "INTEGER",
            "proposal_id": "INTEGER",
        }.items():
            if name not in execution_columns:
                connection.execute(f"ALTER TABLE execution_intents ADD COLUMN {name} {column_type}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_intents_proposal ON execution_intents(proposal_id) WHERE proposal_id IS NOT NULL"
        )
        order_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(simulated_orders)").fetchall()
        }
        for name, column_type in {
            "bot_version_id": "INTEGER",
            "strategy_hash": "TEXT",
            "signal_evaluation_id": "INTEGER",
            "proposal_id": "INTEGER",
        }.items():
            if name not in order_columns:
                connection.execute(f"ALTER TABLE simulated_orders ADD COLUMN {name} {column_type}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_simulated_orders_proposal ON simulated_orders(proposal_id) WHERE proposal_id IS NOT NULL"
        )
        validation_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(risk_validation_log)").fetchall()
        }
        for name, column_type in {
            "account_id": "INTEGER",
            "bot_id": "INTEGER",
            "policy_fingerprint": "TEXT",
            "policy_resolution_json": "TEXT",
        }.items():
            if name not in validation_columns:
                connection.execute(f"ALTER TABLE risk_validation_log ADD COLUMN {name} {column_type}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_risk_validation_scope_created ON risk_validation_log(account_id, bot_id, created_at)"
        )
        version_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(bot_versions)").fetchall()
        }
        if "contract_json" not in version_columns:
            connection.execute("ALTER TABLE bot_versions ADD COLUMN contract_json TEXT")
        if "strategy_hash" not in version_columns:
            connection.execute("ALTER TABLE bot_versions ADD COLUMN strategy_hash TEXT")
        if "validation_status" not in version_columns:
            connection.execute("ALTER TABLE bot_versions ADD COLUMN validation_status TEXT NOT NULL DEFAULT 'legacy'")
        legacy_versions = connection.execute(
            """SELECT id, strategy_json FROM bot_versions
               WHERE contract_json IS NULL OR validation_status = 'legacy' OR contract_json NOT LIKE '%paper_proposal%'"""
        ).fetchall()
        for version in legacy_versions:
            try:
                strategy = json.loads(version["strategy_json"] or "{}")
                contract = compile_strategy(strategy)
            except (json.JSONDecodeError, ValueError) as exc:
                contract = {
                    "status": "invalid",
                    "error": str(exc),
                    "capabilities": {"backtest": False, "paper": False, "live": False},
                }
            connection.execute(
                "UPDATE bot_versions SET contract_json = ?, strategy_hash = ?, validation_status = ? WHERE id = ?",
                (json.dumps(contract, ensure_ascii=True), contract.get("strategy_hash"), contract["status"], version["id"]),
            )
        proposal_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(paper_order_proposals)").fetchall()
        }
        for name, column_type in {
            "execution_intent_id": "TEXT",
            "risk_validation_id": "INTEGER",
            "result_reference": "TEXT",
            "submitted_at": "TEXT",
            "strategy_hash": "TEXT",
            "price_timestamp": "TEXT",
            "expires_at": "TEXT",
            "allocation_id": "INTEGER",
            "allocation_revision": "INTEGER",
            "trigger_reason": "TEXT",
            "claim_token": "TEXT",
            "claimed_at": "TEXT",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT",
        }.items():
            if name not in proposal_columns:
                connection.execute(f"ALTER TABLE paper_order_proposals ADD COLUMN {name} {column_type}")
        protection_columns = {row["name"] for row in connection.execute("PRAGMA table_info(paper_position_protections)").fetchall()}
        if protection_columns and "highest_price" not in protection_columns:
            connection.execute("ALTER TABLE paper_position_protections ADD COLUMN highest_price REAL")
        circuit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(paper_bot_circuit_breakers)").fetchall()}
        if "max_drawdown_pct" not in circuit_columns:
            connection.execute("ALTER TABLE paper_bot_circuit_breakers ADD COLUMN max_drawdown_pct REAL NOT NULL DEFAULT 10")
        spot_portfolio_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(spot_portfolios)").fetchall()
        }
        if "active_cycle" not in spot_portfolio_columns:
            connection.execute("ALTER TABLE spot_portfolios ADD COLUMN active_cycle INTEGER NOT NULL DEFAULT 1")
        spot_transaction_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(spot_transactions)").fetchall()
        }
        if "cycle_number" not in spot_transaction_columns:
            connection.execute("ALTER TABLE spot_transactions ADD COLUMN cycle_number INTEGER NOT NULL DEFAULT 1")
        for name, column_type in {
            "origin": "TEXT NOT NULL DEFAULT 'manual'",
            "origin_reference": "TEXT",
            "risk_validation_id": "INTEGER",
        }.items():
            if name not in spot_transaction_columns:
                connection.execute(f"ALTER TABLE spot_transactions ADD COLUMN {name} {column_type}")
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_spot_transactions_origin_reference
            ON spot_transactions(portfolio_id, origin, origin_reference)
            WHERE origin_reference IS NOT NULL"""
        )
        signal_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(strategy_signal_evaluations)").fetchall()
        }
        if "evaluation_key" not in signal_columns:
            connection.execute("ALTER TABLE strategy_signal_evaluations ADD COLUMN evaluation_key TEXT")
        if "conflict" not in signal_columns:
            connection.execute("ALTER TABLE strategy_signal_evaluations ADD COLUMN conflict INTEGER NOT NULL DEFAULT 0 CHECK(conflict IN (0, 1))")
        for name, column_type in {
            "trigger_reason": "TEXT",
            "price_timestamp": "TEXT",
            "position_return_pct": "REAL",
        }.items():
            if name not in signal_columns:
                connection.execute(f"ALTER TABLE strategy_signal_evaluations ADD COLUMN {name} {column_type}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_signals_evaluation_key ON strategy_signal_evaluations(evaluation_key)"
        )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version < 1:
            _backfill_backtest_payloads(connection)
        if user_version < 2:
            _migrate_nullable_profit_factor(connection)
        connection.executescript(BACKTEST_INTEGRITY_TRIGGERS)
        if user_version < BACKTEST_SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {BACKTEST_SCHEMA_VERSION}")
