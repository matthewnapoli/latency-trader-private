import Foundation
import SwiftUI

struct CrossVenueComparisonView: View {
    let kalshi: VenueLatencyMetrics?
    let polymarketUS: VenueLatencyMetrics?

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
            GridRow {
                Text("Metric").font(.caption.bold())
                Text("Kalshi").font(.caption.bold()).gridColumnAlignment(.trailing)
                Text("Polymarket US").font(.caption.bold()).gridColumnAlignment(.trailing)
            }
            Divider().gridCellUnsizedAxes(.horizontal)
            row("CV signal latency", kalshi?.signalLatencyMilliseconds, polymarketUS?.signalLatencyMilliseconds, suffix: " ms")
            row("Market reaction latency", kalshi?.marketReactionLatencyMilliseconds, polymarketUS?.marketReactionLatencyMilliseconds, suffix: " ms")
            row("Best available price", kalshi?.bestAvailablePrice, polymarketUS?.bestAvailablePrice)
            row("Available depth", kalshi?.availableDepth, polymarketUS?.availableDepth)
            row("Simulated fill", kalshi?.simulatedFillPrice, polymarketUS?.simulatedFillPrice)
            row("Simulated PnL", kalshi?.simulatedPnL, polymarketUS?.simulatedPnL)
        }
        .font(.system(.caption, design: .monospaced))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Cross-venue latency comparison")
    }

    private func row(
        _ label: String,
        _ kalshiValue: Decimal?,
        _ polymarketValue: Decimal?,
        suffix: String = ""
    ) -> some View {
        GridRow {
            Text(label)
            Text(format(kalshiValue, suffix: suffix)).frame(maxWidth: .infinity, alignment: .trailing)
            Text(format(polymarketValue, suffix: suffix)).frame(maxWidth: .infinity, alignment: .trailing)
        }
    }

    private func format(_ value: Decimal?, suffix: String) -> String {
        guard let value else { return "—" }
        return "\(NSDecimalNumber(decimal: value).stringValue)\(suffix)"
    }
}
