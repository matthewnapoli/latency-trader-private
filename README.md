# Latency Trader

A local desktop app for easily submitting all-or-none limit orders at the current best bid or best ask on **Kalshi** and **Polymarket US**.

Choose an exchange, enter its API credentials, select a market and submarket, choose YES or NO, enter a quantity, and review a buy or sell order.

- **Buy:** use the latest best ask as the limit price.
- **Sell:** use the latest best bid as the limit price.
- **All or none:** submit a native **fill-or-kill (FOK)** limit order. The exchange must fill the entire quantity immediately at the limit or better, or cancel it. This is not a resting AON order.

## Run the app

Requires Python 3.10+ with Tkinter (included in the standard Windows Python installer).

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
# source .venv/bin/activate
python -m pip install -e ".[dev]"
latency-trader-app
```

Alternatively, after installing: `python -m latency_trader.app`.

On Windows, you can also double-click **Start App.cmd** after setup.

## Use

1. Select **Kalshi** or **Polymarket US**.
2. Enter your API key ID and signing key: select an RSA PEM file for Kalshi, or paste the base64 API secret for Polymarket US. Credentials are held in process memory and are never saved by the app. Switching exchanges clears them.
3. Click **Load markets**. Filter the loaded events and use **Load more** for additional pages. Select an event on the left and an open submarket on the right. You can also paste an exact Kalshi market ticker or Polymarket US market slug.
4. Select **YES / NO** and a whole-number contract quantity.
5. Click **Review BUY AON at best ask** or **Review SELL AON at best bid**. The app fetches fresh market metadata and order-book prices, then displays the exact contract, direction, limit price, and notional before fees.
6. Click **Submit live FOK order** within five seconds. Expired quotes require a new review. Submission never silently changes the reviewed price.

These are live orders when you press Submit. Browsing markets and reading quotes do not place orders or require credentials. Quotes can change before arrival at the exchange; FOK does not guarantee a fill. Available balance, positions, fees, trading permissions, market increments, and exchange rules still apply. Kalshi uses a net YES position, so an order can change or reverse that position; this app does not enforce position-only sells.

Results show the exchange-reported order ID, state, and filled quantity. An acknowledgment alone is not displayed as a fill. Requests are never automatically retried. An uncertain submission blocks further orders for the session: check the exchange's order history before restarting the app.

## Current scope

- Local Python/Tkinter desktop app with manual order entry.
- Kalshi event markets and the retail Polymarket **US** API; not the international Polymarket CLOB.
- Event/submarket discovery, YES/NO prices, native FOK limit buys and sells.
- Whole-number quantities; no paired arbitrage execution, continuous auto-trading, or credential storage.
- API contract tests and public market-data smoke checks. Live order placement has **not** been tested with a funded account.

The existing `ios/` and market replay/analysis modules are earlier research components. They are not the desktop app and do not provide a finished iPhone trading app.

## Possible future versions

Future versions could add computer vision on top of the manual trading app, for example recognizing live sports events and helping surface relevant markets or draft an order ticket. Computer vision is not part of the current app, and no camera signal submits orders.

## Development

```bash
python -m pytest
```

The desktop UI is in `python/latency_trader/app.py`; quote handling and live FOK submission are in `python/latency_trader/trading.py`. Existing read-only feed adapters and research utilities remain in `python/latency_trader/markets/`, `ios/`, and the replay/analysis modules.

API references: [Kalshi order entry](https://docs.kalshi.com/api-reference/orders/create-order-v2), [Polymarket US order entry](https://docs.polymarket.us/api-reference/orders/create-order), and [Polymarket US price conventions](https://docs.polymarket.us/api-reference/orders/overview).
