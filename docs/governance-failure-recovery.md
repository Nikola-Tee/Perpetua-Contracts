# Governance Execution Failure Recovery

Scope: `contracts/governance/src/lib.rs` (`FluxoraGovernance`) — issue #53,
what happens when a dispatched target call fails.

## 1. The guarantee

If a call dispatched by `execute` reverts or traps, the **entire transaction
fails**. That is not a mechanism this contract implements — it is the host's
transaction semantics, and the contract's job is to make sure it is arranged so
that the host's rollback is the *right* rollback.

Concretely, a failed execution leaves:

* the proposal `Queued`, with `executed == false`;
* the target contract completely unmodified;
* no `proposal_executed` event;
* the `Executing` reentrancy flag cleared;
* the proposal still executable for the remainder of its grace window.

The proposal can therefore simply be retried once a transient downstream
condition clears.

## 2. Why the ordering in `execute` gives this for free

`execute` follows Checks-Effects-Interactions: it writes `status = Executed`
*before* invoking the untrusted target.

```
execute(executor, proposal_id)
  ├─ guard: reject if Executing            (reentrancy, #47)
  ├─ load proposal, check Queued / age / signer generation
  ├─ load QuorumInfo, check weight / timelock / eta / grace
  ├─ EFFECT : status = Executed, executed = true   <─ written first
  ├─ set Executing = true
  ├─ INTERACTION : dispatch_call(...)              <─ may trap
  ├─ set Executing = false
  ├─ dispatched?                     <─ propagates a typed error
  └─ publish proposal_executed
```

Every write above the trap — the CEI marker and the `Executing` flag — is part
of the same transaction as the target call, so the host unwinds them along with
whatever the target did. The marker that says "executed" is therefore *never*
observable in a failed execution. That is the whole of the recovery story: CEI
plus transaction atomicity.

Note that the typed-error path (`dispatched?`) and the trap path both end in a
failed transaction, so both roll back identically. There is no path where the
proposal is marked executed but the target call did not complete.

## 3. What this does *not* do

The contract does not distinguish a transient failure from a permanent one. A
permanently broken target is simply retried until the grace window closes and
the proposal becomes `ProposalExpired`. That is a deliberate trade:

* A retry policy would need to record failure counts, which would itself be
  state that a failed transaction cannot write — so it could never be updated
  on the failure path, which is precisely the path that needs it.
* The grace period already bounds the damage. It is the existing, tested
  staleness control from #46, and it needs no new state.

## 4. Tests

All in `contracts/governance/src/lib.rs`, using `FlakyTarget` — a mock that
traps on `set_cap` while switched into a failing state, and requires the
governance contract's authorization so no state changes except through
`execute`.

| Test | Asserts |
| --- | --- |
| `test_single_call_target_failure_reverts_execution_state` | a single-call proposal whose target traps leaves the target untouched, `executed == false`, status `Queued`, and emits no `proposal_executed` |
| `test_failed_execution_leaves_the_proposal_executable` | the proposal is still executable right after the failure *and* at `deadline - 1`, so the failed attempt did not consume the window |
| `test_single_call_target_failure_is_recoverable_on_retry` | once the lock clears the retry applies the payload exactly once, and exactly one `proposal_executed` event exists |
| `test_failed_dispatch_does_not_latch_the_reentrancy_guard` | a *different*, healthy proposal still executes after a failed one — the `Executing` flag was rolled back rather than left set |
| `test_unrecovered_failure_expires_at_the_end_of_the_grace_period` | execution is allowed exactly at the grace deadline and refused with `ProposalExpired` after it |

`test_failed_dispatch_does_not_latch_the_reentrancy_guard` is the one that
guards against a real outage: if a trap could leave `Executing` set, every
subsequent execution of *any* proposal would fail with `ReentrancyGuard`,
turning one transient failure into permanent governance paralysis.

The batch-payload equivalents already exist from #51
(`test_batch_reverts_every_call_when_one_fails`,
`test_batch_is_retryable_after_a_transient_failure`); this issue adds the
single-call path and the grace-window boundary, which the batch tests do not
reach.
