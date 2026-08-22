import Foundation
import Security

struct KalshiCredentials: Sendable {
    let keyID: String
    let privateKeyPEM: String
}

actor KalshiVenue: PredictionMarketVenue {
    nonisolated let venueID = PredictionMarketVenueID.kalshi

    private let credentials: KalshiCredentials
    private let restBaseURL: URL
    private let webSocketURL: URL
    private let recorder: MarketStateRecorder?
    private let session: URLSession

    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var heartbeatTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var reconnectAttempt = 0
    private var shouldReconnect = false

    private var books: [String: LocalOrderBook] = [:]
    private var orderBookSubscriptions: Set<String> = []
    private var tradeSubscriptions: Set<String> = []
    private var lastSequenceBySID: [Int64: Int64] = [:]
    private var staleStreams: Set<String> = []
    private var nextCommandID: Int64 = 1

    init(
        credentials: KalshiCredentials,
        recorder: MarketStateRecorder? = nil,
        restBaseURL: URL = URL(string: "https://external-api.kalshi.com")!,
        webSocketURL: URL = URL(string: "wss://external-api-ws.kalshi.com/trade-api/ws/v2")!,
        session: URLSession = .shared
    ) {
        self.credentials = credentials
        self.recorder = recorder
        self.restBaseURL = restBaseURL
        self.webSocketURL = webSocketURL
        self.session = session
    }

    func authenticate() async throws {
        _ = try authenticationHeaders(method: "GET", path: "/trade-api/ws/v2")
    }

    func connect() async throws {
        try await authenticate()
        shouldReconnect = true
        reconnectTask?.cancel()
        try await openSocket()
    }

    func close() async {
        shouldReconnect = false
        reconnectTask?.cancel()
        receiveTask?.cancel()
        heartbeatTask?.cancel()
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
    }

    func discoverMarkets(query: String?) async throws -> [NormalizedMarket] {
        var cursor: String?
        var result: [NormalizedMarket] = []
        repeat {
            var items = [
                URLQueryItem(name: "status", value: "open"),
                URLQueryItem(name: "limit", value: "1000")
            ]
            if let cursor { items.append(URLQueryItem(name: "cursor", value: cursor)) }
            let payload = try await VenueTransport.json(
                baseURL: restBaseURL, path: "/trade-api/v2/markets", queryItems: items
            )
            let markets = payload["markets"] as? [[String: Any]] ?? []
            result.append(contentsOf: try markets.map(MarketNormalizer.kalshiMarket))
            cursor = (payload["cursor"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        } while cursor != nil

        guard let query, !query.isEmpty else { return result }
        return result.filter {
            "\($0.title) \($0.contractWording) \($0.tournament ?? "")"
                .localizedCaseInsensitiveContains(query)
        }
    }

    func subscribeOrderBook(_ contracts: [ContractID]) async throws {
        try contracts.forEach { try validate($0) }
        let tickers = Array(Set(contracts.map(\.marketID)))
        for ticker in tickers {
            orderBookSubscriptions.insert(ticker)
            try await bootstrapBook(ticker)
        }
        if socket != nil, !tickers.isEmpty { try await subscribe(channel: "orderbook_delta", markets: tickers) }
    }

    func subscribeTrades(_ contracts: [ContractID]) async throws {
        try contracts.forEach { try validate($0) }
        let tickers = Array(Set(contracts.map(\.marketID)))
        tradeSubscriptions.formUnion(tickers)
        if socket != nil, !tickers.isEmpty { try await subscribe(channel: "trade", markets: tickers) }
    }

    func getBestBidAsk(_ contract: ContractID) throws -> (BookLevel?, BookLevel?) {
        let book = try getOrderBook(contract)
        return (book.bestBid, book.bestAsk)
    }

    func getOrderBook(_ contract: ContractID) throws -> OrderBookSnapshot {
        try validate(contract)
        guard let book = books[contract.marketID] else { throw VenueError.noLocalBook(contract.marketID) }
        return book.snapshot()
    }

    func submitPaperOrder(_ order: PaperOrder) throws -> PaperFill {
        try validate(order.contract)
        return try PaperExecutionEngine.executeIOC(order, against: getOrderBook(order.contract))
    }

    func recordMarketState(_ contract: ContractID) async throws -> OrderBookSnapshot {
        let snapshot = try getOrderBook(contract)
        try await recorder?.record(snapshot: snapshot)
        return snapshot
    }

    private func validate(_ contract: ContractID) throws {
        guard contract.venue == venueID else { throw VenueError.venueMismatch }
    }

    private func openSocket() async throws {
        receiveTask?.cancel()
        heartbeatTask?.cancel()
        lastSequenceBySID.removeAll()
        staleStreams.removeAll()

        var request = URLRequest(url: webSocketURL)
        authenticationHeaders(method: "GET", path: "/trade-api/ws/v2").forEach {
            request.setValue($0.value, forHTTPHeaderField: $0.key)
        }
        let newSocket = session.webSocketTask(with: request)
        socket = newSocket
        newSocket.resume()
        reconnectAttempt = 0
        try await restoreSubscriptions()

        receiveTask = Task { [weak self] in
            await self?.runReceiveLoop(newSocket)
        }
        heartbeatTask = Task { [weak self] in
            await self?.runHeartbeatLoop(newSocket)
        }
    }

    private func runReceiveLoop(_ webSocket: URLSessionWebSocketTask) async {
        do {
            while !Task.isCancelled {
                let message = try await webSocket.receive()
                let received = ReceiveTimestamp.now()
                let data: Data
                switch message {
                case .data(let value): data = value
                case .string(let value): data = Data(value.utf8)
                @unknown default: continue
                }
                if let recorder {
                    try? await recorder.recordRaw(venue: .kalshi, payload: data, received: received)
                }
                try await handleMessage(data, received: received)
            }
        } catch {
            connectionFailed()
        }
    }

    private func runHeartbeatLoop(_ webSocket: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(10))
            do { try await VenueTransport.ping(webSocket) }
            catch { connectionFailed(); return }
        }
    }

    private func connectionFailed() {
        guard shouldReconnect, reconnectTask == nil else { return }
        receiveTask?.cancel()
        heartbeatTask?.cancel()
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        let delay = min(30, 1 << min(reconnectAttempt, 5))
        reconnectAttempt += 1
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self else { return }
            await self.reconnectAfterDelay()
        }
    }

    private func reconnectAfterDelay() async {
        reconnectTask = nil
        do { try await openSocket() }
        catch { connectionFailed() }
    }

    private func restoreSubscriptions() async throws {
        if !orderBookSubscriptions.isEmpty {
            try await subscribe(channel: "orderbook_delta", markets: Array(orderBookSubscriptions))
        }
        if !tradeSubscriptions.isEmpty {
            try await subscribe(channel: "trade", markets: Array(tradeSubscriptions))
        }
    }

    private func subscribe(channel: String, markets: [String]) async throws {
        let command: [String: Any] = [
            "id": nextCommandID,
            "cmd": "subscribe",
            "params": ["channels": [channel], "market_tickers": markets]
        ]
        nextCommandID += 1
        try await sendJSON(command)
    }

    private func requestSnapshot(sid: Int64, market: String) async throws {
        let command: [String: Any] = [
            "id": nextCommandID,
            "cmd": "update_subscription",
            "params": ["sids": [sid], "market_tickers": [market], "action": "get_snapshot"]
        ]
        nextCommandID += 1
        try await sendJSON(command)
    }

    private func sendJSON(_ object: [String: Any]) async throws {
        guard let socket else { return }
        let data = try JSONSerialization.data(withJSONObject: object)
        try await socket.send(.data(data))
    }

    private func bootstrapBook(_ ticker: String) async throws {
        let payload = try await VenueTransport.json(
            baseURL: restBaseURL,
            path: "/trade-api/v2/markets/\(ticker)/orderbook",
            queryItems: [URLQueryItem(name: "depth", value: "0")]
        )
        let levels = try MarketNormalizer.kalshiBook(payload)
        var book = books[ticker] ?? LocalOrderBook(contract: ContractID(venue: .kalshi, marketID: ticker))
        try book.replace(
            bids: levels.bids, asks: levels.asks, state: .open,
            exchangeTimestamp: nil, received: .now(), sequence: nil
        )
        books[ticker] = book
        try await recorder?.record(snapshot: book.snapshot())
    }

    private func handleMessage(_ data: Data, received: ReceiveTimestamp) async throws {
        let root = try JSONSerialization.jsonObject(with: data)
        let messages = (root as? [[String: Any]]) ?? ((root as? [String: Any]).map { [$0] } ?? [])
        for message in messages {
            let messageType = message["type"] as? String
            let body = (message["msg"] as? [String: Any]) ?? [:]
            let sid = (message["sid"] as? NSNumber)?.int64Value ?? 0
            let sequence = (message["seq"] as? NSNumber)?.int64Value

            if messageType == "orderbook_snapshot" {
                guard let ticker = body["market_ticker"] as? String, let sequence else { continue }
                let levels = try MarketNormalizer.kalshiBook(body)
                var book = books[ticker] ?? LocalOrderBook(contract: ContractID(venue: .kalshi, marketID: ticker))
                try book.replace(
                    bids: levels.bids, asks: levels.asks, state: .open,
                    exchangeTimestamp: nil, received: received, sequence: sequence
                )
                lastSequenceBySID[sid] = sequence
                staleStreams.remove(streamKey(sid, ticker))
                books[ticker] = book
                try await recorder?.record(snapshot: book.snapshot())
                continue
            }

            if messageType == "orderbook_delta" {
                guard let ticker = body["market_ticker"] as? String, let sequence else { continue }
                var book = books[ticker] ?? LocalOrderBook(contract: ContractID(venue: .kalshi, marketID: ticker))
                let previous = lastSequenceBySID[sid]
                lastSequenceBySID[sid] = sequence
                let key = streamKey(sid, ticker)
                guard previous != nil, sequence == previous! + 1 else {
                    book.markStale()
                    books[ticker] = book
                    if !staleStreams.contains(key) {
                        staleStreams.insert(key)
                        try await requestSnapshot(sid: sid, market: ticker)
                    }
                    continue
                }
                guard !staleStreams.contains(key) else { continue }
                let rawPrice = MarketNormalizer.decimal(body["price_dollars"])
                guard let rawSide = (body["side"] as? String)?.lowercased(),
                      rawSide == "yes" || rawSide == "no" else {
                    throw VenueError.invalidPayload("Kalshi delta has invalid side")
                }
                let isYes = rawSide == "yes"
                let exchangeDate = (body["ts_ms"] as? NSNumber).map {
                    Date(timeIntervalSince1970: $0.doubleValue / 1_000)
                }
                try book.applyDelta(
                    side: isYes ? .buy : .sell,
                    price: isYes ? rawPrice : 1 - rawPrice,
                    quantityDelta: MarketNormalizer.decimal(body["delta_fp"]),
                    sequence: sequence,
                    exchangeTimestamp: exchangeDate,
                    received: received
                )
                books[ticker] = book
                try await recorder?.record(snapshot: book.snapshot())
                continue
            }

            if messageType == "trade", let ticker = body["market_ticker"] as? String {
                let exchangeDate = (body["ts_ms"] as? NSNumber).map {
                    Date(timeIntervalSince1970: $0.doubleValue / 1_000)
                }
                let trade = NormalizedTrade(
                    contract: ContractID(venue: .kalshi, marketID: ticker),
                    price: MarketNormalizer.decimal(body["yes_price_dollars"]),
                    quantity: MarketNormalizer.decimal(body["count_fp"]),
                    exchangeTimestamp: exchangeDate,
                    received: received,
                    tradeID: body["trade_id"] as? String
                )
                if var book = books[ticker] {
                    book.updateTrade(trade)
                    books[ticker] = book
                    try await recorder?.record(snapshot: book.snapshot())
                }
            }
        }
    }

    private func streamKey(_ sid: Int64, _ market: String) -> String { "\(sid):\(market)" }

    private func authenticationHeaders(method: String, path: String) throws -> [String: String] {
        guard !credentials.keyID.isEmpty, !credentials.privateKeyPEM.isEmpty else {
            throw VenueError.credentialsMissing("Kalshi key ID and RSA private key are required")
        }
        let timestamp = String(Int64(Date().timeIntervalSince1970 * 1_000))
        let signedPath = path.components(separatedBy: "?").first ?? path
        let message = Data("\(timestamp)\(method.uppercased())\(signedPath)".utf8)
        let key = try Self.rsaPrivateKey(from: credentials.privateKeyPEM)
        var error: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(
            key, .rsaSignatureMessagePSSSHA256, message as CFData, &error
        ) as Data? else {
            let detail = error.map { ($0.takeRetainedValue() as Error).localizedDescription }
            throw VenueError.authenticationFailed(detail ?? "RSA-PSS signing failed")
        }
        return [
            "KALSHI-ACCESS-KEY": credentials.keyID,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature.base64EncodedString()
        ]
    }

    private static func rsaPrivateKey(from pem: String) throws -> SecKey {
        let isPKCS8 = pem.contains("BEGIN PRIVATE KEY") && !pem.contains("BEGIN RSA PRIVATE KEY")
        let base64 = pem.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.hasPrefix("---") }
            .joined()
        guard var data = Data(base64Encoded: base64, options: .ignoreUnknownCharacters) else {
            throw VenueError.authenticationFailed("Invalid Kalshi PEM")
        }
        if isPKCS8 { data = try extractPKCS1(fromPKCS8: data) }
        let attributes: [CFString: Any] = [
            kSecAttrKeyType: kSecAttrKeyTypeRSA,
            kSecAttrKeyClass: kSecAttrKeyClassPrivate
        ]
        var error: Unmanaged<CFError>?
        guard let key = SecKeyCreateWithData(data as CFData, attributes as CFDictionary, &error) else {
            let detail = error.map { ($0.takeRetainedValue() as Error).localizedDescription }
            throw VenueError.authenticationFailed(detail ?? "Invalid RSA key")
        }
        return key
    }

    private static func extractPKCS1(fromPKCS8 data: Data) throws -> Data {
        var index = 0
        func element(_ expectedTag: UInt8) throws -> Data {
            guard index < data.count, data[index] == expectedTag else {
                throw VenueError.authenticationFailed("Malformed PKCS#8 key")
            }
            index += 1
            guard index < data.count else { throw VenueError.authenticationFailed("Malformed PKCS#8 length") }
            var length = Int(data[index]); index += 1
            if length & 0x80 != 0 {
                let count = length & 0x7F
                length = 0
                for _ in 0..<count {
                    guard index < data.count else { throw VenueError.authenticationFailed("Malformed PKCS#8 length") }
                    length = (length << 8) | Int(data[index]); index += 1
                }
            }
            guard index + length <= data.count else { throw VenueError.authenticationFailed("Truncated PKCS#8 key") }
            let value = data[index..<(index + length)]
            index += length
            return Data(value)
        }
        let outer = try element(0x30)
        index = data.count - outer.count
        _ = try element(0x02)
        _ = try element(0x30)
        return try element(0x04)
    }
}
