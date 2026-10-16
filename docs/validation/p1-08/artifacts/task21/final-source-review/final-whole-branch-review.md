# P1-08 final whole-branch source review

Reviewed source: backend `71b59fe9c5388b168dde45d54cfb7ead834106e7..02a16b7750f4af34b52abbccf929fe4a87c95cb3`; mobile `8414de8323fbf16bbca2c29fc7cd3b563023267d..1553c8bd511a0654b684beac51bd5c2ee073164d`. Both HEADs matched these endpoints when inspected. B means `D:/Eurith/worktrees/p1-08-backend`; M means `D:/Eurith/worktrees/p1-08-mobile`. File references below are relative to those exact checkouts; line numbers refer to these source versions.

## Coverage and method

One fresh reviewer; no helper agents, second opinions, suite reruns, builds, database actions, network requests, or device actions. Reviewed the supplied code-reviewer rubric, global constraints, latest final-review preparation, binding design and decisions, approved implementation plan, and execution rulings F01–F46 plus the unnumbered ruling. Used the fixed whole-branch diff packages in source-area passes, then targeted current-file reads for integration questions. The source review is independent of the previous task acceptance reports. Existing tests and evidence were read, not executed by this reviewer.

Reviewed the handwritten analytics kernel, admission and historical normalization, snapshots and body journal, shared write paths, relevant routing/schema/serialization changes, CSV/import changes, calendar obligations and active-calendar selectors, repository loading, publication/state/rebuild, report adaptation, mobile transport/compact codec/persistence, offline serialization/reconciliation, owner fences, record previews, caches, charts, muscle views, comparison views, and application mounts. Inspected meaningful regression tests around compact transport, cache and record admission, sync, block context, writes and reports. Auxiliary changed consumers were inspected through their changed hunks rather than claiming a complete audit of every pre-existing implementation. Generated fixture cells and large logs were not exhaustively line-reviewed.

Verified the two generated compact assets by metadata, count, byte length and SHA-256 against their manifest: exercise 10,848,196 bytes, SHA-256 `49e4f6f2e662a40661d0873b3f32195b193071e7e0de8db67b70a4f5382aab87`; comparison 10,860,270 bytes, SHA-256 `67aac668ae288b83c803c0eeeb9877985aa30826476f9f3891922483acae563e4`. Each contains 100 display points retaining 100,000 observations. These are artifact bytes, not a substitute for the separately recorded HTTP byte counts.

## Strengths

- The load/e1RM policy is additive and centralized, preserves unknown context, distinguishes one-implement repetitions from tonnage repetitions, and keeps the existing prescription formula separate.
- Historical facts, journal UUIDs, canonical acknowledgements and immutable report versions have substantial integration coverage. Ownership checks are present at the relevant server lookups and mobile cache/session boundaries.
- Revision/generation publication, explicit reconstruction, deterministic resume and both readiness fences are materially stronger than merely attaching a version label to computed numbers. Exact-type publication traversal still visits distinct nested states.
- F45 removes duplicated A/B history loading while retaining independent projections. Compact transport remains opt-in, includes all observations, supports old-server/full-response compatibility, and binds compact cache data to the Firebase owner.
- The source supports the documented feature-off initial maintenance sequence: the CLI inventories without applying; explicit apply calls rebuild independently of the read flag; reconstruction is explicit and initial-only; resume carries the reconstruction cursor; mark_dirty is not disabled with the feature flag. This source check is not a production execution claim.
- Evidence documents baseline failures and native/performance limitations rather than relabeling them as all-green acceptance.

## Critical

None established. The Important findings below prevent source approval; absence of a Critical label is not release approval.

## Important

### I1 — P1: Feature-off canonical metadata stops subsequent ordinary workout sync

**Location:** M/src/offline/syncEngine.ts:257–261; B/api/services/anomaly_stats.py:107–113,143.

**Trigger and effect:** With the default-false server flag, sync an ordinary set, receive its canonical response, then change notes/sets or finish that workout. The server's legacy scalar review still writes an `anomaly_review` object even for an ordinary non-anomalous set. The client retains that object. The next sync now classifies the entire snapshot as analytics context, fetches capabilities, and skips the whole workout when `write_enabled` is false. Ordinary legacy edits remain pending indefinitely. Existing analytics-bearing workouts also cannot sync ordinary edits during a rollback.

**Evidence:** The gate tests the presence of any snapshot/default/load snapshot/**anomaly_review**, not whether this operation introduces novel context. Canonical reconciliation carries the server metadata. The server explicitly emits the review in its feature-off branch. This contradicts F12's preservation of existing snapshots and legacy edits. The current disabled-new-mode regression does not exercise first canonical ACK followed by an ordinary edit.

**Bounded fix:** Gate genuinely new unsupported analytics inputs, not replay of acknowledged context or derived review metadata. Permit supported legacy edits while preserving the existing snapshots; do not strip facts or silently downgrade newly entered load modes. Add focused first-ACK/edit/finish and rollback replay cases, retaining the new-mode rejection case.

### I2 — P2: Applying a body snapshot discards the server's captured phase

**Location:** B/api/services/analytics/writes.py:58–72; B/api/services/analytics/snapshots.py:143–147; B/api/routers/workout_center.py:744.

**Trigger and effect:** Start a workout in a known saved deload phase with an explicit body-mass snapshot that does not supply `is_deload`/`phase_source` (both optional schema defaults). `capture_workout_snapshot` correctly derives the trusted phase, but the next assignment replaces that result with the submitted object containing null phase fields. Similarly, a body-only snapshot replacement can clear an established phase. Subsequent analytics sees a versioned snapshot and does not use the legacy saved-phase fallback, so deload sessions can enter strength-change and intensity-reference samples.

**Evidence:** The helper call's return is discarded and `workout.analytics_snapshot = value` overwrites its result. The center establishes session phase before applying context. The snapshot schema permits omitted phase fields; repository `_deload` treats an existing snapshot as authoritative. F18 explicitly requires trusted saved phase capture and preservation during body corrections.

**Bounded fix:** Keep server-captured phase/provenance for new snapshots and preserve established phase during body corrections. Apply validated body changes to that canonical snapshot rather than replacing it wholesale with caller defaults. Add a known-deload start with a body-only snapshot and a correction preserving phase; verify downstream exclusion, not just JSON shape.

### I3 — P2: Repeated instances of one exercise can announce a false live PR

**Location:** M/features/analytics/hooks/useWorkoutAnalyticsRecords.ts:43; M/features/analytics/records.ts:17–22.

**Trigger and effect:** A workout contains the same catalog exercise twice. Its pre-workout best is 100; the first instance produces e1RM 110 and a later instance produces 105. Each workspace passes only its own sets to the collector, while both seed from the same baseline excluding the whole active workout. The later 105 is incorrectly announced as an e1RM PR despite the earlier 110 in the same workout. This differs from the server's exercise-wide record chronology.

**Evidence:** The hook resolves a catalog exercise ID but calls `collectAnalyticsRecordHits(sets, ...)` on only the supplied instance's sets. The collector updates its thresholds only while traversing those sets. Local historical admission explicitly excludes the active workout and does not fill this gap.

**Bounded fix:** Build the chronological current-workout record stream across all instances of that catalog exercise, preserving each set's actual context and stable order, then expose hits for the requested workspace. Test two instances with an earlier higher result, equal values, and applicable gym/admission filtering.

### I4 — P2: Block muscle drilldown silently changes the selected period and gym

**Location:** M/features/analytics/components/PeriodMusclePanel.tsx:14; M/features/analytics/components/BlockComparisonPanel.tsx:28; M/app/(tabs)/progress.tsx:798.

**Trigger and effect:** Select a muscle in an older block or in side A/B of a gym-filtered comparison, then select a contributing exercise. Navigation carries only `exerciseId`. Progress reads only that route parameter and uses its own existing/default analytics query, often 90 days and all gyms. The user is no longer inspecting the dates/gym behind the number they tapped and cannot reliably verify that block result.

**Evidence:** `openAnalyticsExercise` discards block identity, period and gym; both comparison sides share this callback. The current navigation test asserts the incomplete route, so its passing result does not establish context retention. Binding spec sections 4, 12 and 15 require one filter context through map, details and source inspection.

**Bounded fix:** Carry the originating block/side and gym (or an explicit resolved query) into exercise drilldown and consume those parameters before rendering. Preserve the original context when opening its source workout. Add a historical block plus non-default gym navigation test, checking the actual query at the destination.

### I5 — P2: Required selected-exercise/muscle comparison evidence is not presented

**Location:** M/features/analytics/components/BlockComparisonPanel.tsx:18–28; M/features/analytics/components/BlockAnalyticsPanel.tsx:21–33; M/features/analytics/components/ExerciseAnalyticsPanel.tsx:82–85.

**Trigger and effect:** In A/B comparison, select a muscle or one of its contributing exercises to understand why its result differs. Selection changes the muscle percentage/contribution list, but all adjacent sets/week, tonnage, intensity and RIR remain block-wide aggregates. There is no selected exercise/muscle A/B detail rendering the required local measures. Direct/indirect muscle-volume channels and strength sample/method disclosure are also absent from the reviewed analytics views, although the response contains these carriers. An exercise chart alone does not identify the actual start/end strength sample.

**Evidence:** BlockAnalyticsView reduces exercise changes to one average and reads `data.volume`, `data.intensity`, and `data.effort`. Muscle selection does not feed that component. No reviewed analytics component consumes `volume.muscles`, contribution observations, strength method/workout IDs, or equivalent sample disclosure. Spec sections 8, 11, 12 and 15 require these inspectable results; this is missing product behavior, not a demand for another invented metric or a different averaging policy.

**Bounded fix:** Render a selected detail from the already loaded A/B resources: local exercise results and weekly workload/effort, explicit muscle direct/indirect channels, strength method and the source workouts used. Keep adherence/body labeled as global context. Do not fabricate physical muscle tonnage by multiplying kilograms by muscle weights. Add a comparison with different exercise compositions where local metrics demonstrably differ from the block total.

### I6 — P2: Unlinked extra sessions disappear from block adherence context

**Location:** B/api/services/analytics/context.py:64–66,142–143.

**Trigger and effect:** Finish a manual/unplanned workout within a block's dates with no `training_block_id`. Its actual sets/load count in the block's date window, but `extra_workouts` remains zero because extras reuse `in_period`, which requires matching block ownership. The user sees extra workload without the required extra-session count. The same issue affects an unlinked session carrying a different historical block ID within the selected actual window.

**Evidence:** Planned-obligation attribution correctly needs the F33 date-and-ownership rule. The extra-session branch incorrectly applies that same ownership predicate to finished workouts rather than using their actual dates. The spec includes unplanned actual sessions in the block window and reports additional sessions separately; fixing this must not revive superseded F32 semantics for planned days.

**Bounded fix:** Count eligible unlinked extras by the resolved actual date window, independently of planned-obligation ownership, while retaining proven linkage/transfer exclusions. Add a block-period test with owned planned days and a date-contained workout with null block linkage; assert both actual load and extras while planned/completed counts stay unchanged.

### I7 — P2: Analytics cancellation and timeout stop at response headers

**Location:** M/api/analytics.ts:9–11,27; M/api/client.ts:85–86,220.

**Trigger and effect:** A large analytics response sends headers but stalls during its body, or the user changes filters while the body is downloading. The shared fetch helper has already cleared its timeout and removed the caller's abort relay. `response.text()` can continue indefinitely; the new 90-second analytics option does not bound this phase. The screen can remain loading without reaching retry, and obsolete large downloads can continue alongside newer queries.

**Evidence:** This follows directly from control flow: cleanup occurs when fetch returns the Response, before body consumption. Owner/render fences prevent stale publication but do not stop the body transfer. The newly retained ~11 MB responses make this an actual supported path, not an imagined giant request. This is a source-level risk, not a claim of a reproduced React Native hang.

**Bounded fix:** Keep analytics request cancellation and a bounded timeout alive through body consumption, cleaning up afterward. This can be an analytics-scoped client option so unrelated request semantics need not change. Test a response whose headers resolve immediately but body stalls, and an abort after headers; verify prompt rejection and no cache publication.

### I8 — P2: New immutable reports convert unavailable adherence into zero

**Location:** B/api/services/reports/metrics.py:432–433.

**Trigger and effect:** Generate a new report for a period whose known calendar has only rest/blackout days and no planned working obligations, but which has an extra completed workout. The analytics kernel correctly has no adherence percentage. The v3 report adapter stores `rate=0` when `planned_days == 0` and there is no incomplete-calendar reason. The report presents a definitive zero adherence despite no denominator, and the misleading value becomes an immutable snapshot.

**Evidence:** The adapter explicitly chooses `0.` for zero planned days instead of retaining the analytics unavailable result. This violates the single calculation/unknown-versus-zero contract. An incomplete calendar test does not cover a complete calendar with zero obligations.

**Bounded fix:** Return null when the planned denominator is zero, or derive the normalized rate from the canonical non-null percentage. Add a complete rest-only calendar with an extra session and verify the newly created v3 report retains unavailable adherence. Preserve existing stored reports.

### I9 — P2: Body comparison conceals recorded-date fallback provenance

**Location:** M/features/analytics/components/BlockAnalyticsPanel.tsx:38–40.

**Trigger and effect:** A confirmed explicit anthropometry value has no `measured_on` and is selected near a block boundary using its permitted local `recorded_at` fallback. The UI displays its date exactly like a known measurement date. The user cannot tell that the measurement date is uncertain when judging a before/after change.

**Evidence:** The rendering uses only `m.before/after.date` and value, ignoring `date_source` and `date_estimated` already supplied by the server. F29 and spec section 13 explicitly require the “record date” quality label. The numeric selection is not being challenged.

**Bounded fix:** Label each fallback endpoint with localized recorded-date/quality text while showing the actual date and value. Test mixed endpoints: one measured date and one recorded-date fallback.

## Minor

### M1 — P3: Fresh compact responses are hydrated twice

**Location:** M/features/analytics/hooks/useAnalytics.ts:24–28.

`writeAnalyticsCache` already returns hydrated data; `readAnalyticsCache` immediately hydrates the same large resource again solely to obtain invalidation/update metadata. This adds avoidable synchronous allocation and work on the fresh-response path. Return metadata with the admitted write or use a metadata-only accessor. Verify that one network result needs one hydration and that invalidation metadata remains accurate. Existing Node adapter timing suggests this is material work, but it is not native frame-time proof. Remove this identified duplication before final performance measurements.

### M2 — P3: Combined diagnostic flags corrupt the legacy URL

**Location:** B/docs/validation/p1-08/artifacts/task21/profile_retained_history.py:60–64.

With `--unprofiled --compact` and no analytics-only `--case`, the script inserts a legacy URL without a query string and then appends `&encoding=compact-v1`. The suffix becomes part of the exercise path parameter and the legacy request fails instead of providing a diagnostic comparison. Add encoding only to analytics cases and build query strings correctly. Existing analytics-only diagnostic results are not invalidated by this separate invocation bug.

### M3 — P3: Remaining body/load presentation bypasses localized formatting

**Location:** M/features/analytics/components/WorkoutBodyMassField.tsx:35,40; M/features/analytics/components/BlockAnalyticsPanel.tsx:39.

The body-mass field prints raw `weightUnit` and JavaScript numeric interpolation rather than the existing localized unit/decimal helpers. A height observation falls back to the literal `height` because its label does not use an explicit localized height mapping. Use existing display helpers and the proper translated body label; apply the same bounded cleanup to the load-field unit labels. This is presentation polish, not a unit conversion defect.

### M4 — P3: An uncached exercise error is announced twice

**Location:** M/features/analytics/components/ExerciseAnalyticsPanel.tsx:41–43.

When no ready cached resource exists and the request fails, both the general state label and the explicit error branch render the same message. Keep one accessible error announcement and one retry action; retain the stale-data behavior for cached failures.

## Named deferred-item triage

- **Static old-schema fixture:** B/tests/test_p108_migrations.py:54 mutates SQLAlchemy's private `_columns` collection. This is a maintenance fragility, not an established product failure at the pinned dependency. Prefer a public schema-construction fixture when touching that test; do not block the source fix wave solely for this rewrite.
- **Compressed UI/tests and `any` casts:** Readability and future type-safety concerns remain. Expand code in the areas changed by the fixes, but no broad style rewrite is needed to resolve these findings. No defect is asserted merely from a cast.
- **Incompatible fresh baseline retaining an older one:** The older baseline is checked against owner/generation/exercise/gym/cutoff and is marked stale when the fresh response does not match. I did not establish a mislabeled fresh baseline from that retention behavior; preserve the original timestamp and explicit stale indication. Do not clear an otherwise compatible offline baseline solely for stylistic simplicity.
- **Raw units/body decimals, English height and duplicate error:** Concrete small UI fixes are M3/M4 above.
- **Double hydration and diagnostic URL:** Concrete bounded work is M1/M2 above, rather than deferring identified degradation into an unexplained final benchmark.
- **Warnings/i18n baseline:** The documented baseline warning count is not itself a new regression. Technical-constant exceptions must remain precise, not blanket localization exclusions.

## Recommendations

Address I1–I9 in the authorized single fix wave, with focused regressions for the triggers listed. Resolve M1/M2 before collecting final measurements; M3/M4 are small presentation repairs. Retain both database readiness fences, complete observations, owner/generation checks, compact compatibility, historical report immutability and explicit unknowns while fixing these paths. Do not trade correctness for smaller benchmark payloads.

The supplied full-run evidence is qualified, not all green: backend 2348 passed / 15 failed / 1 error includes documented baseline reproductions, later photo-configuration passes and the subsequently fixed fixture-order issue; mobile 251 suites passed / one baseline suite failed / one conditional skip includes 2637 passed / one failed / one skipped tests. Later scoped counts overlap and must not be summed into a new total. The actual wire/journal replay and history/edit/delete/rebuild/report evidence are useful, but do not cover all triggers above and do not prove native runtime behavior.

After the one source fix wave, produce the final source/hash and evidence supplement for this same review seat. Execute the required final two 30-run measurements against that final source, recording complete body/serialization costs and retained facts. The historical p95 >2× legacy results triggered diagnosis; they are neither current-source p95 nor an invented absolute response-time SLA. Current compact single-request headers/byte/SQL measurements remain diagnostic only.

## Declined to judge

- **Final performance acceptance:** The required two final 30-run runs have not happened for the final post-fix source. Historical first-source benchmarks and current single-request compact diagnostics cannot establish its p95 or acceptance. This is an open delivery gate, not a waived requirement.
- **Supported native UI/MMKV/offline/manual acceptance:** The latest authoritative evidence supplied by the parent records a successfully built current hybrid QA APK followed by a pre-JavaScript SIGSEGV in HostPlatformViewProps → RNSVGTSpanProps, with Android HOME after the crash and the private AVD stopped. I did not operate the device or independently execute that build. This establishes neither product JavaScript failure nor a supported release-equivalent pass. The bounded native attempts are exhausted; no workaround, server retry or library/host mutation is recommended without new authorization. The native gate stays open under F46.
- **Native cancellation and rendering performance:** I7 is established from source lifetime control; M1 from duplicate work. Actual device hang frequency, frame stalls and memory limits are unmeasured and are not asserted from Node/Jest adapters.
- **Exhaustive opaque-context validation:** The supplied 41-pair/312-field and compact 9-pair/93-field plus tuple-position audits, fixture checks and codec reads support the examined emitted contracts. They do not prove every arbitrary historical/imported opaque context. No blanket malformed-input safety claim is made.
- **Every generated cell, every test and unrelated pre-existing code:** Large generated assets were verified by manifest/count/hash, and meaningful tests/hunks were inspected. This review does not claim a line-by-line audit of all 27 MB of mobile package data, all logs or untouched legacy implementations.
- **Superseded or already resolved objections:** F32 is superseded by F33; the fixed extreme-date overflow is not reopened. Existing progression/fatigue formulas and intentional legacy behavior outside the additive feature are not re-scoped.
- **Deferred maintenance without demonstrated failure:** Private fixture internals, dense formatting, casts, compatible stale-baseline retention and baseline warnings receive the triage above, not invented product severity.
- **Production rollout/backup suitability:** The CLI and recipe were checked at source level only. No production migration, backup restore, deployment or operator environment was executed or approved by this review.
- **Future evidence-only commit:** The parent reports ongoing benchmark-script/native-evidence append work with no product source changes. Unreceived final deltas/hashes are not represented as inspected here and should be appended to this same review, not treated as a second broad review.

## Assessment

**Source: changes required.** Established findings: **0 Critical, 9 Important (one P1, eight P2), 4 Minor (P3)**. The architecture and evidence are substantial, but ordinary feature-off sync, trusted phase preservation, live record coherence, inspectable comparison context and report semantics have concrete integration defects.

**Release/Task21 acceptance: open independently of source fixes.** Required final performance runs and supported native acceptance are unfinished. Fixing the findings, passing focused tests, or supplying Node/Jest results does not close those gates. No merge, deployment or all-green acceptance is recommended by this report.

## Same-seat late measurement/native-evidence supplement

This is a bounded delta review appended to the same whole-branch review, not a second broad review or a re-review of I1–I9/M1–M4.

- Backend BASE `02a16b7750f4af34b52abbccf929fe4a87c95cb3`, HEAD `a058ff08c4e50298716aa6c2269482ce25cda5ed`; reviewed `review-final-measurement-backend.diff` (14,139 bytes), changing only `scripts/benchmark_analytics.py`.
- Mobile BASE `1553c8bd511a0654b684beac51bd5c2ee073164d`, HEAD `63b64a93227b4a6f8873202b8fa86f32d7eb674d`; reviewed the handwritten/evidence-summary portions of `review-final-native-evidence-mobile.diff` (3,577,327 bytes), including acceptance, rollout, F46, native README/runtime manifest, compact fixture harness and identity provenance, APK hash/embedded-proof metadata. Raw boot/build logs dominate this package and were not dumped or exhaustively re-reviewed. Both resulting HEADs were confirmed by read-only inspection.

### Bounded checks

The benchmark's default remains full transport. `--compact` alters only the two analytics URLs, both of which already contain query parameters; the legacy path remains unchanged. This is distinct from the earlier diagnostic script in M2 and does not resolve M2. Default sizes 10,000/100,000, two runs, 30 measured requests per case and a separate warmup are preserved: 360 measured requests across the six size/case combinations, plus warmups.

The new untimed compact validator checks the expected Firebase owner, resource/ready state, rules and revision agreement, each 18-position observation, unique observation count equal to the selected fixture size, registry-reference bounds, expected workout count, valid unique point references, and disjoint/exhaustive A/B halves with exact half-set/workout counts. The current fixture and its 100-day two-block split support those assertions. These are structural/accounting checks, not a replacement for the production codec's full semantic/type validation. SQL capture and limits remain applied to the actual analytics request; timing ends after `client.get` has consumed the ASGI response, before JSON/accounting work. Response bytes, SQL samples, source commit/dirty status and analytics-module SHA-256 provenance remain recorded. Legacy list payloads no longer encounter dictionary-only `.get` metadata access.

The native summaries correctly distinguish successful hybrid QA assembly from failed pre-JavaScript startup and unrun UI/MMKV/manual/authenticated/iOS acceptance. They preserve historical chronology while clearly superseding earlier pending descriptions. The committed harness/identity changes record the compact asset's owner and large-mode/cache-proof fixture; they do not claim a successful runtime observation. Small recorded evidence inspected here includes DEBUG=false/release BuildConfig, IPv4/IPv6 loopback ACCEPT followed by DROP, emulator-stop acknowledgement, and APK/embedded-proof metadata. The actual crash-stack and HOME-image interpretation remain the parent's independently inspected evidence, as explicitly attributed in the main report. No new workaround is authorized or implied.

Preparation reports compilation and AST-isolated helper checks against the actual 100k assets and a legacy list. Those checks were not rerun by this reviewer. No benchmarks, tests, app imports, DB/services, builds, network requests or device actions were performed in this supplement; the only mutation was this report append.

### Supplement assessment

**Supplement approved for the intended measurement/evidence preparation; no new Important finding established.** The original **13 findings remain unchanged: 0 Critical, 9 Important, 4 Minor**. No product fix was supplied or accepted in this delta. The earlier “unreceived future evidence-only commit” limitation is discharged only for the exact ranges and inspected scope above. Final post-fix source hashes, final repeated measurements and supported native acceptance remain outstanding. The source verdict is still **changes required**, and performance/native release gates remain **open** under F46.
