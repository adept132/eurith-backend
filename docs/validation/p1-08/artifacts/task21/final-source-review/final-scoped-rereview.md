# P1-08 — one scoped final-fix re-review

## Scope and evidence

Same final-review seat; one scoped re-review of the supplied fix delta, not another whole-branch review. Requirements are the original I1–I9/M1–M4 and the exact public F48 interpretation. B = `D:/Eurith/worktrees/p1-08-backend`; M = `D:/Eurith/worktrees/p1-08-mobile`.

- Backend BASE `a058ff08c4e50298716aa6c2269482ce25cda5ed` → HEAD `5f4aa578eedbe3abe49a50c5a72acedea97d8542`; package `review-final-fixes-backend.diff`, 22,822 bytes.
- Mobile BASE `63b64a93227b4a6f8873202b8fa86f32d7eb674d` → HEAD `0ec9303ffb2b5a5f6de4bec05e310aa6fc673be7`; package `review-final-fixes-mobile.diff`, 141,131 bytes.

Both HEADs were verified. Read the complete implementer report, handwritten changes and changed tests, with targeted current-file context. One targeted read of the published backend baseline's `api/schemas/sync.py` answered whether an older server actually rejects unknown analytics fields (see N1). No tests, builds, database/HTTP/network/device actions or helper agents were used. Only this report was written. No source/index/HEAD changes were made.

The fixes are substantially aligned with the findings. The remaining compatibility issue below is narrower than the original feature-off sync failure. No formula, public DTO or compact-tuple change was observed in the scoped delta.

## In-order verdicts on the original 13 findings

### I1 — Partially addressed; residual N1 below

**Locations:** M/src/offline/analyticsWriteGate.ts:8–35; M/src/offline/syncEngine.ts:258; M/src/offline/workoutRepository.ts:647,706,1246; B/api/services/analytics/writes.py:37–41.

Canonical checkpoints now distinguish ordinary replay from novel input. Derived unconfirmed anomaly metadata no longer forces the feature gate; ordinary edits and finish can pass when the feature is disabled. New sets can inherit an acknowledged exercise context, and the backend permits exactly that established default while still rejecting a different explicit mode. Checkpoints record the canonical response even when a newer local edit remains pending; they do not overwrite the newer local edit. The changed history-ACK test now checks the pending ordinary weight actually reaches sync rather than depending on the previous defective skip.

The new ACK/edit/finish, rollback, inherited-set and genuinely new-mode cases exercise the intended paths. However, the unconditional no-checkpoint migration bypass at line27 does not preserve old-server unsupported-mode safety. Therefore I1 is not fully closed; see N1. Current expanded-server feature-off validation itself was not weakened into accepting a different novel mode.

### I2 — Addressed

**Locations:** B/api/services/analytics/writes.py:72–80; tests/integration/test_p108_service.py:407; tests/integration/test_p108_snapshots.py:227.

The supplied body is applied to the canonical capture rather than overwriting the captured phase with optional client defaults. Existing canonical phase/provenance survive correction; omitted body fields preserve the canonical body, while explicit body null remains an explicit clear. Matching journal replay keeps established identity. The regression checks downstream strength and intensity-reference exclusion after a known-deload body-only capture and later correction, including a changed current block phase. The omitted-body regression closes the additional refinement gap.

### I3 — Addressed

**Locations:** M/features/analytics/records.ts:22,51–64; M/features/analytics/hooks/useWorkoutAnalyticsRecords.ts:43; M/features/analytics/__tests__/workoutRecordHook.test.ts:9–23.

The hook folds all instances of the catalog exercise, substitutes current workspace rows, and returns only that workspace's hits. Each row retains its own gym/load/review context. Ordering keeps set number first, ascending positive IDs, descending locally allocated negative IDs, and persisted IDs before local additions at equal set number. This matches the existing server set-number/ID policy after persistence without inventing execution timestamps. Pure tests cover higher/equal preceding results and admission/gym context; the real repository/hook test covers repeated local instances and one baseline request. This resolves the identified false later PR; it is not a claim that the data model records actual completion timestamps.

### I4 — Addressed for the reviewed drilldown flow

**Locations:** M/features/analytics/navigation.ts:3–20; components/PeriodMusclePanel.tsx:15; components/BlockComparisonPanel.tsx:29; M/app/(tabs)/progress.tsx:842–844,1132; M/app/workout/history.tsx:32,231–236.

Block/side/muscle origin and resolved date/gym query now travel together. The destination derives the incoming query during render rather than briefly showing the previous/default query. A user override is scoped to that route and owner. Source-workout and strength-sample navigation carry origin, and history offers a return action. The new navigation test checks the route, parsed context and actual panel API request; it does not mount the entire Progress screen. The destination's actual source wiring was inspected separately. No authenticated native-navigation pass is inferred.

### I5 — Addressed under F48, with new Minor N2 below

**Locations:** M/features/analytics/components/AnalyticsEvidence.tsx:15–68; components/BlockComparisonPanel.tsx:29; components/BlockAnalyticsPanel.tsx:48; components/ExerciseAnalyticsPanel.tsx:88.

Selected muscle detail consumes canonical direct/indirect normal/drop totals and weekly channels. Its contributor list uses exercise-local muscle-volume membership, so secondary, B-only and insufficient-strength contributors are not lost merely because they are outside the common strength set. Each exercise card labels its own side/name and uses its own workload, tonnage, intensity, RIR/drift and coverage. Strength methods and dated start/end source workouts are inspectable. Existing common-set muscle strength and added/removed/excluded lists remain visible, and adherence/body retain their global label.

This satisfies F48 without inventing pooled muscle intensity/RIR or physical muscle kilograms and without substituting block totals for local values. Different-composition tests assert local 2 versus 6 weekly sets, local tonnage/effort, B-only inclusion and null reference values. The new card's hardcoded strength-reason label creates the separate Minor N2; the requested local evidence itself is now present.

### I6 — Addressed

**Locations:** B/api/services/analytics/context.py:142–143; tests/integration/test_p108_service.py:437.

Extras now use actual local finish dates independently of block linkage, while already linked workouts remain excluded. The planned-obligation `in_period` ownership predicate was not changed, preserving F33. The two linkage variants (null/other block) assert date-window actual load, one extra session, and unchanged 4/4/100 owned obligations. No superseded F32 semantics were reintroduced.

### I7 — Addressed at source/control-flow level

**Locations:** M/api/client.ts:43–101,248–262; M/api/analytics.ts:27; M/api/__tests__/analyticsDeadline.test.ts:63–93.

Analytics explicitly opts into a lifetime covering response text. Timer and caller relay now remain active through consumption. The abort race rejects even when a test/runtime text promise ignores abort; the same signal is also delivered to fetch. Cleanup occurs after that lifetime. Other API requests and text export retain their previous header-scoped default. Parsing/publication happens only after successful body completion.

Tests cover immediate headers/stalled text, caller abort after headers, rejection without ready/cache publication, header timeout, owner replacement and ordinary-client behavior. This establishes bounded caller completion and abort propagation in the reviewed implementation; it does not prove native transport actually releases every buffer after cancellation.

### I8 — Addressed

**Locations:** B/api/services/reports/metrics.py:433; tests/integration/test_p108_reports.py:216.

The new v3 adapter returns null for zero planned days, preserving unavailable adherence. The regression creates a report with complete rest-only calendar and two actual sessions, then checks the stored v3 payload. Existing stored reports and legacy calculation paths are untouched by this one-line adapter change; covering report tests include immutability behavior.

### I9 — Addressed

**Locations:** M/features/analytics/components/BlockAnalyticsPanel.tsx:25,41; features/analytics/__tests__/blockComparison.test.tsx:66.

Each endpoint independently displays the localized recorded-date quality label when `date_estimated` or `recorded_at_local` indicates fallback. Dates/values are retained. The mixed known-measured/recorded endpoint test verifies that only the fallback endpoint receives the label.

### M1 — Addressed

**Locations:** M/src/offline/cache/analyticsCache.ts:42–49; M/features/analytics/hooks/useAnalytics.ts:27; features/analytics/__tests__/queries.test.ts:93.

The post-write read is metadata-only and checks owner/session before and after access. It no longer validates/hydrates the just-written graph again. The hydration spy checks one expansion, exact full-resource equality and update/stale metadata. This still parses the stored envelope to retrieve metadata; it is not a claim of zero allocation or native frame-time acceptance.

### M2 — Addressed

**Locations:** B/docs/validation/p1-08/artifacts/task21/profile_retained_history.py:40–48,70; B/tests/test_p108_diagnostic_urls.py:7–12.

The explicit case builder adds encoding only to analytics URLs and leaves the legacy path unchanged under combined flags. The focused test asserts all three exact URLs without making requests. No new diagnostic timing result was generated by this fix.

### M3 — Addressed

**Locations:** M/features/analytics/components/WorkoutBodyMassField.tsx:37,42; components/ExerciseLoadFields.tsx:28; M/features/progress/utils/bodyComposition.ts:48; both progress locale files:9.

Displayed body decimals and units use existing localization helpers, load labels use localized units, and height has an explicit localized mapping. Russian decimal/unit and height-label tests cover the reported presentation defects. The numeric input parser remains permissive for comma/period entry; no fact conversion policy changed.

### M4 — Addressed

**Locations:** M/features/analytics/components/ExerciseAnalyticsPanel.tsx:43–45; features/analytics/__tests__/exerciseQuery.test.tsx:55.

The general state message excludes the error branch. One accessible error and one retry remain, including cached/stale behavior. The uncached regression checks one message and an alert.

## New breakage / exact residual list

### Critical

None established in this delta.

### Important — N1 / residual I1, P2: No-checkpoint migration bypass can send novel modes to an unsupported old server

**Location:** M/src/offline/analyticsWriteGate.ts:25–27, reached from M/src/offline/syncEngine.ts:258–262. Related ACK completion: M/src/offline/workoutRepository.ts:1632–1651.

**Concrete trigger:** A previously synced workout is loaded from a pre-checkpoint client store (`syncedOnce=true`, `acknowledgedAnalytics` absent). Before it receives a fresh canonical checkpoint, the user introduces explicit load context while capability support is available/cached. On reconnection the server is an older endpoint without analytics support (for example a server rollback). There is no pending mass intent, only a novel load mode.

**Effect/evidence:** The new early return declares every such snapshot non-novel, so sync never performs the capability request. The published older server's `SyncWorkoutSnapshot`/`SyncExerciseSnapshot`/`SyncSetSnapshot` are ordinary Pydantic BaseModels with no analytics fields or `extra='forbid'`; they ignore those extra fields and can acknowledge the ordinary scalar workout. The normal ACK path can then clear dirty when there is no mass intent. Thus the new context was sent to an unsupported server and can be treated as successfully synchronized without being retained server-side. The comment's assumption that the server will perform canonical novelty validation is true only for the expanded server. This is exactly the old-server compatibility boundary the new-mode gate is meant to protect.

The added genuine-new-mode test first obtains a checkpoint and therefore cannot catch this migration branch. The original feature-off expanded-server ACK/edit/finish case is fixed; this is a narrower compatibility regression introduced by its fallback.

**Bounded recommendation:** For no-checkpoint records, distinguish confirmed expanded-server canonical validation from genuinely unsupported/unknown capability before allowing explicit analytics-bearing snapshots to bypass novelty checks. Preserve ordinary legacy edits and existing canonical context on a supported feature-off server; retain novel input pending on an old server rather than stripping it or trusting omission as acknowledgement. Add a persisted no-checkpoint workout with a new mode, unsupported capabilities/old response omission, and the complementary expanded-server feature-off ordinary replay case. This report does not authorize another automatic fix wave.

### Minor — N2, P3: New local card mislabels an unresolved block end as insufficient workouts

**Location:** M/features/analytics/components/AnalyticsEvidence.tsx:44.

**Trigger/effect:** Select a contributing muscle/exercise in a closed block whose actual end is unknown. The canonical partial resource intentionally sets `summary.strength_reason='unknown_block_end'`, while preserving independent known workload. The new card renders “Not enough workouts” for every non-null strength reason, even when the history is long and the missing end is the actual cause. The existing exercise view has a reason mapper that already distinguishes this condition.

**Bounded recommendation:** Use the canonical reason mapping (or a shared non-circular helper) instead of hardcoding shortage, and add the unknown-end local-card case. Do not invent a block end or hide the available metrics. This is a new display defect, not a failure of F48's approved aggregation interpretation.

## Evidence qualification and out-of-scope observations

Read the retained completed summaries: backend covering **107 passed / 15 warnings / 81.58s**, later write refinement **63 passed / 15 warnings / 67.39s**; mobile covering **27 suites / 223 tests passed, 2 conditional skips / 23.236s**, refinement **6 suites / 75 tests passed / 8.078s**. These counts overlap and are not summed. The expired backend process identifier is not treated as evidence of exit status; the redirected completed summary is the evidence independently read here. Type-check/i18n exit0 are reported by the implementer/parent with empty logs, not rerun or independently reconstructed by this reviewer. Initial RED evidence is partly retained and partly reported tool output; no claim that every initial failure is archived.

The old qualified full-suite baseline failures remain qualified. Focused passes do not relabel the entire repositories as all green. Tests that manually connect parsed navigation to a panel do not prove authenticated application navigation. The no-body-publication test uses a JavaScript adapter, not a device.

No new critique of untouched branch areas is issued. Original deferred private fixture internals, formatting/casts, compatible stale-baseline retention and baseline warnings retain F47's non-blocking disposition. No pooled muscle DTO is requested beyond F48. The targeted published-schema read above was solely to resolve N1's old-server behavior, not a renewed baseline audit.

### Declined to judge

Final p95 and performance acceptance remain unmeasured for this fixed source: the final 360 measured requests/two 30-run sets have not started. Native startup remains the recorded pre-JavaScript hybrid-APK failure; native UI/MMKV/manual/full authentication/iOS acceptance remains unrun. No native cancellation, buffer-release, rendering or memory guarantee follows from these tests. No production backup/migration/deployment readiness, universal opaque-input safety or untouched-code correctness is assessed. These limits remain visible delivery gaps, not waivers.

## Scoped assessment

**Original findings:** 12 addressed; I1 partially addressed because of N1. I5's requested evidence is implemented under F48, with the separate new Minor N2.

**Exact residual list:** **0 Critical, 1 Important (N1/P2), 1 Minor (N2/P3)**. N1 is also the reason I1 remains open; do not count it twice. No other residual original finding or new defect is established in this scoped review.

**Source verdict: changes required / parent adjudication required for the listed residuals.** This completes the one authorized scoped re-review; it does not schedule another fix wave or broad review.

**Task21/release verdict: still open independently.** Final repeated performance evidence and supported native acceptance remain unfinished even if the residual source findings are subsequently resolved or explicitly adjudicated.

