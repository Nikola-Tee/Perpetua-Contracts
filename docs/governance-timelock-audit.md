# Governance Timelock Bypass Audit

Scope: `contracts/governance/src/lib.rs` (`FluxoraGovernance`) — can a governed
target be mutated **without** passing the timelock?

Every claim below is enforced by a test in the same file. Static tests
(`test_static_audit_*`) parse the compiled source with `include_str!("lib.rs")`
and pin the shape of the dispatch graph; dynamic tests drive real proposals
against `MockTarget`, a stand-in governed contract whose only setter requires
the governance contract's authorization, so "was the target touched?" is
observed as state rather than inferred.

Line references are to `contracts/governance/src/lib.rs` at the commit that
introduced the audit.

## 1. Reachability: one door, one key

```
transaction
   │
   ├─ any of the 32 entrypoints ──► governance state only
   │      (no cross-contract call exists in the impl block)
   │
   └─ execute(executor, proposal_id)        src/lib.rs:1239
          └─ dispatch_call(target, calldata) src/lib.rs:1331  (free fn, :262)
                 └─ env.invoke_contract(...) src/lib.rs:267-331
```

* `test_static_audit_contract_impl_never_invokes_a_contract_directly` — the
  whole `impl FluxoraGovernance` block contains **zero** `invoke_contract`
  calls. All ten cross-contract invocations live in `dispatch_call`, a free
  function outside the block, so a transaction cannot name it.
* `test_static_audit_execute_is_the_only_dispatch_site` — `dispatch_call(` is
  called exactly once in the entire block, from `execute`. Every other one of
  the 32 entrypoints is asserted, individually, not to contain it.
* `test_static_audit_entrypoint_inventory_is_complete` — the audited list of 32
  entrypoints must match the contract exactly. A new or renamed entrypoint
  breaks the audit instead of escaping it.
* `test_no_backdoor_entrypoint_is_reachable` — five plausible bypass names
  (`bypass`, `dispatch`, `forcecall`, `raw_call`, `force_exec`) are invoked
  against a live contract and must all fail to resolve.

`CallData` (`:226`) is a closed enum: an unrecognized payload is
`InvalidCalldata` (`:263`), and a decoded variant can only reach the fixed set
of target functions in `dispatch_call`. There is no "raw" variant and no
pass-through of an attacker-supplied function symbol, so dispatch cannot be
steered to an arbitrary method on an arbitrary contract.

## 2. The gate stack in `execute`

All gates run before the dispatch, and the two state writes that make execution
idempotent also happen before it (CEI).

| # | Gate | Line | Error on failure |
|---|---|---|---|
| 1 | `executor.require_auth()` | 1240 | host auth error |
| 2 | in-flight reentrancy flag | 1252 | `ReentrancyGuard` |
| 3 | proposal must exist | 1261 | `ProposalNotFound` |
| 4 | status must be `Queued` | 1263 | `ProposalCancelled` / `AlreadyExecuted` / `QuorumNotReached` |
| 5 | hard max age (`created_at + 30d`) | 1271 | `ProposalExpired` |
| 6 | signer set unchanged since proposal | 1276 | `InvalidSignerGeneration` |
| 7 | quorum snapshot present and weight met | 1283 | `QuorumNotReached` |
| 8 | **timelock from quorum** (`quorum_at + 48h`) | 1297 | `TimelockNotMet` |
| 9 | **proposal-level eta** | 1302 | `TimelockNotMet` |
| 10 | post-eta grace window | 1311 | `ProposalExpired` |
| — | CEI: status `Executed`, `executed = true`, persisted | 1316-1318 | — |
| — | `Executing` flag raised | 1328 | — |
| — | `dispatch_call` — the only interaction | 1331 | — |

* `test_static_audit_execute_dispatches_after_every_gate` — every gate token
  (including both timelock checks) is asserted to occur textually **before**
  the dispatch site. Reordering the body to dispatch first, or dropping a gate,
  fails the test.
* `test_static_audit_execute_applies_cei_before_dispatch` — the `Executed`
  status write and the `Executing` flag must precede the dispatch too;
  otherwise `execute` would be reentrant (#47).

Two timelock checks are performed. Gate 8 recomputes
`quorum_reached_at + GOVERNANCE_TIMELOCK_SECONDS` from the `QuorumReachedAt`
snapshot (`:1296`, helper at `:1873`). Gate 9 reads the `proposal.eta` that
`approve` stamped at quorum (`:1155`). They are the same instant by
construction, so gate 9 is a redundancy rather than a second, longer delay: it
is the check that fails if the snapshot is missing/stale *and* independently if
a proposal was created or stored without an `eta` (a `0` eta otherwise reads as
"executable since the epoch"). Neither is skippable, and neither can be moved
by the executor.

## 3. Entry point inventory (32)

| Class | Entrypoints | `require_auth`? |
|---|---|---|
| Bootstrap | `init` | no — see residual risks |
| Proposal lifecycle | `propose`, `approve`, `execute`, `cancel_proposal` | yes |
| Signer/admin config | `set_admin`, `set_emergency_guardians`, `set_threshold`, `set_vote_weight`, `add_signer`, `remove_signer`, `update_threshold`, `set_grace_period` | yes |
| Permissionless maintenance | `prune_expired_proposals` | no — deletes only already-dead proposals |
| Views | `get_proposal`, `get_proposal_eta`, `grace_period`, `get_proposal_status`, `is_proposal_in_status`, `proposal_count`, `get_signers`, `get_admin`, `get_emergency_guardians`, `get_threshold`, `quorum`, `timelock_seconds`, `max_proposal_age_seconds`, `get_quorum_info`, `is_executable`, `is_signer`, `vote_weight`, `get_proposals_by_id_range` | n/a (read-only) |

`test_static_audit_state_mutating_entrypoints_require_auth` asserts
`require_auth` in each of the 12 state-mutating entrypoints.

The two unauthenticated non-view entrypoints are deliberate and bounded:

* `init` — one-time bootstrap; the deployment is expected to initialize in the
  same transaction or via a factory. Noted as a residual risk below.
* `prune_expired_proposals` — deletes a proposal only when it is neither
  executed nor cancelled **and** it is past both `created_at + 30d` and
  `eta + grace_period`. Every such proposal is already refused by gates 5/10,
  so pruning cannot remove anything executable, and it emits no target call.

## 4. Bypass attempts and what stops them

| Attempt | Result | Test |
|---|---|---|
| Execute at quorum, before the timelock | `TimelockNotMet`; target untouched | `test_negative_execute_before_timelock_leaves_target_untouched` |
| Execute at `quorum + timelock - 1` (off-by-one probe) | `TimelockNotMet`; target untouched | `test_negative_execute_one_second_before_timelock_leaves_target_untouched` |
| Execute a single-approval proposal | `QuorumNotReached`; target untouched | `test_negative_execute_without_quorum_leaves_target_untouched` |
| Cancel, then execute | `ProposalCancelled`; target untouched | `test_negative_cancelled_and_executed_proposals_cannot_reach_target` |
| Re-execute an executed proposal | `AlreadyExecuted`; target not touched twice | `test_negative_cancelled_and_executed_proposals_cannot_reach_target` |
| Wait past the grace window, then execute | `ProposalExpired`; target untouched | `test_negative_expired_proposal_cannot_reach_target` |
| Outsider proposes / approves | `NotASigner`; no quorum | `test_negative_non_signer_cannot_propose_or_approve` |
| Admin proposes, executes early, or is made emergency guardian | `NotASigner`, then `TimelockNotMet`, then `ProposalCancelled` | `test_negative_admin_cannot_bypass_the_timelock` |
| A third-party contract calls the target directly | rejected by the target's own controller check | `test_negative_rogue_contract_cannot_mutate_target_directly` |
| Call a backdoor entrypoint | no such function | `test_no_backdoor_entrypoint_is_reachable` |
| — (control) execute after the timelock | target mutated exactly once | `test_positive_execute_is_the_only_path_that_mutates_the_target` |

`test_negative_rogue_contract_cannot_mutate_target_directly` is the one test
that runs in an environment **without** `mock_all_auths()`. Under
`mock_all_auths` a `require_auth` check is a no-op, so the negative case would
be unobservable there. Real governed targets in this protocol are administered
by the governance contract, so the same property holds on-chain: a rogue
contract cannot supply the governance contract's authorization.

## 5. Residual risks (not fixed by this issue)

1. **`init` is front-runnable in principle.** `init` (`:705`) rejects a second
   call with `AlreadyInitialized` but is not bound to a deployer or factory, so
   whoever transacts first chooses the admin and the signer set. Deployment
   must initialize atomically (or the contract must gain a deployer check);
   that is a separate change.
2. **Executor authorization is per-address, not role-based.** Anyone may
   execute a proposal that has already cleared every gate. This is intentional
   — the timelock is the security boundary, and a permissionless executor
   cannot shorten it — but it does mean a third party can choose the execution
   moment inside the grace window.
3. **The proposal target is chosen at propose time.** Governance trusts its
   signers to point at the right contract; the timelock buys the community time
   to notice a malicious target, it does not restrict which contracts may be
   governed. The audit confirms the target is only ever reached through
   `execute`, not that it is the intended target.
4. **`CallData::Noop` performs no cross-contract call**, so a `Noop` proposal
   exercises the full gate stack without touching state. That is by design
   (it is the fixture the existing test suite uses) and is a no-op, not a
   bypass.
5. **The timelock duration is a compile-time constant**
   (`GOVERNANCE_TIMELOCK_SECONDS`, `:15`, 48h) and cannot be lowered by an
   admin or proposal. Grace period, in contrast, is admin-settable
   (`set_grace_period`, `:1520`) — it can only narrow the post-eta window, so
   it cannot be used to execute earlier.
