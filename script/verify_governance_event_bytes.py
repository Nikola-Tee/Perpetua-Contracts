"""Independent hand-check of vote_cast / proposal_queued sizes, from the
stellar-xdr definitions, importing nothing from governance-gas-model.py.
"""
FRAME = 4 + 40          # ext(4) + contractID ScVal::Address(40)
ADDR = 4 + 4 + 32       # SCV_ADDRESS -> SCAddress::Account(Hash[32])
U32 = 4 + 4
U64 = 4 + 8


def pad4(n):
    return n + ((4 - n % 4) % 4)


def sym(name):
    return 4 + 4 + pad4(len(name))


def svec(elems):
    return 4 + 4 + 4 + sum(elems)          # disc + ptr + count


def smap(pairs):
    return 4 + 4 + 4 + sum(pad4(k) + pad4(v) for k, v in pairs)


def entry(name, val):
    return sym(name), val


# vote_cast: topics (vote_cast, proposal_id, approver)
#   symbol "vote_cast" | u32 proposal_id | Address approver
vote_topics = svec([sym("vote_cast"), U32, ADDR])
vote_data = smap([
    entry("proposal_id", U32),
    entry("voter", ADDR),
    entry("approval_count", U32),
    entry("timestamp", U64),
])
vote_cast = FRAME + vote_topics + vote_data

# proposal_queued: topics (proposal_queued, proposal_id)
q_topics = svec([sym("proposal_queued"), U32])
q_data = smap([
    entry("proposal_id", U32),
    entry("quorum_reached_at", U64),
    entry("executable_after", U64),
    entry("threshold", U32),
])
queued = FRAME + q_topics + q_data

print(f"vote_cast:       topics={vote_topics} data={vote_data} total={vote_cast}")
print(f"proposal_queued: topics={q_topics} data={q_data} total={queued}")
print(f"10 signers:      {vote_cast * 10} + {queued} = {vote_cast * 10 + queued}")
print()
assert vote_cast == 284, f"expected 284, got {vote_cast}"
assert queued == 232, f"expected 232, got {queued}"
print("MATCHES governance-gas-model.py (284 / 232)")
