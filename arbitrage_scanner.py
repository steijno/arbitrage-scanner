"""
Polymarket x Kalshi arbitrage scanner (read-only)
Run:
    pip install streamlit requests pandas
    streamlit run arbitrage_scanner.py

Important:
- Add only markets whose resolution rules, cutoff times and outcomes you have
  manually verified are equivalent.
- This tool only reads public market data. It never places orders.
- Fee inputs are user-configurable estimates; verify current fees on each venue.
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

POLY_CLOB = "https://clob.polymarket.com"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
TIMEOUT = 12


def get_json(url: str, params: Optional[dict] = None) -> dict:
    r = requests.get(url, params=params, timeout=TIMEOUT,
                     headers={"User-Agent": "read-only-arbitrage-monitor/1.0"})
    r.raise_for_status()
    return r.json()


def poly_book(token_id: str) -> dict:
    return get_json(f"{POLY_CLOB}/book", {"token_id": token_id})


def kalshi_book(ticker: str) -> dict:
    return get_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook", {"depth": 10})


def best_poly_ask(book: dict) -> Tuple[Optional[float], float]:
    asks = book.get("asks") or []
    parsed = []
    for level in asks:
        try:
            parsed.append((float(level["price"]), float(level["size"])))
        except (KeyError, TypeError, ValueError):
            pass
    if not parsed:
        return None, 0.0
    price = min(p for p, _ in parsed)
    size = sum(s for p, s in parsed if p == price)
    return price, size


def kalshi_bid_levels(book: dict, side: str) -> List[Tuple[float, float]]:
    """Kalshi orderbook bids: [[price_cents, quantity], ...]."""
    ob = book.get("orderbook") or book
    raw = ob.get(side) or ob.get(f"{side}_dollars") or []
    levels = []
    for level in raw:
        try:
            if isinstance(level, dict):
                p = float(level.get("price_dollars", level.get("price")))
                q = float(level.get("quantity", level.get("count", level.get("size"))))
                if p > 1:
                    p /= 100
            else:
                p, q = float(level[0]), float(level[1])
                if p > 1:
                    p /= 100
            levels.append((p, q))
        except (TypeError, ValueError, IndexError):
            continue
    return levels


def best_kalshi_ask(book: dict, contract_side: str) -> Tuple[Optional[float], float]:
    # A NO bid at x is equivalent to a YES ask at 1-x, and vice versa.
    opposite = "no" if contract_side.lower() == "yes" else "yes"
    bids = kalshi_bid_levels(book, opposite)
    if not bids:
        return None, 0.0
    ask = min(1.0 - p for p, _ in bids)
    size = sum(q for p, q in bids if abs((1.0 - p) - ask) < 1e-9)
    return ask, size


def fee_adjusted_cost(price: float, fee_pct: float) -> float:
    # Approximation: fee_pct is a percentage of the contract purchase cost.
    return price * (1.0 + fee_pct / 100.0)


def scan_pair(pair: dict, poly_fee: float, kalshi_fee: float) -> List[dict]:
    pb = poly_book(str(pair["polymarket_yes_token_id"]))
    kb = kalshi_book(str(pair["kalshi_ticker"]))

    p_yes, p_yes_qty = best_poly_ask(pb)
    k_yes, k_yes_qty = best_kalshi_ask(kb, "yes")
    k_no, k_no_qty = best_kalshi_ask(kb, "no")

    results = []
    # Buy Poly YES + Kalshi NO
    if p_yes is not None and k_no is not None:
        cost = fee_adjusted_cost(p_yes, poly_fee) + fee_adjusted_cost(k_no, kalshi_fee)
        results.append({
            "Event": pair.get("name", pair["kalshi_ticker"]),
            "Direction": "Poly YES + Kalshi NO",
            "Poly YES ask": p_yes,
            "Kalshi NO ask": k_no,
            "Combined cost": cost,
            "Gross edge": 1.0 - p_yes - k_no,
            "Estimated net edge": 1.0 - cost,
            "Max matched qty (top level)": min(p_yes_qty, k_no_qty),
            "Rules verified manually": bool(pair.get("rules_verified", False)),
        })

    # Buy Kalshi YES + Poly NO. Poly NO token ID is optional in mapping.
    poly_no_id = pair.get("polymarket_no_token_id")
    if poly_no_id:
        pn_book = poly_book(str(poly_no_id))
        p_no, p_no_qty = best_poly_ask(pn_book)
        if p_no is not None and k_yes is not None:
            cost = fee_adjusted_cost(k_yes, kalshi_fee) + fee_adjusted_cost(p_no, poly_fee)
            results.append({
                "Event": pair.get("name", pair["kalshi_ticker"]),
                "Direction": "Kalshi YES + Poly NO",
                "Poly NO ask": p_no,
                "Kalshi YES ask": k_yes,
                "Combined cost": cost,
                "Gross edge": 1.0 - p_no - k_yes,
                "Estimated net edge": 1.0 - cost,
                "Max matched qty (top level)": min(p_no_qty, k_yes_qty),
                "Rules verified manually": bool(pair.get("rules_verified", False)),
            })
    return results


st.set_page_config(page_title="Prediction Market Arbitrage Scanner", layout="wide")
st.title("Polymarket × Kalshi Arbitrage Scanner")
st.caption("Read-only monitor · live public order-book snapshots · no order execution")

with st.expander("1. Market pairs (manual matching is required)", expanded=True):
    st.write(
        "Upload a JSON file containing equivalent markets. Matching is deliberately "
        "manual: similar titles do not guarantee identical settlement rules."
    )
    st.code(
        json.dumps(
            [{
                "name": "Example: same event and exact settlement",
                "polymarket_yes_token_id": "PASTE_POLYMARKET_YES_TOKEN_ID",
                "polymarket_no_token_id": "PASTE_POLYMARKET_NO_TOKEN_ID",
                "kalshi_ticker": "PASTE_KALSHI_MARKET_TICKER",
                "rules_verified": False
            }],
            indent=2
        ),
        language="json"
    )
    uploaded = st.file_uploader("Upload market_pairs.json", type=["json"])

with st.sidebar:
    st.header("Scanner settings")
    poly_fee = st.number_input("Polymarket estimated fee (%)", 0.0, 20.0, 0.0, 0.05)
    kalshi_fee = st.number_input("Kalshi estimated fee (%)", 0.0, 20.0, 0.0, 0.05)
    min_edge = st.number_input("Minimum estimated net edge (%)", 0.0, 50.0, 0.5, 0.1)
    refresh_seconds = st.selectbox("Refresh interval", [5, 10, 15, 30, 60], index=2)
    auto_refresh = st.checkbox("Auto-refresh", value=False)
    st.caption("Fees are estimates. Enter your actual applicable fee assumptions.")

if uploaded:
    try:
        pairs = json.load(uploaded)
        if not isinstance(pairs, list):
            raise ValueError("JSON root must be a list.")
        if not pairs:
            st.warning("The uploaded pair list is empty.")
        else:
            if st.button("Scan now", type="primary"):
                st.session_state["run_scan"] = True

            if st.session_state.get("run_scan") or auto_refresh:
                rows = []
                errors = []
                progress = st.progress(0)
                for i, pair in enumerate(pairs):
                    try:
                        rows.extend(scan_pair(pair, poly_fee, kalshi_fee))
                    except Exception as exc:
                        errors.append(f'{pair.get("name", pair.get("kalshi_ticker", i))}: {exc}')
                    progress.progress((i + 1) / len(pairs))

                if rows:
                    df = pd.DataFrame(rows)
                    df["Estimated net edge (%)"] = df["Estimated net edge"] * 100
                    df["Gross edge (%)"] = df["Gross edge"] * 100
                    df["Combined cost"] = df["Combined cost"].map(lambda x: f"${x:.4f}")
                    df["Estimated net edge (%)"] = df["Estimated net edge (%)"].map(lambda x: f"{x:.2f}%")
                    df["Gross edge (%)"] = df["Gross edge (%)"].map(lambda x: f"{x:.2f}%")
                    df["Max matched qty (top level)"] = df["Max matched qty (top level)"].map(lambda x: f"{x:.2f}")
                    df["Candidate"] = df["Estimated net edge (%)"].str.rstrip("%").astype(float) >= min_edge
                    st.subheader("Results")
                    st.dataframe(df.sort_values("Candidate", ascending=False), use_container_width=True, hide_index=True)
                    st.metric("Potential candidates", int(df["Candidate"].sum()))
                    st.warning(
                        "A positive displayed edge is only a signal, not a guaranteed profit. "
                        "Re-check both books immediately before any decision; prices can move "
                        "between requests, and the displayed top-level size may not be fillable."
                    )
                else:
                    st.info("No comparable quotes returned. Check token IDs, ticker and API availability.")
                if errors:
                    with st.expander(f"Data errors ({len(errors)})"):
                        for err in errors:
                            st.write(f"- {err}")
                if auto_refresh:
                    time.sleep(refresh_seconds)
                    st.rerun()
    except Exception as exc:
        st.error(f"Could not read JSON: {exc}")
else:
    st.info("Upload a market_pairs.json file to start scanning.")
    st.write("The first version uses manually verified market pairs so it does not compare unrelated contracts.")
