import Foundation

struct MarketMatch: Sendable {
    let left: NormalizedMarket
    let right: NormalizedMarket
    let confidence: Double
    let components: [String: Double]
    let requiresManualConfirmation: Bool
    private(set) var manuallyConfirmed = false

    var usable: Bool { !requiresManualConfirmation || manuallyConfirmed }
    mutating func confirm() { manuallyConfirmed = true }
}

struct MarketMatcher: Sendable {
    let automaticThreshold: Double

    init(automaticThreshold: Double = 0.80) {
        self.automaticThreshold = automaticThreshold
    }

    func compare(_ left: NormalizedMarket, _ right: NormalizedMarket) -> MarketMatch {
        let players = playerSimilarity(left.playerNames, right.playerNames)
        let tournament = similarity(left.tournament, right.tournament)
        let wording = similarity(left.contractWording, right.contractWording)
        let eventDate: Double
        if let lhs = left.eventDate, let rhs = right.eventDate {
            let days = abs(Calendar(identifier: .iso8601).dateComponents([.day], from: lhs, to: rhs).day ?? 99)
            eventDate = days == 0 ? 1 : (days == 1 ? 0.5 : 0)
        } else { eventDate = 0 }
        let startTime: Double
        if let lhs = left.scheduledStart, let rhs = right.scheduledStart {
            startTime = max(0, 1 - abs(lhs.timeIntervalSince(rhs)) / (6 * 3_600))
        } else { startTime = 0 }

        let components = [
            "players": players,
            "tournament": tournament,
            "start_time": startTime,
            "event_date": eventDate,
            "wording": wording
        ]
        let score = 0.40 * players + 0.15 * tournament + 0.15 * startTime
            + 0.10 * eventDate + 0.20 * wording
        let automatic = score >= automaticThreshold && (players >= 0.75 || left.playerNames.isEmpty)
        return MarketMatch(
            left: left,
            right: right,
            confidence: score,
            components: components,
            requiresManualConfirmation: !automatic
        )
    }

    func bestMatches(left: [NormalizedMarket], right: [NormalizedMarket]) -> [MarketMatch] {
        left.compactMap { market in
            right.map { compare(market, $0) }.max { $0.confidence < $1.confidence }
        }.sorted { $0.confidence > $1.confidence }
    }

    private func playerSimilarity(_ left: [String], _ right: [String]) -> Double {
        guard !left.isEmpty, !right.isEmpty else { return 0 }
        let direct = zip(left, right).map { similarity($0.0, $0.1) }.reduce(0, +)
            / Double(max(left.count, right.count))
        let reversed = zip(left, right.reversed()).map { similarity($0.0, $0.1) }.reduce(0, +)
            / Double(max(left.count, right.count))
        return max(direct, reversed)
    }

    private func similarity(_ left: String?, _ right: String?) -> Double {
        guard let left, let right else { return 0 }
        return similarity(left, right)
    }

    private func similarity(_ left: String, _ right: String) -> Double {
        let lhs = tokens(left), rhs = tokens(right)
        guard !lhs.isEmpty, !rhs.isEmpty else { return 0 }
        return Double(lhs.intersection(rhs).count) / Double(lhs.union(rhs).count)
    }

    private func tokens(_ value: String) -> Set<String> {
        let folded = value.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: .current)
        return Set(folded.components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty })
    }
}
