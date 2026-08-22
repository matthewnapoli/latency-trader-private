import Foundation

enum MarketNormalizer {
    static func decimal(_ value: Any?) -> Decimal {
        let raw: Any?
        if let object = value as? [String: Any] { raw = object["value"] } else { raw = value }
        if let decimal = raw as? Decimal { return decimal }
        if let number = raw as? NSNumber { return number.decimalValue }
        if let string = raw as? String { return Decimal(string: string) ?? 0 }
        return 0
    }

    static func date(_ value: Any?) -> Date? {
        guard let text = value as? String else { return nil }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }

    static func kalshiState(_ raw: Any?) -> NormalizedMarketState {
        switch String(describing: raw ?? "").lowercased() {
        case "initialized", "unopened": return .preopen
        case "open": return .open
        case "paused": return .suspended
        case "closed": return .closed
        case "settled", "finalized": return .settled
        default: return .unknown
        }
    }

    static func polymarketUSState(_ raw: Any?) -> NormalizedMarketState {
        switch String(describing: raw ?? "").uppercased() {
        case "MARKET_STATE_OPEN", "OPEN": return .open
        case "MARKET_STATE_PREOPEN", "PREOPEN": return .preopen
        case "MARKET_STATE_SUSPENDED", "MARKET_STATE_HALTED", "SUSPENDED", "HALTED": return .suspended
        case "MARKET_STATE_EXPIRED", "MARKET_STATE_TERMINATED", "CLOSED": return .closed
        case "SETTLED", "RESOLVED": return .settled
        default: return .unknown
        }
    }

    static func kalshiBook(_ object: [String: Any]) throws -> (bids: [BookLevel], asks: [BookLevel]) {
        let body = (object["orderbook_fp"] as? [String: Any]) ?? object
        let yes = (body["yes_dollars"] ?? body["yes_dollars_fp"]) as? [[Any]] ?? []
        let no = (body["no_dollars"] ?? body["no_dollars_fp"]) as? [[Any]] ?? []
        let bids = yes.compactMap { level -> BookLevel? in
            guard level.count >= 2 else { return nil }
            return BookLevel(price: decimal(level[0]), quantity: decimal(level[1]))
        }.sorted { $0.price > $1.price }
        let asks = no.compactMap { level -> BookLevel? in
            guard level.count >= 2 else { return nil }
            return BookLevel(price: 1 - decimal(level[0]), quantity: decimal(level[1]))
        }.sorted { $0.price < $1.price }
        return (bids, asks)
    }

    static func polymarketUSBook(_ object: [String: Any]) throws -> (bids: [BookLevel], asks: [BookLevel]) {
        let body = (object["marketData"] as? [String: Any]) ?? object
        func levels(_ key: String) -> [BookLevel] {
            ((body[key] as? [[String: Any]]) ?? []).map {
                BookLevel(price: decimal($0["px"]), quantity: decimal($0["qty"]))
            }
        }
        return (
            levels("bids").sorted { $0.price > $1.price },
            levels("offers").sorted { $0.price < $1.price }
        )
    }

    static func kalshiMarket(_ object: [String: Any]) throws -> NormalizedMarket {
        guard let ticker = object["ticker"] as? String else {
            throw VenueError.invalidPayload("Kalshi market has no ticker")
        }
        let title = (object["title"] as? String) ?? (object["subtitle"] as? String) ?? ticker
        let start = date(object["occurrence_datetime"] ?? object["open_time"])
        return NormalizedMarket(
            contract: ContractID(venue: .kalshi, marketID: ticker),
            title: title,
            contractWording: (object["rules_primary"] as? String) ?? title,
            state: kalshiState(object["status"]),
            playerNames: playerNames(in: title),
            tournament: (object["series_ticker"] as? String) ?? (object["event_ticker"] as? String),
            scheduledStart: start,
            eventDate: start.map { Calendar(identifier: .iso8601).startOfDay(for: $0) }
        )
    }

    static func polymarketUSMarket(_ object: [String: Any]) throws -> NormalizedMarket {
        guard let slug = object["slug"] as? String else {
            throw VenueError.invalidPayload("Polymarket US market has no slug")
        }
        let title = (object["question"] as? String) ?? (object["title"] as? String) ?? slug
        let start = date(object["gameStartTime"] ?? object["startDate"])
        let state: NormalizedMarketState = (object["closed"] as? Bool) == true
            ? .closed
            : ((object["active"] as? Bool) == true ? .open : polymarketUSState(object["ep3Status"]))
        let tags = object["tags"] as? [[String: Any]] ?? []
        return NormalizedMarket(
            contract: ContractID(venue: .polymarketUS, marketID: slug),
            title: title,
            contractWording: (object["description"] as? String) ?? title,
            state: state,
            playerNames: playerNames(in: title),
            tournament: tags.lazy.compactMap { $0["label"] as? String }.first,
            scheduledStart: start,
            eventDate: start.map { Calendar(identifier: .iso8601).startOfDay(for: $0) }
        )
    }

    static func playerNames(in text: String) -> [String] {
        let separators = [" vs. ", " vs ", " v. ", " v ", " - "]
        let lower = text.lowercased()
        for separator in separators {
            guard let range = lower.range(of: separator) else { continue }
            let left = String(text[..<range.lowerBound])
                .trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
            let right = String(text[range.upperBound...]).components(separatedBy: "?").first?
                .trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters)) ?? ""
            if !left.isEmpty && !right.isEmpty { return [left, right] }
        }
        return []
    }
}
