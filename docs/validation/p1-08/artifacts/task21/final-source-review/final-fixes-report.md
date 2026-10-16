# Final source-fix wave — P1-08

All I1–I9 and M1–M4 are addressed in this single implementer wave. This is an implementation/evidence report for the parent's scoped re-review, not independent source approval or release acceptance.

## Exact committed scope

- Backend BASE `a058ff08c4e50298716aa6c2269482ce25cda5ed` → HEAD `5f4aa578eedbe3abe49a50c5a72acedea97d8542`.
- Mobile BASE `63b64a93227b4a6f8873202b8fa86f32d7eb674d` → HEAD `0ec9303ffb2b5a5f6de4bec05e310aa6fc673be7`.
- Both worktrees were clean after committing. Parent-owned F47/F48 are included in the mobile commit with permission. No subagents, native attempts, benchmarks, production actions, migrations, dependency changes, push or merge were performed.

## Finding verdicts and focused RED → GREEN evidence

| Finding | Verdict and implementation | Actual regression evidence |
| --- | --- | --- |
| I1 | Addressed. Persist local-only canonical ACK checkpoints; gate novel explicit analytics inputs, preserve ordinary edits/finish/replay and derived review metadata. Existing exercise context can be inherited by a newly added ordinary set during rollback, including server validation. | First-ACK/edit/finish and rollback cases failed before the gate fix; later inherited-set rejection failed before its server/client refinement. Separate genuinely new-mode rejection remains. Final sync suite passes in the 75-test run; backend write paths pass in the 63-test run. |
| I2 | Addressed. Merge supplied body fields into server-captured snapshot; preserve trusted phase/provenance and canonical body when omitted. | Known deload with body-only input/correction initially produced strength change 3% instead of unavailable; downstream strength and intensity-reference assertions now pass. Additional omitted-body test initially got null instead of canonical body, then passed. |
| I3 | Addressed. Collect every instance of the same catalog exercise using each row's saved context, then return only current-workspace hits. | Pure repeated-instance higher/equal/admission/gym cases pass. Actual repository/hook test initially announced one false later PR; now returns none with exactly one baseline request. |
| I4 | Addressed. Carry resolved dates/gym plus origin block, A/B side and muscle through exercise and source-workout routes; destination consumes query immediately. | Historical 2025 block, non-default gym and side B test asserts the actual destination API query. Source workout route asserts retained context. Previous incomplete exerciseId-only route assertion replaced with exact full context. |
| I5 | Addressed under F48. Selected muscle shows canonical normal/drop direct/indirect total and weekly channels plus explicitly labelled local A/B exercise workload, intensity, RIR/drift/coverage and strength method/source samples. Global body/adherence remain separate. | Comparison RED lacked selected-local evidence. GREEN different-composition fixture asserts local 2 versus 6 sets/week and local tonnage/intensity/RIR instead of block totals; B-only contributor and canonical null reasons remain visible. Actual dated strength-sample IDs/method/source routes asserted. |
| I6 | Addressed. Actual extras use resolved actual dates independently of planned ownership; proven linked IDs still excluded. | Null and different-block unlinked sessions initially yielded zero extras. GREEN asserts actual load 17, extra 1 and unchanged owned obligations 4/4/100. |
| I7 | Addressed. Opt-in analytics body-lifetime timeout/abort retains cleanup through response text; unrelated client behavior stays header-scoped. | Immediate headers/stalled body and abort-after-headers initially remained unsettled. GREEN checks rejection and real query hook no-cache publication; existing owner fence and ordinary-client tests pass. |
| I8 | Addressed. New v3 report rate is null for zero planned denominator. | Complete rest-only calendar plus extra sessions initially produced 0; corrected actual report regression now retains null. Existing report/immutability cases pass. |
| I9 | Addressed. Per-endpoint localized recorded-date fallback label preserves actual dates/values. | Mixed measured/recorded endpoint test failed missing label, then passed. |
| M1 | Addressed. Metadata-only owner-fenced cache accessor avoids second hydration after admitted write. | Hydration spy initially counted 2; GREEN counts 1, exact expanded fixture equality and updatedAt/staleness retained. |
| M2 | Addressed. Diagnostic request-case builder applies compact encoding only to analytics URLs. | New isolated script test initially failed missing builder; GREEN asserts exact combined-flag legacy and both analytics URLs. No diagnostic network requests executed. |
| M3 | Addressed. Existing localized decimal/unit helpers and explicit translated height mapping. | Russian body decimal/unit and mixed body-height label tests failed before fix and pass afterward; load controls remain covered. |
| M4 | Addressed. One accessible uncached alert and retry, preserving cached stale display. | Correct trigger initially rendered 2 error messages; GREEN asserts one alert/live-region error and retry. |

RED evidence was observed in tool outputs; not every initial failure has a separate saved log. Retained failure logs include `final-backend-first.log`, `final-comparison-red.log`, and `final-evidence-first.log`. The latter also records outdated route expectation and a test-label mismatch corrected to the actual translated label; these are not counted as additional product defects.

I1 test correction: `revisioned history acknowledgement boundaries › preserves a newer row edit while a full history PATCH is in flight` previously expected no subsequent sync, relying on the defective any-metadata flag gate. It now asserts that the newer weight 35 snapshot actually syncs, while preserving dirty/history-correction/phase assertions. This is separate from genuine new-mode rejection and the ACK/edit/finish/rollback regressions. Pre-upgrade `syncedOnce` records without a checkpoint defer novelty validation to the existing server canonical validator, avoiding stranded ordinary replay.

I3 ordering uses the existing set-number ordering followed by stable IDs, across the whole catalog exercise. Positive persisted IDs keep ascending order; local negative IDs are allocated decreasingly, so they sort descending to preserve local insertion order; positive IDs precede local additions at equal set number. Repeated instances and repeated set numbers are covered by the real repository/hook regression. No `updated_at` field is treated as execution time and no new timestamp contract or formula was introduced.

## Verification and retained output

Counts below overlap and MUST NOT be summed. All backend commands used `D:/Eurith/backend/.venv/Scripts/python.exe` and the guarded `W/run_backend_tests.py` against the approved private test database, with a dedicated external basetemp. No credentials were emitted.

- Backend covering: `run_backend_tests.py tests/integration/test_p108_service.py tests/integration/test_p108_reports.py tests/integration/test_p108_snapshots.py tests/integration/test_p108_write_paths.py tests/integration/test_p108_calendar_context.py tests/analytics/test_block_context.py tests/test_p108_diagnostic_urls.py -q --basetemp=D:/Eurith/p108-final-fixes-tmp` — **107 passed, 15 warnings**, 81.58s; `final-backend-covering.log`.
- After final write refinements: `run_backend_tests.py tests/integration/test_p108_write_paths.py tests/integration/test_p108_snapshots.py tests/integration/test_p108_service.py -q --basetemp=D:/Eurith/p108-final-fixes-final-tmp` — **63 passed, 15 warnings**, 67.39s; `final-backend-write-refinement.log`. Prior process 33701 expired after auth interruption; the complete redirected pytest summary was inspected, not inferred from Unknown process. Earlier completed execution had returned exit 0 before context interruption.
- Earlier report/URL subset: **13 passed** (`final-backend-report-url.log`); inherited-write subset: **19 passed** (`final-inherited-backend.log`). These overlap the above.
- Mobile covering: `node node_modules/jest/bin/jest.js features/analytics/__tests__ src/offline/__tests__/syncEngine.test.ts src/offline/__tests__/analyticsContext.test.ts src/offline/__tests__/analyticsHttpAcceptance.test.ts api/__tests__/analyticsDeadline.test.ts api/__tests__/analyticsCompactHttp.test.ts api/__tests__/clientTimeout.test.ts api/__tests__/client-localization.test.ts --runInBand --silent` — **27 suites / 223 tests passed; 2 conditional suites/tests skipped**, 23.236s; `final-mobile-covering.log`.
- Mobile final refinements: `node node_modules/jest/bin/jest.js api/__tests__/analyticsDeadline.test.ts api/__tests__/clientTimeout.test.ts api/__tests__/client-localization.test.ts features/analytics/__tests__/exercisePanel.test.tsx features/analytics/__tests__/blockComparison.test.tsx src/offline/__tests__/syncEngine.test.ts --runInBand --silent` — **6 suites / 75 tests passed**, 8.078s; `final-mobile-refinement.log`.
- `node node_modules/typescript/bin/tsc --noEmit --pretty false` — exit 0; `final-tsc.log` empty as expected.
- `node tools/i18n-audit/check-user-facing-strings.mjs` with explicit changed analytics UI component paths plus `app/(tabs)/progress.tsx` and `app/workout/history.tsx` — exit 0; `final-i18n.log` empty. Initial no-path usage was corrected; no blanket exceptions added.
- `git diff --check` in both worktrees — passed; only Git LF/CRLF notices. Post-commit status empty in both.

Existing Pydantic deprecation and Node punycode warnings are not new failures. No blanket full-suite rerun was performed. Earlier whole-branch baseline failures remain qualified as documented in the review, not relabelled as green.

## Contracts and limits

No server DTO/compact tuple/schema or physical aggregation formula changes. F48 uses existing numerical truth; no pooled muscle intensity/tonnage is invented and no DTO supplement is needed. Additive internal interfaces are local-only ACK metadata, optional analytics body-lifetime request flag, metadata-only cache read and navigation origin parameters. Owner/revision/publication fences, full facts, phase provenance, immutable stored reports, normal/drop semantics and existing formula policy remain intact.

Source fixes do not close Task21/release acceptance. Final two 30-run performance measurements remain unrun and unauthorized during this wave. The existing hybrid APK startup failed before JavaScript; native UI/MMKV/manual/full-auth/iOS acceptance remains unrun. Jest/Node results do not establish native cancellation, rendering or memory performance. Parent's scoped re-review is still required.

## Changed files

Backend:
- api/services/analytics/context.py
- api/services/analytics/writes.py
- api/services/reports/metrics.py
- docs/validation/p1-08/artifacts/task21/profile_retained_history.py
- tests/integration/test_p108_reports.py
- tests/integration/test_p108_service.py
- tests/integration/test_p108_snapshots.py
- tests/integration/test_p108_write_paths.py
- tests/test_p108_diagnostic_urls.py

Mobile:
- api/__tests__/analyticsDeadline.test.ts
- api/analytics.ts
- api/client.ts
- app/(tabs)/progress.tsx
- app/workout/history.tsx
- docs/validation/p1-08/execution-decisions.md
- features/analytics/__tests__/blockComparison.test.tsx
- features/analytics/__tests__/exercisePanel.test.tsx
- features/analytics/__tests__/exerciseQuery.test.tsx
- features/analytics/__tests__/loadFields.test.tsx
- features/analytics/__tests__/navigation.test.tsx
- features/analytics/__tests__/queries.test.ts
- features/analytics/__tests__/records.test.ts
- features/analytics/__tests__/workoutRecordHook.test.ts
- features/analytics/components/AnalyticsEvidence.tsx
- features/analytics/components/BlockAnalyticsPanel.tsx
- features/analytics/components/BlockComparisonPanel.tsx
- features/analytics/components/ExerciseAnalyticsPanel.tsx
- features/analytics/components/ExerciseLoadFields.tsx
- features/analytics/components/PeriodMusclePanel.tsx
- features/analytics/components/WorkoutBodyMassField.tsx
- features/analytics/hooks/useAnalytics.ts
- features/analytics/hooks/useWorkoutAnalyticsRecords.ts
- features/analytics/navigation.ts
- features/analytics/records.ts
- features/i18n/locales/en/progress.ts
- features/i18n/locales/ru/progress.ts
- features/progress/utils/bodyComposition.ts
- src/offline/__tests__/syncEngine.test.ts
- src/offline/analyticsWriteGate.ts
- src/offline/cache/analyticsCache.ts
- src/offline/syncEngine.ts
- src/offline/types.ts
- src/offline/workoutRepository.ts
