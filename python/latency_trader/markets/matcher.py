from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import timezone
from difflib import SequenceMatcher

from .models import NormalizedMarket


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.casefold()))


def _similarity(left: str | None, right: str | None) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_tokens, b_tokens = set(a.split()), set(b.split())
    token = len(a_tokens & b_tokens) / max(len(a_tokens | b_tokens), 1)
    return max(sequence, token)


def _player_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    if not left or not right:
        return 0.0
    # Player order can be reversed across venues.
    direct = sum(_similarity(a, b) for a, b in zip(left, right)) / max(len(left), len(right))
    reverse = sum(_similarity(a, b) for a, b in zip(left, reversed(right))) / max(
        len(left), len(right)
    )
    return max(direct, reverse)


@dataclass(frozen=True, slots=True)
class MarketMatch:
    left: NormalizedMarket
    right: NormalizedMarket
    confidence: float
    components: dict[str, float]
    requires_manual_confirmation: bool
    manually_confirmed: bool = False

    @property
    def usable(self) -> bool:
        return not self.requires_manual_confirmation or self.manually_confirmed

    def confirm(self) -> MarketMatch:
        return replace(self, manually_confirmed=True)


class MarketMatcher:
    def __init__(self, *, automatic_threshold: float = 0.80) -> None:
        if not 0 <= automatic_threshold <= 1:
            raise ValueError("automatic threshold must be in [0, 1]")
        self.automatic_threshold = automatic_threshold

    def compare(self, left: NormalizedMarket, right: NormalizedMarket) -> MarketMatch:
        players = _player_similarity(left.player_names, right.player_names)
        tournament = _similarity(left.tournament, right.tournament)
        wording = _similarity(left.contract_wording or left.title, right.contract_wording or right.title)

        if left.event_date and right.event_date:
            date_delta = abs((left.event_date - right.event_date).days)
            event_date = 1.0 if date_delta == 0 else 0.5 if date_delta == 1 else 0.0
        else:
            event_date = 0.0

        if left.scheduled_start and right.scheduled_start:
            l_time = left.scheduled_start.astimezone(timezone.utc)
            r_time = right.scheduled_start.astimezone(timezone.utc)
            hours = abs((l_time - r_time).total_seconds()) / 3600
            start_time = max(0.0, 1.0 - hours / 6.0)
        else:
            start_time = 0.0

        components = {
            "players": players,
            "tournament": tournament,
            "start_time": start_time,
            "event_date": event_date,
            "wording": wording,
        }
        score = (
            0.40 * players
            + 0.15 * tournament
            + 0.15 * start_time
            + 0.10 * event_date
            + 0.20 * wording
        )
        # A high aggregate score may still be unsafe when explicit players disagree.
        automatic = score >= self.automatic_threshold and (players >= 0.75 or not left.player_names)
        return MarketMatch(
            left=left,
            right=right,
            confidence=score,
            components=components,
            requires_manual_confirmation=not automatic,
        )

    def best_matches(
        self, left_markets: list[NormalizedMarket], right_markets: list[NormalizedMarket]
    ) -> list[MarketMatch]:
        results: list[MarketMatch] = []
        for left in left_markets:
            candidates = [self.compare(left, right) for right in right_markets]
            if candidates:
                results.append(max(candidates, key=lambda item: item.confidence))
        return sorted(results, key=lambda item: item.confidence, reverse=True)

