import Foundation

enum PredictionMarketVenueID: String, Codable, Sendable {
    case kalshi
    case polymarketUS = "polymarket_us"
}

enum MarketOutcome: String, Codable, Sendable {
    case yes
    case no
}

enum NormalizedMarketState: String, Codable, Sendable {
    case preopen
    case open
    case suspended
    case closed
    case settled
    case unknown
}

enum PaperOrderSide: String, Codable, Sendable {
    case buy
    case sell
}

struct ReceiveTimestamp: Codable, Equatable, Sendable {
    let wallUnixNanoseconds: UInt64
    let monotonicNanoseconds: UInt64

    static func now() -> ReceiveTimestamp {
        let wall = UInt64(max(0, Date().timeIntervalSince1970 * 1_000_000_000))
        return ReceiveTimestamp(
            wallUnixNanoseconds: wall,
            monotonicNanoseconds: DispatchTime.now().uptimeNanoseconds
        )
    }

    enum CodingKeys: String, CodingKey {
        case wallUnixNanoseconds = "wall_time_ns"
        case monotonicNanoseconds = "monotonic_ns"
    }
}

struct ContractID: Codable, Hashable, Sendable {
    let venue: PredictionMarketVenueID
    let marketID: String
    let outcome: MarketOutcome

    init(venue: PredictionMarketVenueID, marketID: String, outcome: MarketOutcome = .yes) {
        self.venue = venue
        self.marketID = marketID
        self.outcome = outcome
    }
}

struct BookLevel: Codable, Equatable, Sendable {
    let price: Decimal       // normalized USD probability in [0, 1]
    let quantity: Decimal    // normalized contracts/shares
}

struct NormalizedTrade: Codable, Equatable, Sendable {
    let contract: ContractID
    let price: Decimal
    let quantity: Decimal
    let exchangeTimestamp: Date?
    let received: ReceiveTimestamp
    let tradeID: String?
}

struct NormalizedMarket: Codable, Sendable {
    let contract: ContractID
    let title: String
    let contractWording: String
    let state: NormalizedMarketState
    let playerNames: [String]
    let tournament: String?
    let scheduledStart: Date?
    let eventDate: Date?
}

struct OrderBookSnapshot: Codable, Equatable, Sendable {
    let contract: ContractID
    let bids: [BookLevel]
    let asks: [BookLevel]
    let state: NormalizedMarketState
    let exchangeTimestamp: Date?
    let received: ReceiveTimestamp
    let lastTrade: NormalizedTrade?
    let sequence: Int64?
    let synchronized: Bool

    var bestBid: BookLevel? { bids.first }
    var bestAsk: BookLevel? { asks.first }
    var mid: Decimal? {
        guard let bid = bestBid?.price, let ask = bestAsk?.price else { return nil }
        return (bid + ask) / 2
    }
}

struct PaperOrder: Codable, Sendable {
    let contract: ContractID
    let side: PaperOrderSide
    let quantity: Decimal
    let limitPrice: Decimal?
    let signalID: String?
}

struct PaperFill: Codable, Sendable {
    let order: PaperOrder
    let submitted: ReceiveTimestamp
    let filledQuantity: Decimal
    let averagePrice: Decimal?
    let notional: Decimal
    let unfilledQuantity: Decimal
    let levelsConsumed: [BookLevel]
}

enum VenueError: Error, LocalizedError {
    case credentialsMissing(String)
    case authenticationFailed(String)
    case invalidPayload(String)
    case noLocalBook(String)
    case venueMismatch
    case staleBook
    case sequenceGap(expected: Int64, received: Int64)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .credentialsMissing(let message), .authenticationFailed(let message),
             .invalidPayload(let message), .transport(let message):
            return message
        case .noLocalBook(let market): return "No local order book for \(market)"
        case .venueMismatch: return "Contract belongs to a different venue"
        case .staleBook: return "Paper execution is forbidden on a stale book"
        case .sequenceGap(let expected, let received):
            return "Order-book sequence gap: expected \(expected), received \(received)"
        }
    }
}

protocol PredictionMarketVenue: Actor {
    nonisolated var venueID: PredictionMarketVenueID { get }

    func connect() async throws
    func authenticate() async throws
    func discoverMarkets(query: String?) async throws -> [NormalizedMarket]
    func subscribeOrderBook(_ contracts: [ContractID]) async throws
    func subscribeTrades(_ contracts: [ContractID]) async throws
    func getBestBidAsk(_ contract: ContractID) throws -> (BookLevel?, BookLevel?)
    func getOrderBook(_ contract: ContractID) throws -> OrderBookSnapshot
    func submitPaperOrder(_ order: PaperOrder) throws -> PaperFill
    func recordMarketState(_ contract: ContractID) async throws -> OrderBookSnapshot
    func close() async
}

struct LocalOrderBook: Sendable {
    let contract: ContractID
    private(set) var bids: [Decimal: Decimal] = [:]
    private(set) var asks: [Decimal: Decimal] = [:]
    private(set) var state: NormalizedMarketState = .unknown
    private(set) var exchangeTimestamp: Date?
    private(set) var received = ReceiveTimestamp(wallUnixNanoseconds: 0, monotonicNanoseconds: 0)
    private(set) var lastTrade: NormalizedTrade?
    private(set) var sequence: Int64?
    private(set) var synchronized = false

    mutating func replace(
        bids newBids: [BookLevel],
        asks newAsks: [BookLevel],
        state: NormalizedMarketState,
        exchangeTimestamp: Date?,
        received: ReceiveTimestamp,
        sequence: Int64?
    ) throws {
        bids = Dictionary(uniqueKeysWithValues: newBids.filter { $0.quantity > 0 }.map { ($0.price, $0.quantity) })
        asks = Dictionary(uniqueKeysWithValues: newAsks.filter { $0.quantity > 0 }.map { ($0.price, $0.quantity) })
        self.state = state
        self.exchangeTimestamp = exchangeTimestamp
        self.received = received
        self.sequence = sequence
        synchronized = true
        try validateUncrossed()
    }

    mutating func applyDelta(
        side: PaperOrderSide,
        price: Decimal,
        quantityDelta: Decimal,
        sequence: Int64,
        exchangeTimestamp: Date?,
        received: ReceiveTimestamp
    ) throws {
        guard synchronized else { throw VenueError.staleBook }
        var levels = side == .buy ? bids : asks
        let quantity = (levels[price] ?? 0) + quantityDelta
        guard quantity >= 0 else {
            synchronized = false
            throw VenueError.invalidPayload("Delta made a price level negative")
        }
        if quantity == 0 { levels.removeValue(forKey: price) } else { levels[price] = quantity }
        if side == .buy { bids = levels } else { asks = levels }
        self.sequence = sequence
        self.exchangeTimestamp = exchangeTimestamp
        self.received = received
        try validateUncrossed()
    }

    mutating func updateTrade(_ trade: NormalizedTrade) { lastTrade = trade }
    mutating func markStale() { synchronized = false }

    func snapshot() -> OrderBookSnapshot {
        OrderBookSnapshot(
            contract: contract,
            bids: bids.map { BookLevel(price: $0.key, quantity: $0.value) }.sorted { $0.price > $1.price },
            asks: asks.map { BookLevel(price: $0.key, quantity: $0.value) }.sorted { $0.price < $1.price },
            state: state,
            exchangeTimestamp: exchangeTimestamp,
            received: received,
            lastTrade: lastTrade,
            sequence: sequence,
            synchronized: synchronized
        )
    }

    private mutating func validateUncrossed() throws {
        if let bid = bids.keys.max(), let ask = asks.keys.min(), bid > ask {
            synchronized = false
            throw VenueError.invalidPayload("Normalized order book is crossed")
        }
    }
}

enum PaperExecutionEngine {
    static func executeIOC(_ order: PaperOrder, against book: OrderBookSnapshot) throws -> PaperFill {
        guard order.contract == book.contract else { throw VenueError.venueMismatch }
        guard book.synchronized else { throw VenueError.staleBook }
        guard order.quantity > 0 else { throw VenueError.invalidPayload("Quantity must be positive") }

        let levels = order.side == .buy ? book.asks : book.bids
        var remaining = order.quantity
        var notional: Decimal = 0
        var consumed: [BookLevel] = []
        for level in levels {
            if let limit = order.limitPrice {
                if order.side == .buy && level.price > limit { break }
                if order.side == .sell && level.price < limit { break }
            }
            let quantity = min(remaining, level.quantity)
            guard quantity > 0 else { continue }
            consumed.append(BookLevel(price: level.price, quantity: quantity))
            notional += level.price * quantity
            remaining -= quantity
            if remaining == 0 { break }
        }
        let filled = order.quantity - remaining
        return PaperFill(
            order: order,
            submitted: .now(),
            filledQuantity: filled,
            averagePrice: filled > 0 ? notional / filled : nil,
            notional: notional,
            unfilledQuantity: remaining,
            levelsConsumed: consumed
        )
    }
}

actor MarketStateRecorder {
    private let fileURL: URL
    private let encoder: JSONEncoder

    init(fileURL: URL) throws {
        self.fileURL = fileURL
        encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        try FileManager.default.createDirectory(
            at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        if !FileManager.default.fileExists(atPath: fileURL.path) {
            FileManager.default.createFile(atPath: fileURL.path, contents: nil)
        }
    }

    func record(snapshot: OrderBookSnapshot) throws {
        let envelope = SnapshotEnvelope(recordType: "normalized_market_state", snapshot: snapshot)
        try append(encoder.encode(envelope))
    }

    func recordRaw(venue: PredictionMarketVenueID, payload: Data, received: ReceiveTimestamp) throws {
        guard let raw = String(data: payload, encoding: .utf8) else {
            throw VenueError.invalidPayload("Market WebSocket payload is not UTF-8")
        }
        let object: [String: Any] = [
            "record_type": "raw_market_message",
            "venue": venue.rawValue,
            "received": [
                "wall_time_ns": received.wallUnixNanoseconds,
                "monotonic_ns": received.monotonicNanoseconds
            ],
            "raw_payload": raw
        ]
        try append(JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]))
    }

    private func append(_ data: Data) throws {
        let handle = try FileHandle(forWritingTo: fileURL)
        defer { try? handle.close() }
        try handle.seekToEnd()
        try handle.write(contentsOf: data)
        try handle.write(contentsOf: Data([0x0A]))
        try handle.synchronize()
    }

    private struct SnapshotEnvelope: Codable {
        let recordType: String
        let snapshot: OrderBookSnapshot

        enum CodingKeys: String, CodingKey {
            case recordType = "record_type"
            case snapshot
        }
    }
}
