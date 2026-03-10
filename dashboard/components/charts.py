"""Chart/visualization helpers using Plotly."""

import pandas as pd
import plotly.graph_objects as go


def pnl_line_chart(df: pd.DataFrame) -> go.Figure:
    """Return a Plotly line chart of cumulative PnL over time.

    Expects *df* to have columns: executed_at, cumulative_pnl
    """
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["executed_at"],
            y=df["cumulative_pnl"],
            mode="lines+markers",
            name="Cumulative PnL",
            line={"color": "royalblue", "width": 2},
            marker={"size": 5},
            hovertemplate="%{x}<br>PnL: $%{y:.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        title="Cumulative PnL",
        xaxis_title="Time",
        yaxis_title="PnL (USD)",
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        yaxis={"gridcolor": "#eee"},
        xaxis={"gridcolor": "#eee"},
    )
    return fig


def trade_status_bar_chart(df: pd.DataFrame) -> go.Figure:
    """Return a Plotly bar chart showing trade counts per status.

    Expects *df* to have a 'status' column.
    """
    counts = df["status"].value_counts().reset_index()
    counts.columns = ["status", "count"]
    color_map = {
        "success": "green",
        "dry_run": "royalblue",
        "failed": "red",
        "slippage_exceeded": "gold",
        "below_threshold": "lightgray",
        "position_limit": "orange",
    }
    colors = [color_map.get(s, "gray") for s in counts["status"]]
    fig = go.Figure(
        go.Bar(
            x=counts["status"],
            y=counts["count"],
            marker_color=colors,
            hovertemplate="%{x}: %{y}<extra></extra>",
        )
    )
    fig.update_layout(
        title="Trades by Status",
        xaxis_title="Status",
        yaxis_title="Count",
        plot_bgcolor="white",
        paper_bgcolor="white",
        yaxis={"gridcolor": "#eee"},
    )
    return fig
