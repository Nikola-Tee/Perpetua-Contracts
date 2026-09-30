"""Independent hand-calculation of the `withdrawn` event, written from the
stellar-xdr definitions, to cross-check evsize.py. Deliberately does NOT
import or reuse anything from that script.
"""
# From Stellar-contract.x:
#   union SCVal switch (SCValType type)
#   case SCV_U64:  uint64 u64;                 -> 4 + 8  = 12
#   case SCV_I128: Int128Parts i128;           -> 4 + 16 = 20
#   case SCV_U32:  uint32 u32;                 -> 4 + 4  = 8
#   case SCV_SYMBOL: SCSymbol sym;             -> string<32>: 4 + 4 + pad4(len)
#   case SCV_ADDRESS: SCAddress address;       -> 4 + 4 (type) + 32 (Hash)
#   case SCV_MAP:  SCMap *map;                 -> 4 + 4 (ptr) + 4 (count) + entries
#   SCV_VEC (topics) -> 4 + 4 (ptr) + 4 (count) + elements
# ContractEvent = ExtentionPoint ext (4) + ContractID contractID (ScVal::Address)

FRAME = 4 + (4 + 4 + 32)        # 44
U64 = 4 + 8                    # 12
ADDR = 4 + 4 + 32              # 40
U32 = 4 + 4                    # 8
I128 = 4 + 16                  # 20


def pad4(n):
    return n + ((4 - n % 4) % 4)


def sym(name):
    return 4 + 4 + pad4(len(name))


def entry(key, val_bytes):
    """An SCMapEntry holds two SCVals, each already a multiple of 4."""
    return pad4(key) + pad4(val_bytes)


# topics: stream_id (u64), recipient (address)
topics = 4 + 4 + 4 + U64 + ADDR

# data: a map of four entries
data = 4 + 4 + 4
data += entry(sym("amount"), I128)
data += entry(sym("withdrawn"), I128)
data += entry(sym("deposited"), I128)
data += entry(sym("status"), U32)

total = FRAME + topics + data
print(f"FRAME  = {FRAME}")
print(f"topics = {topics}")
print(f"data   = {data}")
print(f"total  = {total}")
print()
print(f"sym sizes: amount={sym('amount')} withdrawn={sym('withdrawn')} "
      f"deposited={sym('deposited')} status={sym('status')}")
print(f"val sizes: i128={I128} u32={U32} u64={U64} addr={ADDR}")
assert total == 260, f"expected 260, got {total}"
print("\nMATCHES evsize.py (260)")
