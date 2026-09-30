# Governance Batch Proposals

Scope: `contracts/governance/src/lib.rs` (`FluxoraGovernance`) — issue #51,
"batch proposals": one proposal, one timelock cycle, several governed calls.

Before this change a proposal carried exactly one `(target, calldata)` pair, so
a policy change that touched three parameters meant three proposals, three
quorum rounds and three timelocks. A batch payload makes the list the unit of
governance instead. The list is applied **in order** and **all-or-nothing**.

Every claim below is enforced by a test in the same file. Line references are
to `contracts/governance/src/lib.rs` at the commit that introduced batching.

## 1. Wire format

`Proposal` is unchanged — still `{ target, calldata }` (`:86`). Batching is a
new `CallData` variant instead of a new `Proposal` field, so stored proposals
keep their layout and existing callers keep their encoding.

```rust
pub struct ExecutionCall {   // :239
    pub target: Address,     // contract to invoke
    pub calldata: Bytes,     // XDR-encoded single-operation CallData
}

pub enum CallData {
    ...
    Batch(Vec<ExecutionCall>),   // :270
}
```

A proposer serialises the batch the same way it serialises any other payload:

```rust
let calls = vec![
    &env,
    ExecutionCall { target: factory, calldata: CallData::FactorySetCap(500_i128).to_xdr(&env) },
    ExecutionCall { target: factory, calldata: CallData::FactorySetMinDuration(1_209_600_u64).to_xdr(&env) },
    ExecutionCall { target: stream,  calldata: CallData::StreamGlobalResume.to_xdr(&env) },
];
governance.propose(&proposer, &factory, &CallData::Batch(calls).to_xdr(&env));
```

`proposal.target` is still recorded and still emitted in `ProposalCreated` /
`ProposalExecuted` as the proposal's nominal target. For a batch it is **not**
dispatched: each entry carries its own address. That keeps the event schema and
the storage layout stable for indexers, at the cost of a field that is
descriptive rather than load-bearing for batch proposals.

### Wire compatibility

`#[contracttype]` enums are encoded as a `Symbol` carrying the **variant name**,
not a positional index, so adding `Batch` leaves every already-encoded payload
decodable. `test_single_call_proposal_still_dispatches` is the control case: a
single-operation proposal still round-trips through `execute` unchanged.

## 2. Dispatch

```
execute(executor, proposal_id)                    :1330
  └─ dispatch_call(target, calldata)               :1422 → :314
       ├─ decode_calldata                           :302
       ├─ CallData::Batch(calls)
       │    ├─ empty            → InvalidCalldata
       │    ├─ len > 5          → BatchTooLarge
       │    └─ for each entry, in order:
       │         ├─ decode_calldata(entry.calldata)   → InvalidCalldata
       │         ├─ entry is itself a Batch?         → InvalidCalldata (no nesting)
       │         └─ dispatch_operation(entry.target, op)   :344
       └─ any other variant
            └─ dispatch_operation(proposal.target, op)
```

`dispatch_call` peels the batch variant off; `dispatch_operation` (:344) holds
the ten `env.invoke_contract` arms, each binding the same owned values the
single-operation path has always passed. Its `Batch` arm is unreachable
defence-in-depth and returns `InvalidCalldata`.

`Batch` is a *proposal shape*, not a new operation: the set of reachable target
functions is exactly the set reachable before this change. There is still no
raw-call variant and no attacker-supplied function symbol, so a batch cannot
reach a method the single-call path could not.

## 3. Invariants

| Property | Enforced by |
|---|---|
| A single operation still dispatches as before | `test_single_call_proposal_still_dispatches` |
| Every entry runs, in written order | `test_batch_applies_every_call_in_order` |
| One proposal can span several targets | `test_batch_spans_multiple_targets` |
| A trapping entry unwinds the entries before it | `test_batch_reverts_every_call_when_one_fails` |
| The proposal stays executable after such a failure | `test_batch_reverts_every_call_when_one_fails`, `test_batch_is_retryable_after_a_transient_failure` |
| A successful batch cannot be replayed | `test_batch_is_retryable_after_a_transient_failure` |
| The timelock gates the whole batch | `test_batch_still_respects_the_timelock` |
| An empty batch is refused | `test_empty_batch_is_rejected` |
| Exactly 5 entries run, 6 are refused before any target call | `test_batch_is_bounded_by_max_batch_calls` |
| Batches do not nest | `test_nested_batch_is_rejected` |
| An undecodable entry fails the batch before any target call | `test_batch_entry_with_undecodable_calldata_is_rejected` |
| A 5-entry worst-case payload fits `MAX_CALLDATA_BYTES` | `test_max_batch_payload_fits_within_max_calldata_bytes` |

### Atomicity is the host's, not ours

No compensation logic is implemented, and none is needed: every write made by
an entry — including the entries that already succeeded — belongs to the same
transaction, so an error returned by any entry, or a trap in any target,
unwinds all of them. A partially applied batch is not reachable.

This is asserted against observable state, not inferred. `BatchTarget` (:4276)
records the order in which governance applied its setters and requires the
governance contract's authorization on every setter, so "was the target
touched?" is read back from storage. After a failing entry:

* the healthy target still reads its pre-execution value,
* its ordered log is still empty,
* the proposal is not marked executed,
* no `proposal_executed` event exists,
* `is_executable` is still true.

`FlakyTarget` (:4382) is switched into a trapping state to model a transient
downstream lock. It is the shared fixture for #51's atomicity claim and #53's
recovery claim.

Note the interaction with the CEI ordering in `execute` (:1406-1426): the
proposal is marked executed *before* dispatch. Because a failed dispatch is a
host rollback, that mark is rolled back with everything else — the failure
path leaves no half-executed proposal behind. The `Executing` reentrancy flag
raised immediately before dispatch is rolled back for the same reason.

## 4. Limits

| Bound | Value | Error |
|---|---|---|
| Entries per batch | `MAX_BATCH_CALLS = 5` (`:31`) | `BatchTooLarge = 27` (`:185`) |
| Bytes per proposal payload | `MAX_CALLDATA_BYTES = 4_096` (`:21`) | `CalldataTooLarge = 10` |
| Batches per payload | 1 | `InvalidCalldata = 20` |

The entry bound is checked before the first target call, so an over-limit batch
costs a decode and nothing else. The nesting rule exists for the same reason:
without it the bound could be side-stepped by hiding calls one level down.

Both bounds are checked against the *payload*, not the number of storage
writes, so a batch cannot be used to inflate a single transaction beyond what
`MAX_CALLDATA_BYTES` already allows.

### Cost envelope — what was and was not measured

Measured here, in the test suite:

* payload size. `test_max_batch_payload_fits_within_max_calldata_bytes` encodes
  five copies of the largest operation governance can dispatch
  (`FactorySetStreamWasmHash`, a 32-byte hash) and asserts the result is within
  `MAX_CALLDATA_BYTES`, so a batch at the entry bound cannot trip the payload
  bound.

Not measured here, and stated as such:

* **gas.** The suite runs in the SDK test environment, which does not meter
  gas. The expected production shape is roughly linear in entry count (one
  `invoke_contract` per entry plus the loop), so a 5-entry batch costs
  approximately five dispatches in one transaction instead of five transactions
  — but the absolute number has not been measured and no figure is claimed
  here. Measuring it requires a network or a gas-metering harness.
* **rent.** Batching does not change the storage layout, so rent per proposal is
  unchanged; entry targets and payloads live inside the single `calldata`
  bytes already stored. See `docs/gas.md` for the general cost discussion and
  the #52 TTL work for retention.

If a future entry type is large enough to threaten `MAX_CALLDATA_BYTES`, the
bound to lower is `MAX_BATCH_CALLS`; the payload test is the tripwire.

## 5. Residual risks

* **A batch is all-or-nothing, so it is all-or-nothing *late*.** If entry 4 of
  5 fails, the transaction unwinds and nothing lands, but the signer set has
  already waited out the timelock. Failures therefore cost time, not state. The
  retry path is covered by `test_batch_is_retryable_after_a_transient_failure`.
* **Ordering is a governance decision, not a guarantee.** Entries run in the
  order written; a proposer can sequence a policy update in a way a reviewer
  must read twice. `test_batch_applies_every_call_in_order` pins the order, and
  the ordered log on `BatchTarget` exists so off-chain tooling can do the same.
* **`proposal.target` is not the dispatched target for a batch.** Anything that
  reads the proposal record to decide what will run must read the batch payload
  instead. Indexers that key on `ProposalExecuted.target` will see the nominal
  target; the full payload is in the same event.
* **Gas is unmeasured.** See above. A 5-entry batch is one transaction; if the
  combined cost ever approaches a block limit, `MAX_BATCH_CALLS` is the knob.
