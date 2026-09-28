# Audit Entrypoint Table

This document tracks every public ABI entrypoint in `contracts/stream/src/lib.rs`.
The CI "Audit entrypoint drift check" step verifies this table against the source.

Last verified: 2026-08-29 (PR #1665)

## Arithmetic audit (§98 — silent wrapping)

There are no unchecked primitive operators on the value path. Every addition,
subtraction, multiplication and division in `contracts/stream/src/accrual.rs`
is either `checked_` (mapping failures to `Error::Overflow`), `saturating_`
(clamping at the boundary) or guarded by a creation-time domain bound:

| Op | Location | Behaviour |
|---|---|---|
| `frozen_at - paused_total` | `stream_time` | `saturating_sub`, clamps at zero |
| `end_time - start_time` | `duration` | `saturating_sub` |
| `clock - start_time` (capped) | `elapsed` | `saturating_sub` |
| `deposited * consumed` | `vested` | `checked_mul` -> `Error::Overflow` |
| `deposited / duration` | `vested` | `checked_div`, `duration == 0` short-circuit |
| `earned - withdrawn` | `withdrawable` | `checked_sub`, saturates at zero |
| `deposited - earned` | `refundable` | `checked_sub` |
| `deposited - withdrawn` | `liability` | `checked_sub` |

Backing guarantees:

* **`overflow-checks = true`** in `contracts/stream/Cargo.toml`
  (`[profile.release]`): even a missed `+`/`*`/`-` panics on overflow instead
  of silently wrapping in the deployed WASM.
* **Creation-time domain bound** (`create_stream`, lib.rs:255-271): a stream is
  rejected unless `deposit * duration` fits in `i128`, so `deposited *
  elapsed` inside `vested` can never overflow for a stream that reached
  storage. `top_up` re-establishes the same bound post-extension.
* **`u64 as i128` casts are lossless**, and `u64::MAX` fits comfortably in
  `i128`, so no `as` cast on the value path can truncate.
* **Typed, not trapped:** `test::accrual_overflow` (accrual_overflow.rs) drives
  every helper at `u64::MAX` timestamps and `i128`-ceiling deposits and asserts
  the result is `Ok(bounded)` or `Err(Error::Overflow)` — never a panic and
  never a wrap.

## Stream Contract — `fluxora_stream`

### Lifecycle

| Entrypoint | Description |
|---|---|
| `create_stream` | Create a new payment stream with deposit, schedule, and capability flags |
| `top_up` | Extend stream duration at a fixed rate (sender auth) |
| `withdraw` | Pull accrued balance; `None` = withdraw max |
| `batch_withdraw` | Atomic multi-stream withdrawal |
| `cancel` | Cancel stream, refund unvested to sender (sender auth, `cancellable`) |
| `pause` | Freeze accrual (sender auth, `pausable`) |
| `resume` | Unfreeze accrual (sender auth, `pausable`) |
| `transfer_recipient` | Change stream recipient (recipient auth, `transferable`) |

### Delegation

| Entrypoint | Description |
|---|---|
| `grant_delegate` | Grant per-operation delegation to a third party |
| `revoke_delegate` | Revoke previously granted delegation |
| `delegate_withdraw` | Withdraw on behalf of recipient via delegation |
| `delegate_cancel` | Cancel on behalf of sender via delegation |
| `delegate_pause` | Pause on behalf of sender via delegation |
| `delegate_resume` | Resume on behalf of sender via delegation |
| `delegate_top_up` | Top up on behalf of sender via delegation |
| `delegate_transfer_recipient` | Transfer recipient on behalf of recipient via delegation |

### Views (read-only)

| Entrypoint | Description |
|---|---|
| `get_stream` | Return full stream struct |
| `withdrawable_of` | Return withdrawable amount |
| `vested_of` | Return vested amount |
| `refundable_of` | Return refundable amount |
| `stream_count` | Return total stream count |
| `stream_exists` | Check if a stream ID exists |
| `get_cliff_status` | Return cliff close-time skew status |

### Maintenance (permissionless)

| Entrypoint | Description |
|---|---|
| `extend_stream_ttl` | Extend a single stream's storage TTL |
| `batch_extend_ttl` | Extend multiple streams' storage TTLs |

## Reentrancy Resistance

All withdrawal and token-transfer paths follow the **Checks-Effects-Interactions**
pattern. In `apply_withdrawal` (`src/lib.rs`):

1. `stream.withdrawn` is updated and `stream.status` is possibly set to `Depleted`.
2. `storage::save_stream` persists the updated stream state.
3. `token_transfer` moves tokens to the recipient.

State is written **before** the external token contract is called. Soroban's
host forbids contract reentrancy by default, so even a malicious or buggy token
contract cannot call back into `FluxoraStream` mid-transfer. If the token
transfer fails (insufficient balance, deauthorized recipient, host trap), the
Soroban host unwinds the entire transaction and no storage write is committed.

This ordering is verified by `withdrawal_atomicity.rs`, which engineers two
failure modes — SAC `set_authorized(recipient, false)` and an always-panicking
token contract — and asserts that stream state and token balances are
byte-for-byte identical before and after the failed call.

## Governance Contract - `fluxora_governance`

Whether a governed target can be mutated without passing the timelock is
audited separately in [governance-timelock-audit.md](./governance-timelock-audit.md).
Summary of that audit:

- `execute` is the only entrypoint that reaches a target. The
  `impl FluxoraGovernance` block contains no `invoke_contract` call at all; the
  single dispatch site is the free function `dispatch_call`, reached from
  `execute` after auth, reentrancy, status, max-age, signer-generation, quorum,
  both timelock checks and the grace window.
- 32 entrypoints are inventoried in that document; the 12 state-mutating ones
  are asserted to `require_auth`, and the only unauthenticated non-view
  entrypoints are `init` and `prune_expired_proposals`.
- Negative tests execute real proposals against a governed mock target and
  assert the target's state is untouched for every bypass attempt (early
  execution, one second early, missing quorum, cancelled, executed, expired,
  non-signer, admin, rogue direct call).
