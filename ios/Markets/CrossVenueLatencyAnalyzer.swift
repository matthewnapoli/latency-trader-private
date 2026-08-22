import Foundation

struct PointSignal: Codable, Sendable {
    let signalID: String
    let winner: String
    let confidence: Decimal
    let observablePointEndMonotonicNanoseconds: UInt64
    let cvDetectionMonotonicNanoseconds: UInt64

    var signalLatencyMilliseconds: Decimal {
        Decimal(cvDetectionMonotonicNanoseconds - observablePointEndMonotonicNanoseconds) / 1_000_000
    }
}

struct VenueSignalSnapshot: Sendable {
    let signal: PointSignal
    let kalshi: OrderBookSnapshot
    let polymarketUS: OrderBookSnapshot
}

struct VenueLatencyMetrics: Codable, Identifiable, Sendable {
    var id: PredictionMarketVenueID { venue }
    let venue: PredictionMarketVenueID
    let contract: ContractID
    let observablePointEndTimestamp: UInt64
    let cvDetectionTimestamp: UInt64
    let signalLatencyMilliseconds: Decimal
    let preEventMid: Decimal?
    let simulatedFillPrice: Decimal?
    let firstRepricingTimestamp: UInt64?
    let marketReactionLatencyMilliseconds: Decimal?
    let availableSizeBeforeRepricing: Decimal
    let simulatedPnL: Decimal?
    let bestAvailablePrice: Decimal?
    let availableDepth: Decimal
}

actor CrossVenueSignalCoordinator {
    private let minimumConfidence: Decimal

    init(minimumConfidence: Decimal = Decimal(string: "0.95")!) {
        self.minimumConfidence = minimumConfidence
    }

    func snapshot(
        signal: PointSignal,
        match: MarketMatch,
        kalshi: any PredictionMarketVenue,
        polymarketUS: any PredictionMarketVenue
    ) async throws -> VenueSignalSnapshot {
        guard signal.confidence >= minimumConfidence else {
            throw VenueError.invalidPayload("CV signal is below the configured confidence threshold")
        }
        guard match.usable else {
            throw VenueError.invalidPayload("Low-confidence market match requires manual confirmation")
        }
        let contracts = [match.left.contract, match.right.contract]
        guard let kalshiContract = contracts.first(where: { $0.venue == .kalshi }),
              let polymarketContract = contracts.first(where: { $0.venue == .polymarketUS }) else {
            throw VenueError.invalidPayload("Cross-venue match must contain one contract from each venue")
        }

        async let kalshiSnapshot = kalshi.recordMarketState(kalshiContract)
        async let polymarketSnapshot = polymarketUS.recordMarketState(polymarketContract)
        return try await VenueSignalSnapshot(
            signal: signal,
            kalshi: kalshiSnapshot,
            polymarketUS: polymarketSnapshot
        )
    }
}

enum VenueLatencyAnalyzer {
    static func analyze(
        signal: PointSignal,
        signalBook: OrderBookSnapshot,
        fill: PaperFill,
        subsequentBooks: [OrderBookSnapshot],
        preEventBook: OrderBookSnapshot? = nil,
        settlementOrMark: Decimal? = nil,
        repricingThreshold: Decimal = Decimal(string: "0.001")!
    ) -> VenueLatencyMetrics {
        let baseline = preEventBook ?? signalBook
        let baselineMid = baseline.mid
        let repricing = subsequentBooks
            .sorted { $0.received.monotonicNanoseconds < $1.received.monotonicNanoseconds }
            .first { candidate in
            guard candidate.received.monotonicNanoseconds >= signal.cvDetectionMonotonicNanoseconds,
                  let before = baselineMid, let after = candidate.mid else { return false }
            return abs(after - before) >= repricingThreshold
            }
        let reactionLatency = repricing.map {
            Decimal($0.received.monotonicNanoseconds - signal.observablePointEndMonotonicNanoseconds)
                / 1_000_000
        }
        let best = fill.order.side == .buy ? signalBook.bestAsk : signalBook.bestBid
        let executableSide = fill.order.side == .buy ? signalBook.asks : signalBook.bids
        let pnl: Decimal?
        if let mark = settlementOrMark, let fillPrice = fill.averagePrice {
            let direction: Decimal = fill.order.side == .buy ? 1 : -1
            pnl = direction * fill.filledQuantity * (mark - fillPrice)
        } else { pnl = nil }

        return VenueLatencyMetrics(
            venue: signalBook.contract.venue,
            contract: signalBook.contract,
            observablePointEndTimestamp: signal.observablePointEndMonotonicNanoseconds,
            cvDetectionTimestamp: signal.cvDetectionMonotonicNanoseconds,
            signalLatencyMilliseconds: signal.signalLatencyMilliseconds,
            preEventMid: baselineMid,
            simulatedFillPrice: fill.averagePrice,
            firstRepricingTimestamp: repricing?.received.monotonicNanoseconds,
            marketReactionLatencyMilliseconds: reactionLatency,
            availableSizeBeforeRepricing: best?.quantity ?? 0,
            simulatedPnL: pnl,
            bestAvailablePrice: best?.price,
            availableDepth: executableSide.reduce(0) { $0 + $1.quantity }
        )
    }

    static func preEventBook(
        from history: [OrderBookSnapshot],
        signal: PointSignal
    ) -> OrderBookSnapshot? {
        history
            .filter { $0.received.monotonicNanoseconds <= signal.observablePointEndMonotonicNanoseconds }
            .max { $0.received.monotonicNanoseconds < $1.received.monotonicNanoseconds }
    }
}
