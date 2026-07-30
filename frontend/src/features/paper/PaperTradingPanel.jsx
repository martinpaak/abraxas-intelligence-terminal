import React, { useEffect, useState } from "react";
import { getPaperAccount, placePaperOrder, resetPaperAccount, setPaperBotRuntime } from "../../api/client.js";

const money = (value) => Number(value || 0).toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const when = (value) => value ? new Date(value).toLocaleString() : "--";

export default function PaperTradingPanel({ defaultSymbol = "BTCUSDT" }) {
  const [activeTab, setActiveTab] = useState("account");
  const [snapshot, setSnapshot] = useState(null);
  const [order, setOrder] = useState({ symbol: defaultSymbol, side: "buy", quantity: "0.001" });
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    try { setSnapshot(await getPaperAccount()); setError(""); }
    catch (requestError) { setError(requestError.message); }
  };
  useEffect(() => { load(); }, []);
  useEffect(() => { setOrder((current) => ({ ...current, symbol: defaultSymbol })); }, [defaultSymbol]);

  const submit = async (event) => {
    event.preventDefault(); setBusy(true);
    try {
      const response = await placePaperOrder({ ...order, symbol: order.symbol.toUpperCase(), quantity: Number(order.quantity), bot_id: null });
      setResult(response); await load();
    } catch (requestError) { setError(requestError.message); }
    finally { setBusy(false); }
  };
  const reset = async () => {
    if (!window.confirm("Resetear cuenta paper a USD 10,000? El ledger histórico se conserva.")) return;
    setBusy(true);
    try { setSnapshot(await resetPaperAccount({ initial_balance: 10000, reason: "Manual reset from Paper Desk" })); setResult(null); setError(""); }
    catch (requestError) { setError(requestError.message); }
    finally { setBusy(false); }
  };
  const changeBotRuntime = async (bot) => {
    const pause = bot.runtime?.status !== "paused";
    const reason = window.prompt(pause ? `Motivo para pausar ${bot.name} durante 24 horas:` : `Motivo para reanudar ${bot.name}:`);
    if (!reason?.trim()) return;
    setBusy(true);
    try {
      await setPaperBotRuntime(bot.id, { status: pause ? "paused" : "active", reason: reason.trim(), pause_minutes: pause ? 1440 : null });
      await load();
    } catch (requestError) { setError(requestError.message); }
    finally { setBusy(false); }
  };

  if (!snapshot) return <section className="exchange-panel paper-panel"><div className="chart-state">{error || "Cargando cuenta paper desde SQLite..."}</div></section>;
  const protections = snapshot.protections || {};
  return <>
    {error && <div className="error-box">{error}</div>}
    <section className="paper-runtime-strip">
      <article><span>Mode</span><strong>PAPER</strong><small>live {snapshot.live_execution}</small></article>
      <article><span>Risk gate</span><strong>{protections.risk_required ? "REQUIRED" : "SIN DATO"}</strong><small>kill {protections.kill_switch_active ? "active" : "clear"}</small></article>
      <article><span>Policy hierarchy</span><strong>{protections.account_policy ? `ACCOUNT V${protections.account_policy.current_version}` : "GLOBAL"}</strong><small>{protections.bot_policy_count || 0} bot policies · restrictive</small></article>
      <article><span>Price freshness</span><strong>{protections.price_max_age_seconds ?? "--"}s</strong><small>máximo permitido</small></article>
      <article><span>Proposal TTL</span><strong>{protections.proposal_ttl_seconds ?? "--"}s</strong><small>drift {protections.max_price_drift_pct ?? "--"}%</small></article>
    </section>
    <nav className="paper-subtabs" aria-label="Paper Trading sections">
      {[["account", "Cuenta"], ["activity", "Actividad"], ["bots", "Bots"]].map(([value, label]) => <button type="button" key={value} className={activeTab === value ? "active" : ""} onClick={() => setActiveTab(value)}>{label}</button>)}
    </nav>

    {activeTab === "account" && <>
      <section className="paper-account-strip">
        {[["Equity", money(snapshot.equity)], ["Cash", money(snapshot.account.cash_balance)], ["Market value", money(snapshot.market_value)], ["PnL realizado", money(snapshot.account.realized_pnl)], ["PnL diario", money(snapshot.daily_realized_pnl)], ["Drawdown", `${Number(snapshot.drawdown_pct).toFixed(2)}%`]].map(([label, value]) => <article key={label}><span>{label}</span><strong>{value}</strong></article>)}
      </section>
      <section className="paper-workspace">
        <form className="exchange-panel paper-order-ticket" onSubmit={submit}>
          <div className="exchange-panel-head compact"><div><p className="eyebrow">Manual paper desk</p><h2>Market ticket</h2></div><span>UNATTRIBUTED</span></div>
          <label>Symbol<input value={order.symbol} onChange={(event) => setOrder((current) => ({ ...current, symbol: event.target.value.toUpperCase() }))} /></label>
          <label>Side<select value={order.side} onChange={(event) => setOrder((current) => ({ ...current, side: event.target.value }))}><option value="buy">Buy</option><option value="sell">Sell</option></select></label>
          <label>Quantity<input type="number" min="0" step="0.000001" value={order.quantity} onChange={(event) => setOrder((current) => ({ ...current, quantity: event.target.value }))} /></label>
          <button type="submit" disabled={busy || Number(order.quantity) <= 0}>{busy ? "Validando..." : "Enviar a Risk Engine"}</button>
          <small>Operación manual sin atribución a bots. Precio real persistido; fill únicamente simulado.</small>
          {result && <div className={`paper-result ${result.status}`}><strong>{result.status.toUpperCase()}</strong><span>{result.reason || `Order #${result.order_id} · fill #${result.fill_id}`}</span></div>}
        </form>
        <section className="exchange-panel paper-positions">
          <div className="exchange-panel-head compact"><div><p className="eyebrow">Portfolio</p><h2>Posiciones agregadas</h2></div><button type="button" onClick={load}>Refrescar</button></div>
          {snapshot.positions.length ? snapshot.positions.map((position) => <article key={position.symbol}><div><strong>{position.symbol}</strong><span>{position.quantity} units</span></div><div><span>Avg {money(position.average_price)}</span><b className={position.unrealized_pnl >= 0 ? "positive" : "negative"}>{money(position.unrealized_pnl)}</b></div></article>) : <div className="chart-state">Sin posiciones abiertas.</div>}
          {!!snapshot.allocations?.length && <div className="allocation-list">{snapshot.allocations.filter((item) => Number(item.quantity) > 0).map((item) => <small key={item.id}>#{item.id} · {item.owner_key} · {item.quantity} @ {money(item.average_price)} · rev {item.revision}</small>)}</div>}
        </section>
      </section>
      <button type="button" className="paper-reset-button secondary" onClick={reset} disabled={busy}>Reset account</button>
    </>}

    {activeTab === "activity" && <section className="paper-activity-stack">
      <AuditTable title="Propuestas" columns={["id", "bot_id", "bot_version_id", "signal_evaluation_id", "action", "quantity", "status", "risk_validation_id", "result_reference"]} rows={snapshot.proposals || []} />
      <AuditTable title="Execution intents" columns={["id", "bot_id", "bot_version_id", "signal_evaluation_id", "proposal_id", "action", "status", "risk_validation_id", "result_reference"]} rows={snapshot.execution_intents || []} />
      <AuditTable title="Órdenes" columns={["id", "bot_id", "bot_version_id", "proposal_id", "symbol", "side", "quantity", "status", "fee", "rejection_reason"]} rows={snapshot.orders || []} />
      <AuditTable title="Fills" columns={["id", "order_id", "symbol", "side", "quantity", "price", "fee", "filled_at"]} rows={snapshot.fills || []} />
      <AuditTable title="Ledger" columns={["id", "event_type", "reference_id", "symbol", "cash_delta", "realized_pnl_delta", "cash_balance", "created_at"]} rows={snapshot.ledger || []} />
      <AuditTable title="Bot runtime events" columns={["id", "bot_id", "event_type", "previous_status", "new_status", "reason", "paused_until", "created_at"]} rows={snapshot.bot_runtime_events || []} />
    </section>}

    {activeTab === "bots" && <section className="exchange-panel paper-bot-performance">
      <div className="exchange-panel-head compact"><div><p className="eyebrow">Bot ROI profiles</p><h2>Rendimiento atribuido por bot</h2></div><span>FILLS + MARK TO MARKET</span></div>
      <div className="paper-bot-grid">{snapshot.bot_performance.map((bot) => <article key={bot.id} className={`${bot.paper_status} risk-${bot.risk_budget?.status || "clear"}`}>
        <div className="paper-bot-head"><div><span>BOT #{bot.id}</span><strong>{bot.name}</strong></div><b className={bot.roi_pct >= 0 ? "positive" : "negative"}>{bot.roi_pct >= 0 ? "+" : ""}{Number(bot.roi_pct).toFixed(2)}%</b></div>
        <div className={`paper-bot-runtime ${bot.runtime?.status || "active"}`}><div><small>Runtime</small><strong>{bot.runtime?.status?.toUpperCase() || "ACTIVE"}</strong></div><button type="button" className="secondary" disabled={busy} onClick={() => changeBotRuntime(bot)}>{bot.runtime?.status === "paused" ? "Reanudar" : "Pausar 24h"}</button><small>{bot.runtime?.reason || "Sin override operativo"}{bot.runtime?.paused_until ? ` · hasta ${when(bot.runtime.paused_until)}` : ""}</small></div>
        <div className="paper-bot-metrics"><span><small>PnL</small><strong>{money(bot.pnl)}</strong></span><span><small>Capital</small><strong>{money(bot.deployed_capital)}</strong></span><span><small>Fills</small><strong>{bot.filled_orders}</strong></span><span><small>Rechazos</small><strong>{bot.rejected_orders}</strong></span></div>
        <div className={`paper-bot-risk ${bot.risk_budget?.status || "clear"}`}>
          <div><span>Presupuesto de pérdida diario</span><strong>{bot.risk_budget?.status?.toUpperCase() || "CLEAR"}</strong></div>
          <div className="paper-bot-risk-track" aria-label={`${Number(bot.risk_budget?.daily_loss_usage_pct || 0).toFixed(1)}% del presupuesto diario consumido`}><i style={{ width: `${Math.min(100, Number(bot.risk_budget?.daily_loss_usage_pct || 0))}%` }} /></div>
          <div><small>Usado {money(bot.risk_budget?.daily_loss_used)} / {money(bot.risk_budget?.daily_loss_budget)}</small><small>Disponible {money(bot.risk_budget?.daily_loss_remaining)}</small></div>
          <div><span>Capital abierto del bot</span><strong>{Number(bot.risk_budget?.capital_usage_pct || 0).toFixed(1)}%</strong></div>
          <div className="paper-bot-risk-track capital" aria-label={`${Number(bot.risk_budget?.capital_usage_pct || 0).toFixed(1)}% del capital del bot consumido`}><i style={{ width: `${Math.min(100, Number(bot.risk_budget?.capital_usage_pct || 0))}%` }} /></div>
          <div><small>Abierto {money(bot.risk_budget?.capital_used)} / {money(bot.risk_budget?.capital_budget)}</small><small>Disponible {money(bot.risk_budget?.capital_remaining)}</small></div>
          <small>loss {Number(bot.risk_budget?.daily_loss_limit_pct || 0).toFixed(2)}% · capital {Number(bot.risk_budget?.capital_limit_pct || 0).toFixed(2)}% · policy {bot.risk_budget?.policy_scope || "global"} · {String(bot.risk_budget?.policy_fingerprint || "").slice(0, 8)}</small>
        </div>
        <p>{bot.base_symbol} / {bot.timeframe} / {bot.risk_profile}</p><time>{bot.started_at ? `Desde ${when(bot.started_at)}` : "Sin actividad paper"}</time>
      </article>)}</div>
    </section>}
  </>;
}

function AuditTable({ title, columns, rows }) {
  return <section className="exchange-panel paper-audit-table"><div className="exchange-panel-head compact"><h3>{title}</h3><span>{rows.length} ROWS</span></div><div className="backtest-trades-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={row.id || index}>{columns.map((column) => <td key={column} title={String(row[column] ?? "")}>{String(row[column] ?? "--")}</td>)}</tr>)}</tbody></table>{!rows.length && <div className="chart-state">Sin registros.</div>}</div></section>;
}
