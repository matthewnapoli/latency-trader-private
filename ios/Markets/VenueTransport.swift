import Foundation

enum VenueTransport {
    static func json(
        baseURL: URL,
        path: String,
        queryItems: [URLQueryItem] = [],
        headers: [String: String] = [:]
    ) async throws -> [String: Any] {
        guard var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false) else {
            throw VenueError.transport("Could not construct URL")
        }
        if !queryItems.isEmpty { components.queryItems = queryItems }
        guard let url = components.url else { throw VenueError.transport("Could not construct URL") }
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("LatencyTrader/0.1 (read-only market data)", forHTTPHeaderField: "User-Agent")
        headers.forEach { request.setValue($0.value, forHTTPHeaderField: $0.key) }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, 200..<300 ~= http.statusCode else {
            throw VenueError.transport("GET \(url.absoluteString) failed")
        }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw VenueError.invalidPayload("Expected a JSON object")
        }
        return object
    }

    static func ping(_ task: URLSessionWebSocketTask) async throws {
        return try await withCheckedThrowingContinuation { continuation in
            task.sendPing { error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume() }
            }
        }
    }
}
