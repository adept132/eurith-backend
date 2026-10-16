# F49 bounded residual re-review

Same final-review seat. Reviewed only N1/N2, their repair delta and possible breakage introduced by that delta. The previously accepted 12 original findings remain settled; no second broad review was performed.

## Exact source

- Mobile BASE `0ec9303ffb2b5a5f6de4bec05e310aa6fc673be7` → HEAD `72e1d76614ba3e4e17d8757c7276cb52a43be9c4`.
- Backend unchanged HEAD `5f4aa578eedbe3abe49a50c5a72acedea97d8542`.
- Both current HEADs confirmed by read-only inspection.

Read the complete `final-residual-fixes-report.md`, `review-final-residual-mobile.diff`, exact public F49 and retained focused output summaries. No tests, builds, database/network/HTTP/device actions or helpers were used. No source, index or HEAD mutation; only this report was written.

## N1 — Addressed; residual I1 closed

Locations in `D:/Eurith/worktrees/p1-08-mobile`:

- `src/offline/analyticsWriteGate.ts:23–32`
- `src/offline/syncEngine.ts:258–269`
- `api/analytics.ts:13–19`

The unconditional `syncedOnce`/missing-checkpoint bypass is removed. Explicit non-null context without a checkpoint now reaches a fresh capability request. Migration may rely on server canonical novelty validation only when the response explicitly identifies schema 1, rules `p1-08-v1`, a recognized state and a boolean write flag. The helper's migration-only argument propagates a real 404 rather than turning it into the normal synthetic disabled capability. Cached enabled support is not consulted on this path. Unknown/unsupported/failed responses therefore do not reach the old scalar ACK path and cannot clear the pending context through that omission.

Freshly confirmed expanded feature-off servers may still validate a pre-checkpoint replay. The server remains responsible for rejecting genuinely novel context there; no backend validator was changed. Ordinary scalar snapshots without explicit analytics input and canonical checkpoint-bearing replay retain their existing path. Pending mass intents retain the write-enabled requirement. The default helper call still provides the established 404 fallback to other callers.

The added regressions persist/reload a no-checkpoint synced workout, block old/404, unknown and failed capability results despite previous enabled support, and verify dirty/context retention without invoking the prepared scalar ACK. Complementary cases cover canonical existing-mode replay against a compatible disabled server and pending retention after its rejection of a genuinely new mode. The latter uses a mocked 409 and is not claimed as a new live server test; expanded-server validation was already inspected in the preceding scoped review.

No new defect established in this bounded gate/helper delta.

## N2 — Addressed

Locations in `D:/Eurith/worktrees/p1-08-mobile`:

- `features/analytics/reasons.ts:1–8`
- `features/analytics/components/AnalyticsEvidence.tsx:45`
- `features/analytics/components/ExerciseAnalyticsPanel.tsx:1–2`

The existing canonical reason mapping was moved unchanged into a dependency-free helper. The exercise panel re-exports it for existing consumers, while the local evidence card imports it directly, avoiding an evidence/panel import cycle. `unknown_block_end` now renders the established unknown-end explanation instead of shortage. The regression verifies both the correct explanation and continued visibility of known sets/tonnage; it does not infer an end date or alter calculations.

No new defect established in this bounded label/helper delta.

## Evidence and limitations

Independently read the retained RED summary (**5 failed / 38 passed**) and final covering summary (**4 suites / 60 tests passed, 7.484 seconds**). The reported intermediate 43-pass result overlaps the covering run; counts are not added. Type-check and targeted i18n success are reported by the implementer with empty logs, not rerun by this reviewer. No global all-green or native-runtime claim follows from these focused results.

### Declined to judge

Final repeated performance acceptance remains pending for the frozen final source. Supported native acceptance remains open: the recorded hybrid APK failed before JavaScript, while UI/MMKV/manual/full authentication/iOS checks remain unrun. These JavaScript tests do not establish native cancellation, rendering, buffer-release or memory behavior. Production rollout, deployment, backups and untouched code were not assessed. Prior whole-branch limits and non-blocking maintenance dispositions remain in force.

## Verdict

**N1 and N2 closed. No residual Critical, Important or Minor source finding remains from this review chain.** Original I1 is now closed alongside the 12 previously accepted findings.

**Source approved within the reviewed scope at B `5f4aa578eedbe3abe49a50c5a72acedea97d8542` / M `72e1d76614ba3e4e17d8757c7276cb52a43be9c4`.** This closes the source-review gate and permits the separately authorized final measurement work; it is not approval to merge, deploy or declare Task21 complete.

**Performance/native release gates remain OPEN independently.** Final measurement evidence must be tied to the frozen source identities and reconciled as the same-review evidence supplement, without another broad review. Supported native acceptance is not waived.
