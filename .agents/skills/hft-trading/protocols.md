# Exchange Protocol Notes

General note: exact message formats are venue-specific and versioned — always confirm against the specific exchange's published spec/rulebook rather than assuming a format from memory. The notes below are about *how to build a parser*, not verbatim byte layouts for any particular venue.

## FIX (Financial Information eXchange)

- Text-based, tag=value pairs delimited by SOH (0x01), used widely for order entry (less common for market data at the top end of HFT, more common for algo/broker connectivity).
- Session layer (heartbeats, sequence numbers, logon/logout) is separate from the application layer (order messages, execution reports) — implement/test them independently.
- Sequence number handling: on a gap, send a resend request rather than dropping messages; on receiving a `PossDupFlag`, dedupe by sequence number and don't reprocess.
- FIX parsing is a good candidate for a hand-rolled tag-value scanner (avoid a generic XML/JSON-style parser) since it's a simple, fixed grammar — this matters if FIX is on a latency-sensitive path.

## Binary market data protocols (ITCH-style, OUCH-style, and vendor binary feeds)

- Typically fixed-length or length-prefixed binary messages, often over multicast UDP for market data (feed handlers must tolerate packet loss/reordering — a sequence number in the message or a separate sequenced feed with retransmission channel handles this).
- Two common designs to know when reading a spec: (1) an itch-style *order-by-order* feed where you reconstruct the book from add/execute/cancel/replace messages referencing an exchange-assigned order ID, and (2) a depth-of-book snapshot+delta feed where you get periodic full snapshots plus incremental updates.
- For order-entry binary protocols (ouch-style), messages are typically request/response with an explicit acknowledgment before the order is live — don't treat "sent" as "accepted."
- When implementing a parser: validate message length before reading fields, never trust a length field blindly (bounds-check against buffer size), and fuzz-test against truncated/malformed input since a crashed feed handler is worse than a dropped message.

## General parser implementation notes

- Zero-copy where possible: parse in place over the receive buffer rather than copying into intermediate structures, especially for market data at high message rates.
- Endianness: confirm byte order per venue (many exchange binary protocols are big-endian/network order for headers even when payload fields differ) — don't assume host endianness.
- Version negotiation: exchanges evolve protocol versions; a production parser should reject/flag an unrecognized version rather than silently misparsing it as the wrong format.
- Separate the "decode bytes into a typed message" step from "apply message to book/order state" — keeps the parser unit-testable against raw captured packet data independent of book logic.

## Where to get authoritative specs

Point users to the exchange's own technical documentation portal for exact specs (e.g., Nasdaq TotalView-ITCH spec, Cboe/NYSE binary feed specs, individual FIX dictionary versions) rather than reproducing exact byte layouts from memory — these are versioned and change, and getting a field offset wrong in a rebuilt spec is worse than pointing to the source.
