# Governance Proposal Storage and Rent

Scope: `contracts/governance/src/lib.rs` (`FluxoraGovernance`) — issue #52,
retention strategy for historical proposal entries.

## 1. The problem

A proposal occupies three persistent entries:

| Key | Contents |
| --- | --- |
| `DataKey::Proposal(id)` | the record: approvals, calldata, timestamps, status |
| `DataKey::QuorumReachedAt(id)` | the `QuorumInfo` snapshot (`reached_at`, `threshold`) |
| `DataKey::ProposalApprovalIdx(id)` | `Map<Address, bool>` for O(1) duplicate-approval checks |

Persistent entries cost rent for as long as they stay live. Before this change
every one of those keys was bumped by a flat `PERSISTENT_BUMP_AMOUNT` of
**120 960 ledgers** on every read and write. That is fine for keeping a live
proposal from archiving, but it has two problems:

1. **Too short to be safe.** A proposal stays actionable for up to
   `MAX_PROPOSAL_AGE_SECONDS` (30 days) after `created_at`. A proposal created
   and then left untouched would be readable for only 120 960 ledgers
   (≈ 7 days at 5 s/ledger) before archiving — while still, in principle,
   executable.
2. **Paid forever.** Because the bump was unconditional, *any* read of a
   finished proposal extended its rent again. An indexer that pages through
   history, or a dashboard polling `get_proposal`, would keep every proposal
   ever created alive indefinitely. Storage cost would grow with the number of
   proposals rather than with the number of *active* ones.

## 2. The policy

Retention is now keyed on the proposal's lifecycle state.

```
                 propose / approve / queue          execute | cancel
                        │                                  │
      ┌─────────────────▼──────────────┐                   ▼
      │  ACTIVE                        │            TERMINAL
      │  Proposed / Approved / Queued  │      Executed / Cancelled
      └─────────────────┬──────────────┘                   │
                        │                                  │
        every read      │   every read                     │  one-shot
        and write       │   and write                     │  at the
                        ▼                                  ▼  transition
              extended to                             granted the
              ACTIVE_PROPOSAL_BUMP_AMOUNT              30-day floor,
              (30 days + 1 week)                       never topped up
                                                       again → decays
```

**Active** proposals get `ACTIVE_PROPOSAL_BUMP_AMOUNT`, which is
`MAX_PROPOSAL_AGE_SECONDS + 604 800` (30 days plus a week). One bump therefore
carries an entry past the last moment it could legally be read or written. The
extra week is headroom for the worst case, where the bump lands exactly at
`created_at` and the entry must then survive a full max-age window.

**Terminal** proposals get `TERMINAL_PROPOSAL_RETENTION` — 30 days — exactly
once, inside `seal_terminal_proposal`, at the transition. `bump_active_entry`
then skips them forever, so the entry decays back to the ledger minimum and its
rent goes to zero.

The 30-day figure is a **floor, not a target**. `extend_ttl` only ever raises a
TTL, so an entry that still had a longer life left over from its active bumps
keeps it; the seal guarantees the entry lives *at least* 30 more days, and
never longer. What matters for cost is that nothing extends it again.

### Host semantics that constrain the design

Three behaviours of the Soroban host drove the shape of this policy, and all
three are easy to get wrong:

* `extend_ttl(key, threshold, extend_to)` applies the extension **only when the
  entry's current TTL is at or below `threshold`**. A threshold equal to the
  target therefore makes the call a no-op for any entry that still had life
  left. `seal_terminal_proposal` therefore uses the low
  `PERSISTENT_LIFETIME_THRESHOLD`, meaning "top up if this is anywhere near
  expiring".
* `extend_ttl` on a key that does not exist is an **error**, not a no-op, so
  every key is probed with `has` first. A proposal that has not reached quorum
  has no `QuorumReachedAt` entry.
* The host rejects any `threshold > extend_to` pair before consulting the
  network limit.

### Units

Soroban's `extend_ttl` is denominated in **ledgers**, not seconds. The
`ledgers()` helper converts the wall-clock intent at the network's 5 s/ledger
cadence, so the constants read in days and the host receives ledgers. Getting
this wrong is not a rounding error: `max_entry_ttl` is 6 312 000 ledgers
(≈ 1 year), and the host clamps anything above it.

## 3. Where the policy is applied

`bump_active_entry` is the single choke point. It is reached from:

| Call site | Path |
| --- | --- |
| `load_proposal` / `save_proposal` | every mutating entry point (`approve`, `execute`, `cancel_proposal`, `propose`) |
| `get_approval_index` / `save_approval_index` | duplicate-approval detection |
| quorum snapshot, in `approve` / `execute` / `is_executable` | timelock and threshold gates |
| `get_quorum_info` | public view |
| `get_proposals_by_id_range` | bulk paging |

The two views that do not already hold a `Proposal` (`get_quorum_info`,
`get_proposals_by_id_range`) consult `proposal_is_terminal` first, so a public
read cannot be used to keep a dead proposal alive.

`save_proposal` is the only place that grants the terminal floor, which keeps
"when do we stop paying" a single decision.

## 4. Rent cost

Rent on Soroban is a minimum entry fee plus a per-ledger rate, so the cost of a
proposal is dominated by how long its entries stay live. In ledger terms, and
holding the entry sizes constant:

| Proposal state | Retention | Paid by |
| --- | --- | --- |
| Active (`Proposed`/`Approved`/`Queued`) | 30 days + 1 week, refreshed on each read/write | the signers still voting |
| Terminal (`Executed`/`Cancelled`) | 30 days, once | the protocol, at the transition |
| After the terminal window | decays to the network minimum | no one |

The change therefore moves cost from "grows forever with proposal count" to
"proportional to the number of proposals currently in flight". In practice the
active window is *longer* than the old flat bump, so a live proposal is safer
than before, and a dead one is cheaper than before.

Exact XLM figures are not quoted here: they depend on `base_reserve`, the
network's current `min_persistent_entry_ttl`, and the size of the stored
calldata, all of which are network parameters rather than contract constants.
What is pinned by tests is the ledger arithmetic, which is the part this
contract controls.

## 5. Tests

All in `contracts/governance/src/lib.rs`, asserting against the TTL the host
actually records (`testutils::storage::Persistent::get_ttl`) rather than
inferring behaviour:

| Test | Asserts |
| --- | --- |
| `test_retention_windows_stay_within_network_limits` | every bump pairs a low threshold with a larger window, and both windows fit under `max_entry_ttl` |
| `test_active_proposal_storage_outlives_its_max_age` | a `Proposed` record already carries the full active window, which exceeds the proposal's own max age |
| `test_voting_and_queuing_extend_the_active_window` | all three keys of a `Queued` proposal are at the active window |
| `test_executed_proposal_decays_instead_of_being_kept_alive` | execution leaves at least the 30-day floor, and five later reads only let it decay with the ledger |
| `test_cancelled_proposal_decays_instead_of_being_kept_alive` | same for cancellation |
| `test_paging_history_does_not_revive_terminal_proposals` | one bulk page extends the live proposal but not the cancelled one |
| `test_quorum_view_does_not_revive_an_executed_proposal` | the public `get_quorum_info` view is readable but does not extend |

The decay tests compare TTL *before and after* a ledger advance rather than
against a fixed constant, because the terminal floor is a minimum: an entry that
had a longer life left from its active bumps keeps it. What the tests pin is
the property that actually matters — that a read moves the TTL by exactly the
elapsed ledgers and nothing more.

## 6. Known limits

* A terminal proposal is readable for 30 days and then **gone**. Anything that
  needs governance history beyond that must archive it off-chain — the events
  (`proposal_created`, `vote_cast`, `proposal_queued`, `proposal_executed`,
  `proposal_cancelled`) remain in the ledger permanently and are the durable
  record.
* Because the floor is never topped up, a terminal entry cannot be resurrected
  by a keeper. That is intentional; a permissionless "revive" path would
  reintroduce exactly the unbounded growth this issue closes.
* The contract has no way to prune entries explicitly. Decay is the ledger's
  job, and the entry simply stops resolving once `live_until_ledger` passes.
