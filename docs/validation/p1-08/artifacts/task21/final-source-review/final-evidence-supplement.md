# Final same-seat evidence supplement — P1-08

## Scope and identities

Evidence-only supplement to the existing whole-branch/scoped/residual review chain. The 13 original findings and N1/N2 remain settled; no product-source review was reopened. No product changes were found in the late packages.

- Approved product: backend `5f4aa578eedbe3abe49a50c5a72acedea97d8542`; mobile `72e1d76614ba3e4e17d8757c7276cb52a43be9c4`.
- Backend evidence range: approved product BASE → `c4dbc1698d6685a9b04cc9bd26a97c6ec6717b4c` (three commits).
- Mobile evidence range: approved product BASE → `a372aa5d06a8c4f3bd6b2b69b87013d8bf43011d` (four commits).
- Actual timing HEADs: backend `a4f235b4effb35135e6815936dcc3e3fd043258c`; mobile `f57154ac5ffbe6791a38221c4fb273aa76b68f83`, both recorded clean and identical across segments.

Read the requested execution/parent-verification reports, public performance manifest, packaged changed-file inventories and handwritten QA configuration/coverage scripts and current claims. Used pure standard-library JSON/statistics/hash/ZIP reads to reconcile recorded evidence. Current final HEADs were independently confirmed. No tests, application imports, builds, DB/HTTP/device/network actions, helper agents or source/index/HEAD mutations were performed. Only this report was written. Raw logs/generated data were inspected through targeted records and manifests rather than every line/cell.

## Final measurements — valid completed two-segment evidence

Independently verified from the preserved raw files:

- Exactly **12 unique (size, run, endpoint) groups**, **30 samples each**, **360 measured requests** and **12 separate warmups**.
- Every median and nearest-rank p95 recomputes from its individual samples within 1e-6 ms; every maximum response-byte and SQL count equals its samples.
- Both raw files retain their actual helper kinds (`acceptance` and `diagnostic-preflight`) and the frozen clean source identity. The public manifest correctly explains the combination; it does not relabel the second invocation or fabricate one successful command.
- The 15 manifest-listed raw files match their SHA-256 in both backend originals and mobile mirrors. All 21 recorded analytics-module hashes match current disk.
- All three pre/post/between-segment source snapshots are identical. Independently checked current hashes for **272 backend and 680 mobile non-document files** against the frozen snapshot. Documentation changes after measurement do not masquerade as product changes.
- Raw compact profiles retain 100 points, 10,000/100,000 observations, two contexts and 100 metrics. Rules are `p1-08-v1`, captured revision 0. The frozen benchmark's per-response assertions check expected owner, ready/rule/revision agreement, 18-position rows, unique selected fact counts, reference bounds, 100 workouts and disjoint/exhaustive 50-workout comparison halves with exact half counts. I did not re-execute these requests or claim that every returned payload is archived; the successful measured loops plus inspected assertions provide that execution evidence.
- Exercise/comparison SELECT counts are **12/13 at both sizes**, within **12/20**, including application-user lookup. This series demonstrates no query-count growth with the tenfold set count for its one-exercise/100-workout fixture; it is not itself a many-exercise fan-out benchmark.

F53's interruption is properly accounted for. All six 10k groups completed before the untimed cleanup. The exact own cleanup cancellation, retained owner11/100 workouts/10,000 facts and closed/rolled-back connection are recorded. That process really exited **1**, and the raw exception is retained. Only the remaining 100k groups ran, once, with `--keep-fixtures`, exit **0**. No completed 10k measurements were repeated. Because the exception occurred after all 10k request samples and outside their timing, it does not invalidate those samples; it does invalidate any claim that the original combined command completed successfully. The current manifest makes that distinction correctly.

The 27 indexed source-review/evidence archive files match their manifest hashes, with no listed missing file. Historical full-format performance content matches the approved Git blob after ordinary CRLF normalization. A literal Git-blob versus Windows-working-file byte comparison initially differed solely by CRLF; the reviewer therefore verifies historical content preservation, not an independently observed pre-copy working-file byte identity. The new raw timing files/mirrors were checked byte-for-byte without normalization.

### Measured latency and disposition

| 100k endpoint | Run 1 p95 | Run 2 p95 | Bytes | SQL |
|---|---:|---:|---:|---:|
| Legacy | 7.176 s | 6.866 s | 2,509,413 | 107 |
| Exercise | 20.204 s | 19.157 s | 10,908,466 | 12 |
| Comparison | 21.664 s | 20.406 s | 10,920,704 | 13 |

Independently recomputed **seven of eight** analytics p95 ratios above 2× legacy. At 100k, exercise is 2.815/2.790× and comparison 3.019/2.972×. At 10k, exercise is 1.934/2.813× and comparison 2.122/3.119×. These are substantial waits, not a latency PASS hidden behind improved byte counts.

**Reviewer disposition:** accept the measurement, query-budget and completed diagnosis/removal evidence for source handoff. The binding plan makes >2× a trigger to inspect and remove identified degradation, not a requirement to keep optimizing until a numerical ratio passes. The previously identified repeated normalization/history loading/publication traversal/point serialization and duplicate client hydration were addressed in the already approved source. The final evidence retains all selected facts and shows constant query count and the expected compact byte reduction. This supplement establishes no new concrete avoidable source defect requiring another fix wave. The ratio alone is insufficient to invent one or impose an unapproved absolute SLA.

The residual roughly 18–22-second complete-history server processing and ~10.9 MB payload are nevertheless a **disclosed usability/operational risk**, not proof of optimality or end-user acceptability. In-process ASGI covers routing/DB/serialization/body production, not physical network, real concurrent users, native fetch latency, device memory or full-app responsiveness. Accepting the completed source/measurement obligations does not waive those unperformed acceptance dimensions. An unconditional “performance is good” or release-ready claim remains unsupported. No truncation, new pooled metric or speculative optimization is recommended by this evidence review.

## Aligned native evidence — scoped component rows now supported

The QA changes are configuration/evidence under `docs/validation/p1-08/native`, not a product dependency/native-source change. The authored Gradle hook propagates the pinned RN debug class definition while retaining release Java/Expo variants, requires the existing graph check before preBuild, and enables the configure-only forbidden-task guard only under its explicit proof property. The correction coverage script checks actual effective model/commands and exact include providers, rather than merely trusting requested flags. Earlier incomplete app coverage is preserved and superseded by the corrected registration-before-plugin proof.

Independently checked the final correction manifest: all **eight consumers / 292 commands** pass with no listed failures and release/nondebuggable effective models. Retained command hashes match expected hashes. The initial debug/production class-layout mismatch is concrete diagnostic evidence; successful aligned startup supports that mechanism without claiming instruction-level proof or absence of all native ABI defects.

Provenance is stronger than checkout identity alone:

- The first aligned stale JS bundle was detected before launch, preserved and not used as current-code acceptance.
- Final APK file actually hashes to **`4fc6463d0b0d7796c5f6f65b14d6807b48118a3669a8e9e1da7b8f3985376276`**, size **80,115,530 bytes**.
- Pure ZIP reads independently verify its embedded HBC and `libreactnative.so` hashes against the final proof: bundle `200612eefc5ad03a5a2fa7f90f8125b7f6345b983ca251415f08b0d4e0832efc`; provider `1c4d09576163a7f0b10f32ce1fae2e1c04701dc2e29786697465b0f2fd0c1b7a`.
- All **109** recorded imported product source hashes match current disk and their recorded `sourcesContent` hashes exactly. This does not imply every product module was exercised by the component fixture.
- Targeted runtime records show PID **3450 → 4287**, boot counter **1 → 2**, offline=true after relaunch, the same owner, **100 points / 100,000 observations**, invalidated=false and the same original timestamp `2026-10-16T10:54:36.396256Z`. This is actual separate-process native persistence evidence, not a module-reload/in-memory-adapter claim.

The parent independently viewed the named settled screenshots. This reviewer relies on that explicitly attributed visual inspection and inspected the supporting manifests/log markers; I did not independently re-view every image. Bounded standalone startup, examined full/short/unknown component states, front positive/zero/no-data map selection, the examined en/kg/light and ru/lbs/dark settings, 1.3-font/~320dp wrapping/scroll, offline Retry/reconnect and 100k compact cold-process persistence are supported **within the fixture scope**. Early loading/stale-state frames are not substituted for the later settled images.

The fixture's hardcoded small-mode period/source dates prevent a native period-filter correctness claim. Full authenticated app/history/source routes/block comparison, negative/back map, every settings combination, remaining manual/device cases, iOS, physical-device memory/performance and production release equivalence remain unrun. The hybrid RN/Hermes debug-provider artifact is not a supported production-release performance proxy. Historical pre-JS crash evidence remains historical; the current aligned observed startup supersedes the earlier blanket “startup failed” statement only for this exact QA artifact.

Cleanup records correctly distinguish the controlled `pg_ctl` exit0 and stopped verified PID9532/closed port55408 from the parent launcher's later exit1/output0. Private AVD and private PostgreSQL are recorded stopped, data retained, shared ADB untouched. No additional cleanup/service action was performed here.

## New evidence inconsistency only

**Minor E1 — narrow the deadline wording.** `D:/Eurith/worktrees/p1-08-mobile/docs/validation/p1-08/acceptance.md:124` says the final I7 deadline/caller abort extends through “body consumption/JSON parsing.” The approved implementation retains the timer/abort race through `response.text()`; JSON.parse occurs after that helper returns and cleans up, and synchronous parsing is not preempted by an abort timer. The actual source fix and focused tests established the body lifetime, not an interruptible parsing deadline.

Bounded correction: say “through response-body consumption; JSON parsing follows successful consumption,” retaining the existing native/runtime limitations. This is a documentation overstatement, not a reopened I7 product defect or a reason to rerun tests/measurements. No other new evidence inconsistency was established in this supplement.

## Declined to judge

No assertion of exhaustive log/cell inspection, universal malformed historical JSON safety, full native/authenticated/manual/iOS acceptance, physical network/concurrency/device-memory performance, production-release equivalence, deployment/backup suitability or every untouched code path. The surviving gaps are explicit, and unchanged qualified full-suite failures/overlapping focused counts are not relabeled all green.

## Final evidence verdict

**Bounded evidence accepted, with one Minor documentation wording correction (E1).** Required repeated sample collection and frozen-source reconciliation are complete; the >2× diagnosis requirement has an explicit evidence-backed disposition above, without inventing an SLA or claiming favorable end-user latency.

**Source approval remains unchanged:** B `5f4aa578eedbe3abe49a50c5a72acedea97d8542` / M `72e1d76614ba3e4e17d8757c7276cb52a43be9c4`; later HEADs are the inspected QA/document/evidence commits. The 13 original findings and N1/N2 remain closed. No new Critical or Important source issue is established.

**Task21/release readiness remains OPEN.** The scoped native component successes close only their listed fixture rows. Mandatory full-app/authenticated/navigation/comparison and remaining supported native/manual/iOS/release-equivalence gaps remain; measured complete-history latency and unmeasured end-user performance must remain visible in delivery status. This supplement does not approve merge, deployment, production actions or a blanket acceptance PASS.

## E1 closure and final documentation disposition

Same-seat bounded closure check of backend `c4dbc1698d6685a9b04cc9bd26a97c6ec6717b4c..46d7b3362ba9b133a9e6aa94f3540a9d9d429d02` and mobile `a372aa5d06a8c4f3bd6b2b69b87013d8bf43011d..0976c36d36c2ff9867bc710f21ea9f35a022abe3`, using the supplied E1 closure packages only.

**E1 closed.** The acceptance text now explicitly limits the timer/caller abort to response-body consumption and states that subsequent synchronous JSON parsing is not preempted. The rollout and handoff use the same qualified meaning.

**F54 and handoff accepted as consistent with this review.** They adopt completed measurement/SQL/fact/source and diagnosis/removal obligations for source handoff while retaining the substantial latency risk, default-false rollout, isolated branches and all unperformed full-app/native/manual/iOS/release-equivalence limits. They do not claim a favorable SLA, blanket native PASS or Task21 completion.

The three combined-manifest diffs change only one limitations string and add `review_disposition`; no raw results, sample statistics, segments, exits or hash maps are changed. Other additions archive the already reviewed reports. No new product fix, test/build/benchmark/DB/device/service action or Git mutation was performed or authorized by this closure check. Only this append was written; the preceding report and its historical E1 observation remain intact.

**Final documentation verdict: approved; no remaining evidence wording finding.** Source approval remains B `5f4aa578eedbe3abe49a50c5a72acedea97d8542` / M `72e1d76614ba3e4e17d8757c7276cb52a43be9c4`. **Task21/P1-08 release readiness remains OPEN** for the explicitly retained acceptance gaps. Recorded owned services remain stopped; no fresh service-state verification is implied. Copying this append to the public review archive requires no further review cycle.
