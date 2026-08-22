# Verified market API contracts

Verified against current official documentation on **2026-08-22**. These notes deliberately separate the retail Polymarket US API from both the institutional Exchange API and the international Polymarket CLOB.

## Polymarket US retail API used here

### Discovery and order books

- Public base: `https://gateway.polymarket.us`
- Markets: `GET /v1/markets`
- Full book: `GET /v1/markets/{slug}/book`
- BBO: `GET /v1/markets/{slug}/bbo`
- No authentication is required for these public reads.

The book response is `marketData` with `marketSlug`, `bids`, `offers`, `state`, `stats`, and `transactTime`. Price is `px.value` in USD probability units and quantity may be fractional.

Official sources:

- [Polymarket US API introduction](https://docs.polymarket.us/api-reference/introduction)
- [Get markets](https://docs.polymarket.us/api-reference/markets/get-markets)
- [Get market book](https://docs.polymarket.us/api-reference/markets/get-market-book)
- [Get market BBO](https://docs.polymarket.us/api-reference/markets/get-market-bbo)

### Authentication

Authenticated base: `https://api.polymarket.us`.

Handshake/request headers:

```text
X-PM-Access-Key: key ID
X-PM-Timestamp: Unix milliseconds
X-PM-Signature: base64 Ed25519 signature
```

Signature payload: `timestamp + uppercase HTTP method + path`. The developer secret is base64; the official example constructs an Ed25519 private key from its first 32 decoded bytes. Timestamps must be within 30 seconds of server time.

Official source: [Polymarket US authentication](https://docs.polymarket.us/api-reference/authentication).

### WebSocket

- URL: `wss://api.polymarket.us/v1/ws/markets`
- Handshake signature path: `/v1/ws/markets`
- Full book type: `SUBSCRIPTION_TYPE_MARKET_DATA`
- Trade type: `SUBSCRIPTION_TYPE_TRADE`
- Current official SDK envelope fields are camelCase: `requestId`, `subscriptionType`, `marketSlugs`, `marketData`, and `marketDataLite`.
- Maximum 100 market slugs per subscription.
- Full market-data messages contain the complete published depth. Each message replaces local state.
- JSON heartbeat shape: `{"heartbeat": {}}`. The client also uses WebSocket ping/pong and reconnects on feed silence.
- Messages are documented as ordered, but the retail market message schema documents no sequence number or resume token. Recovery therefore uses stale detection, reconnect, REST snapshot, and resubscribe—not a guessed sequence algorithm.

The overview page also shows a numeric/snake_case wire example, while the dedicated markets page and the official current Python SDK use enum strings/camelCase. This implementation follows the dedicated page and official SDK and accepts response payloads by their documented camelCase keys.

Official sources:

- [WebSocket overview and heartbeats](https://docs.polymarket.us/api-reference/websocket/overview)
- [Markets WebSocket schemas](https://docs.polymarket.us/api-reference/websocket/markets)
- [Official Polymarket US Python SDK](https://github.com/Polymarket/polymarket-us-python)

### Order entry (verified, intentionally not called)

The retail US live order endpoint is `POST https://api.polymarket.us/v1/orders`. It uses the same signed headers. Relevant fields include `marketSlug`, `intent`, `type`, `price`, `quantity`, and `tif`; IOC is `TIME_IN_FORCE_IMMEDIATE_OR_CANCEL`. This v1 implementation does **not** issue this request. Its common venue method is local paper execution only.

Official sources:

- [Create order](https://docs.polymarket.us/api-reference/orders/create-order)
- [Orders overview](https://docs.polymarket.us/api-reference/orders/overview)

### APIs explicitly not used

- No international `clob.polymarket.com`, token IDs, EVM keys, CLOB HMAC, or CTF order schema.
- No institutional `api.prod.polymarketexchange.com`, Auth0 bearer tokens, gRPC, FIX, or participant IDs.

## Kalshi API used here

### Discovery and books

- REST base: `https://external-api.kalshi.com`
- Markets: `GET /trade-api/v2/markets`
- Book: `GET /trade-api/v2/markets/{ticker}/orderbook`
- Trades: `GET /trade-api/v2/markets/trades`

The fixed-point book contains YES bids and NO bids. A NO bid at `p` is normalized as a YES ask at `1 - p`, with the same quantity.

Official sources:

- [Get markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Get market order book](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Order-book interpretation](https://docs.kalshi.com/getting_started/orderbook_responses)

### Authentication and WebSocket

- WebSocket: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- Signature: RSA-PSS/SHA-256 over `timestamp + uppercase method + path_without_query`
- Handshake signature path: `/trade-api/ws/v2`
- Book channel: `orderbook_delta`
- Trade channel: `trade`
- Kalshi sends ping control frames about every 10 seconds; clients answer with pong (handled by URLSession/websockets) and send their own pings.

Official sources:

- [Kalshi API key signing](https://docs.kalshi.com/getting_started/api_keys)
- [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Connection keep-alive](https://docs.kalshi.com/websockets/connection-keep-alive)
- [Public trades](https://docs.kalshi.com/websockets/public-trades)

### Sequence handling

The `orderbook_delta` subscription sends `orderbook_snapshot` first and then `orderbook_delta`. Both carry `sid` and `seq`; deltas carry `price_dollars`, signed `delta_fp`, `side`, `ts`, and `ts_ms`.

Sequence is tracked per subscription ID. On a gap, the affected market is marked stale, its deltas are ignored, and `update_subscription` with action `get_snapshot` is sent. Paper execution stays disabled until the replacement snapshot arrives.

Official source: [Kalshi order-book updates](https://docs.kalshi.com/websockets/orderbook-updates).

