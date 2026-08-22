import CryptoKit
import Foundation

struct PolymarketUSCredentials: Sendable {
    let keyID: String
    let secretKeyBase64: String
}

actor PolymarketUSVenue: PredictionMarketVenue {
    nonisolated let venueID = PredictionMarketVenueID.polymarketUS

    private let credentials: PolymarketUSCredentials
    private let gatewayBaseURL: URL
    private let webSocketURL: URL
    private let recorder: MarketStateRecorder?
    private let session: URLSession

    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var heartbeatTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var reconnectAttempt = 0
    private var shouldReconnect = false
    private var lastReceiveMonotonicNanoseconds: UInt64 = 0

    private var books: [String: LocalOrderBook] = [:]
    private var orderBookSubscriptions: Set<String> = []
    private var tradeSubscriptions: Set<String> = []

    init(
        credentials: PolymarketUSCredentials,
        recorder: MarketStateRecorder? = nil,
        gatewayBaseURL: URL = URL(string: "https://gateway.polymarket.us")!,
        webSocketURL: URL = URL(string: "wss://api.polymarket.us/v1/ws/markets")!,
        session: URLSession = .shared
    ) {
        self.credentials = credentials
        self.recorder = recorder
        self.gatewayBaseURL = gatewayBaseURL
        self.webSocketURL = webSocketURL
        self.session = session
    }

    func authenticate() async throws {
        _ = try authenticationHeaders(method: "GET", path: "/v1/ws/markets")
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
        var offset = 0
        let limit = 100
        var result: [NormalizedMarket] = []
        while true {
            let payload = try await VenueTransport.json(
                baseURL: gatewayBaseURL,
                path: "/v1/markets",
                queryItems: [
                    URLQueryItem(name: "active", value: "true"),
                    URLQueryItem(name: "closed", value: "false"),
                    URLQueryItem(name: "limit", value: String(limit)),
                    URLQueryItem(name: "offset", value: String(offset))
                ]
            )
            let page = payload["markets"] as? [[String: Any]] ?? []
            result.append(contentsOf: try page.map(MarketNormalizer.polymarketUSMarket))
            if page.count < limit { break }
            offset += limit
        }

        guard let query, !query.isEmpty else { return result }
        return result.filter {
            "\($0.title) \($0.contractWording) \($0.tournament ?? "")"
                .localizedCaseInsensitiveContains(query)
        }
    }

    func subscribeOrderBook(_ contracts: [ContractID]) async throws {
        try contracts.forEach { try validate($0) }
        let slugs = Array(Set(contracts.map(\.marketID)))
        orderBookSubscriptions.formUnion(slugs)
        try await withThrowingTaskGroup(of: Void.self) { group in
            for slug in slugs { group.addTask { try await self.bootstrapBook(slug) } }
            try await group.waitForAll()
        }
        if socket != nil { try await subscribe(type: "SUBSCRIPTION_TYPE_MARKET_DATA", prefix: "book", slugs: slugs) }
    }

    func subscribeTrades(_ contracts: [ContractID]) async throws {
        try contracts.forEach { try validate($0) }
        let slugs = Array(Set(contracts.map(\.marketID)))
        tradeSubscriptions.formUnion(slugs)
        if socket != nil { try await subscribe(type: "SUBSCRIPTION_TYPE_TRADE", prefix: "trades", slugs: slugs) }
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
        var request = URLRequest(url: webSocketURL)
        authenticationHeaders(method: "GET", path: "/v1/ws/markets").forEach {
            request.setValue($0.value, forHTTPHeaderField: $0.key)
        }
        let newSocket = session.webSocketTask(with: request)
        socket = newSocket
        newSocket.resume()
        reconnectAttempt = 0
        lastReceiveMonotonicNanoseconds = DispatchTime.now().uptimeNanoseconds
        try await bootstrapAllBooks()
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
                noteReceive(received.monotonicNanoseconds)
                if let recorder {
                    try? await recorder.recordRaw(venue: .polymarketUS, payload: data, received: received)
                }
                try await handleMessage(data, received: received)
            }
        } catch {
            connectionFailed()
        }
    }

    private func runHeartbeatLoop(_ webSocket: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(20))
            if isFeedSilent(for: 90) { connectionFailed(); return }
            do { try await VenueTransport.ping(webSocket) }
            catch { connectionFailed(); return }
        }
    }

    private func noteReceive(_ timestamp: UInt64) { lastReceiveMonotonicNanoseconds = timestamp }

    private func isFeedSilent(for seconds: UInt64) -> Bool {
        DispatchTime.now().uptimeNanoseconds - lastReceiveMonotonicNanoseconds > seconds * 1_000_000_000
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

    private func bootstrapAllBooks() async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            for slug in orderBookSubscriptions {
                group.addTask { try await self.bootstrapBook(slug) }
            }
            try await group.waitForAll()
        }
    }

    private func bootstrapBook(_ slug: String) async throws {
        let payload = try await VenueTransport.json(
            baseURL: gatewayBaseURL, path: "/v1/markets/\(slug)/book"
        )
        try await applyMarketData((payload["marketData"] as? [String: Any]) ?? payload, received: .now())
    }

    private func restoreSubscriptions() async throws {
        if !orderBookSubscriptions.isEmpty {
            try await subscribe(
                type: "SUBSCRIPTION_TYPE_MARKET_DATA", prefix: "book",
                slugs: Array(orderBookSubscriptions)
            )
        }
        if !tradeSubscriptions.isEmpty {
            try await subscribe(
                type: "SUBSCRIPTION_TYPE_TRADE", prefix: "trades",
                slugs: Array(tradeSubscriptions)
            )
        }
    }

    private func subscribe(type: String, prefix: String, slugs: [String]) async throws {
        for (index, chunk) in slugs.chunked(into: 100).enumerated() {
            let request: [String: Any] = [
                "subscribe": [
                    "requestId": "\(prefix)-\(index)-\(UUID().uuidString)",
                    "subscriptionType": type,
                    "marketSlugs": chunk,
                    "responsesDebounced": false
                ]
            ]
            try await sendJSON(request)
        }
    }

    private func sendJSON(_ object: [String: Any]) async throws {
        guard let socket else { return }
        let data = try JSONSerialization.data(withJSONObject: object)
        try await socket.send(.data(data))
    }

    private func handleMessage(_ data: Data, received: ReceiveTimestamp) async throws {
        let root = try JSONSerialization.jsonObject(with: data)
        let messages = (root as? [[String: Any]]) ?? ((root as? [String: Any]).map { [$0] } ?? [])
        for message in messages {
            if message["heartbeat"] != nil {
                if let socket { try? await VenueTransport.ping(socket) }
                continue
            }
            if let marketData = message["marketData"] as? [String: Any] {
                try await applyMarketData(marketData, received: received)
                continue
            }
            if let tradeData = message["trade"] as? [String: Any],
               let slug = tradeData["marketSlug"] as? String {
                let trade = NormalizedTrade(
                    contract: ContractID(venue: .polymarketUS, marketID: slug),
                    price: MarketNormalizer.decimal(tradeData["price"]),
                    quantity: MarketNormalizer.decimal(tradeData["quantity"]),
                    exchangeTimestamp: MarketNormalizer.date(tradeData["tradeTime"]),
                    received: received,
                    tradeID: tradeData["id"] as? String
                )
                if var book = books[slug] {
                    book.updateTrade(trade)
                    books[slug] = book
                    try await recorder?.record(snapshot: book.snapshot())
                }
            }
        }
    }

    private func applyMarketData(_ data: [String: Any], received: ReceiveTimestamp) async throws {
        guard let slug = data["marketSlug"] as? String else {
            throw VenueError.invalidPayload("Polymarket US book has no marketSlug")
        }
        let levels = try MarketNormalizer.polymarketUSBook(data)
        var book = books[slug] ?? LocalOrderBook(contract: ContractID(venue: .polymarketUS, marketID: slug))
        try book.replace(
            bids: levels.bids,
            asks: levels.asks,
            state: MarketNormalizer.polymarketUSState(data["state"]),
            exchangeTimestamp: MarketNormalizer.date(data["transactTime"]),
            received: received,
            sequence: nil // retail US WebSocket documents full updates, no sequence field
        )
        let stats = (data["stats"] as? [String: Any]) ?? [:]
        if stats["lastTradePx"] != nil {
            book.updateTrade(NormalizedTrade(
                contract: book.contract,
                price: MarketNormalizer.decimal(stats["lastTradePx"]),
                quantity: MarketNormalizer.decimal(stats["lastTradeQty"]),
                exchangeTimestamp: MarketNormalizer.date(stats["lastTradeSetTime"]),
                received: received,
                tradeID: nil
            ))
        }
        books[slug] = book
        try await recorder?.record(snapshot: book.snapshot())
    }

    private func authenticationHeaders(method: String, path: String) throws -> [String: String] {
        guard !credentials.keyID.isEmpty, let raw = Data(base64Encoded: credentials.secretKeyBase64), raw.count >= 32 else {
            throw VenueError.credentialsMissing("Polymarket US key ID and base64 Ed25519 secret are required")
        }
        let seed = Data(raw.prefix(32))
        let key: Curve25519.Signing.PrivateKey
        do { key = try Curve25519.Signing.PrivateKey(rawRepresentation: seed) }
        catch { throw VenueError.authenticationFailed("Invalid Polymarket US Ed25519 secret") }
        let timestamp = String(Int64(Date().timeIntervalSince1970 * 1_000))
        let message = Data("\(timestamp)\(method.uppercased())\(path)".utf8)
        let signature = try key.signature(for: message)
        return [
            "X-PM-Access-Key": credentials.keyID,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": signature.base64EncodedString()
        ]
    }
}

private extension Array {
    func chunked(into size: Int) -> [[Element]] {
        guard size > 0 else { return [] }
        return stride(from: 0, to: count, by: size).map {
            Array(self[$0..<Swift.min($0 + size, count)])
        }
    }
}
