from __future__ import annotations

import argparse
import asyncio

from .replay import replay_raw, summarize_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(prog="latency-trader")
    subparsers = parser.add_subparsers(dest="command", required=True)
    summary = subparsers.add_parser("summarize", help="count raw and normalized JSONL records")
    summary.add_argument("path")
    replay = subparsers.add_parser("replay", help="rebuild local books from raw JSONL")
    replay.add_argument("path")
    args = parser.parse_args()

    if args.command == "summarize":
        for key, value in sorted(summarize_jsonl(args.path).items()):
            print(f"{key}: {value}")
        return

    books = asyncio.run(replay_raw(args.path))
    for venue, venue_books in books.items():
        print(f"{venue.value}: {len(venue_books)} synchronized/local books")
        for market_id, book in venue_books.items():
            print(
                f"  {market_id}: bid={book.best_bid} ask={book.best_ask} "
                f"synchronized={book.synchronized}"
            )


if __name__ == "__main__":
    main()

