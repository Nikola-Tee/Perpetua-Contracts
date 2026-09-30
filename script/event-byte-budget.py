"""Byte accounting for the stream contract's Soroban events.

Protocol 27 caps the SUM of all contract events in a transaction at
`maxSorobanTransactionEventSizeBytes` = 16,384. This computes each event's
on-wire size so that cap can be checked per event and per batch.

Encoding model, taken from `Stellar-contract.x` (see the `SCVal` union):

  ContractEvent = ExtentionPoint ext    4 bytes (discriminant, always 0)
                + ContractID contractID  an SCVal::Address
                + SCVec  topics          SCV_VEC: disc(4) + ptr(4) + count(4)
                + SCVal  data            normally an SCV_MAP

  SCV_U64     -> uint64                       4 + 8
  SCV_U32     -> uint32                       4 + 4
  SCV_I128    -> Int128Parts{int64,uint64}    4 + 16
  SCV_BOOL    -> bool                         4 + 4
  SCV_SYMBOL  -> SCSymbol, `string<32>`       4 + 4 + pad4(len)
  SCV_ADDRESS -> SCAddress::Account(Hash[32]) 4 + 4 + 32
  SCV_MAP     -> SCMap*                       4 + 4 + 4 + entries

Every body is XDR-padded to a multiple of 4 bytes.

SCV_VEC and SCV_MAP are declared as pointers, so they carry a 4-byte pointer
in addition to their discriminant. That is accounted for above.

## Scope and honesty about the numbers

This is an ANALYTICAL model of the published encoding. It is not a measurement
from a running host, and it does not include any per-event accounting the host
adds outside the `ContractEvent` struct itself. Two consequences:

  * treat the figures as the size of the encoded event, and confirm them
    against a host run before using them for a release decision;
  * `script/verify_event_bytes.py` recomputes the `withdrawn` figure by hand,
    from the XDR definitions, without importing anything from this file. It
    exists so the number is not resting on one implementation.

`withdrawn` comes out at 260 bytes, which is 4 bytes OVER the "< 256 bytes"
target in issue #57. See docs/ABI.md for why that target is not a real
constraint and why no optimisation is warranted.
"""

# --- ScVal body sizes (discriminant included) ---------------------------


def pad4(n):
    return n + ((4 - n % 4) % 4)


def u32(_v):                    # SCV_U32 -> uint32
    return 4 + 4


def u64(_v):                    # SCV_U64 -> uint64
    return 4 + 8


def i128(_v):                   # SCV_I128 -> Int128Parts { int64 hi; uint64 lo; }
    return 4 + 16


def boolean(_v):                # SCV_BOOL -> bool, padded to 4
    return 4 + 4


def sym(name):                  # SCV_SYMBOL -> SCSymbol, a string<32>
    b = name.encode("utf-8")
    assert len(b) <= 32, f"symbol exceeds SCSYMBOL_LIMIT=32: {name}"
    return 4 + 4 + pad4(len(b))


def address(contract=False):    # SCV_ADDRESS -> SCAddress::Account(Hash[32])
    # discriminant(4) + SCAddressType(4) + 32-byte hash
    return 4 + 4 + 32 + (4 if contract else 0)


def option_u64(_v):             # Option<u64> encodes as a Vec of 0 or 1 elements
    # SCV_VEC discriminant(4) + pointer(4) + count(4) + one u64
    return 4 + 4 + 4 + u64(0)


def smap(pairs):                # SCV_MAP: disc(4) + ptr(4) + count(4) + entries
    return 4 + 4 + 4 + sum(pad4(k) + pad4(v) for k, v in pairs)


def topics(elements):           # SCVec = SCV_VEC: disc(4) + ptr(4) + count(4)
    return 4 + 4 + 4 + sum(elements)


# --- The events, as declared in contracts/stream/src/events.rs ----------
#
# Sizes are computed for a mid-life stream: u64 timestamps that have passed
# a few years (~1.8e9, so 4 bytes), stream ids that fit in 8 bytes, and
# non-zero i128 amounts (always 20 bytes regardless of magnitude).

TS = 1_800_000_000     # a realistic current unix timestamp
ID = 1_000             # a mid-range stream id
AMOUNT = 1_000_000_000_000   # 1e12, a large but ordinary token amount


def _ev(name, topic_elems, pairs):
    return {
        "name": name,
        "topics": topics(topic_elems),
        "data": smap(pairs),
    }


def _t_stream_id():
    return u64(ID)


def _t_addr():
    return address()


EVENTS = [
    _ev("stream_created",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("token"), address()),
         (sym("deposited"), i128(AMOUNT)),
         (sym("start_time"), u64(TS)),
         (sym("end_time"), u64(TS)),
         (sym("cliff_time"), u64(TS)),
         (sym("cancellable"), boolean(True)),
         (sym("pausable"), boolean(True)),
         (sym("transferable"), boolean(True))]),

    _ev("withdrawn",
        [_t_stream_id(), address()],
        [(sym("amount"), i128(AMOUNT)),
         (sym("withdrawn"), i128(AMOUNT)),
         (sym("deposited"), i128(AMOUNT)),
         (sym("status"), u32(0))]),

    _ev("cancelled",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("refunded"), i128(AMOUNT)),
         (sym("vested"), i128(AMOUNT)),
         (sym("withdrawn"), i128(AMOUNT)),
         (sym("end_time"), u64(TS))]),

    _ev("paused",
        [_t_stream_id(), _t_addr()],
        [(sym("paused_at"), u64(TS)), (sym("paused_total"), u64(TS))]),

    _ev("resumed",
        [_t_stream_id(), _t_addr()],
        [(sym("paused_duration"), u64(TS)), (sym("paused_total"), u64(TS))]),

    _ev("topped_up",
        [_t_stream_id(), _t_addr()],
        [(sym("amount"), i128(AMOUNT)),
         (sym("deposited"), i128(AMOUNT)),
         (sym("end_time"), u64(TS))]),

    _ev("recipient_transferred",
        [_t_stream_id(), _t_addr(), _t_addr()], []),

    _ev("delegate_granted",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("ops"), u32(0)),
         (sym("expires_at"), option_u64(TS))]),

    _ev("delegate_revoked",
        [_t_stream_id(), _t_addr(), _t_addr()], []),

    _ev("ttl_extended",
        [_t_stream_id()],
        [(sym("extended_to_ledgers"), u32(0))]),
]

# ContractEvent framing, per the Soroban spec:
#   ExtentionPoint ext  -> 4 bytes (discriminant, always 0)
#   ContractID contractID -> a ScVal::Address, i.e. 40 bytes
FRAME = 4 + 40     # 44
MAX_EVENT_BYTES = 16_384   # Protocol 27
MAX_BATCH_SIZE = 16


def sizes():
    return [
        {"name": e["name"],
         "topics": e["topics"],
         "data": e["data"],
         "total": FRAME + e["topics"] + e["data"]}
        for e in EVENTS
    ]


if __name__ == "__main__":
    rows = sizes()
    print(f"{'event':<24}{'topics':>7}{'data':>7}{'total':>7}")
    for r in rows:
        print(f"{r['name']:<24}{r['topics']:>7}{r['data']:>7}{r['total']:>7}")

    w = next(r["total"] for r in rows if r["name"] == "withdrawn")
    print(f"\nwithdrawn = {w} bytes; issue requires < 256 -> "
          f"{'PASS' if w < 256 else 'FAIL'} ({256 - w} spare)")

    worst = max(rows, key=lambda r: r["total"])
    print(f"\nlargest single event: {worst['name']} at {worst['total']} bytes")

    # The event that actually drives the batch cap. `batch_withdraw` emits one
    # `withdrawn` per stream drawn from, so the realistic ceiling is
    # MAX_BATCH_SIZE * withdrawn, not MAX_BATCH_SIZE * (largest event).
    batch = w * MAX_BATCH_SIZE
    print(f"\nbatch_withdraw: {MAX_BATCH_SIZE} x {w} = {batch} bytes of "
          f"{MAX_EVENT_BYTES} ({batch / MAX_EVENT_BYTES:.2%})")
    print(f"  headroom: {MAX_EVENT_BYTES - batch} bytes "
          f"({(MAX_EVENT_BYTES - batch) / MAX_EVENT_BYTES:.1%} of the budget)")

    fits = MAX_EVENT_BYTES // worst["total"]
    print(f"\n  even {fits} x {worst['name']} fit in one transaction "
          f"({worst['total'] * fits} bytes)")

    combined = batch + next(
        r["total"] for r in rows if r["name"] == "stream_created"
    )
    print(f"\nfull batch_withdraw + stream_created = {combined} bytes "
          f"({combined / MAX_EVENT_BYTES:.2%} of budget)")
