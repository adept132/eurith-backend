# Final frozen-source measurement execution report

Evidence/orchestration only; no product modules, fixtures/indexes/environment behavior, native source/build/device, tests/profiles, server, migrations or production actions changed. No helpers/subagents, push/merge/deploy. Parent owns PG/shared ADB; both left running. W intact.

## Exact identities and commits

Backend task BASE 5f4aa578eedbe3abe49a50c5a72acedea97d8542; preparation evidence commits35724ec and a4f235b4effb35135e6815936dcc3e3fd043258c. Both actual timing segments captured clean HEAD a4f235b4effb35135e6815936dcc3e3fd043258c. Final backend HEAD c4dbc1698d6685a9b04cc9bd26a97c6ec6717b4c.
Mobile task BASE54741f2482ddfaef6ef66aa1a8e408155949bf9d (QA-only since approved product72e1d76614ba3e4e17d8757c7276cb52a43be9c4); prep evidence commit/timing HEADf57154ac5ffbe6791a38221c4fb273aa76b68f83. Final mobile HEAD 8047058bb890c9a5d6995a794d26d63e9598435c. Both final worktrees clean. Full pre/post hash snapshots and each helper analytics-module hash map are preserved. Product hashes matched throughout, including before the F53 continuation and after final commits. Backend delta from approved product is docs-only; mobile task delta is docs-only.

## Actual commands, process outcomes and F53 exception

Python D:/Eurith/backend/.venv/Scripts/python.exe, cwd B, login=false. Private ignored benchmark URL file was parsed/validated BEFORE application/Firebase imports: localhost port55408 exact eurith_p108_test_benchmark, no query/fragment or fallback. Credentials never printed/copied. P108_TEST_DATABASE_URL_FILE points only to existing private runtime file.

1. `python -u scripts/benchmark_analytics.py --compact --sizes 10000 100000 --workouts 100 --runs 2 --requests 30 --output <ignored-W>/final-compact-performance.json`. Launcher25300/actual worker10832, tool session80652. Six10k groups/180 measured requests plus six separate warmups completed. Launch omitted the existing --keep-fixtures flag. Untimed synthetic cleanup ran over320s. One parent-authorized bounded pg_stat_activity check found active unblocked DELETE; F53 then authorized cancel only that exact own cleanup. Verified backendPID23040 exact DELETE text, OS connection ports54015/54008 owned by worker10832 and synthetic owner11 marker/count. pg_cancel_backend returned true; no termination/other transaction altered. Actual exit1 and QueryCanceledError retained. Post-close independent count verifies owner11 retains100workouts/10000facts; connection closed/rollback completed. No measurement invalidated or repeated.
2. F53 continuation ONLY: `python -u scripts/benchmark_analytics.py --compact --sizes 100000 --workouts 100 --runs 2 --requests 30 --keep-fixtures --output <ignored-W>/final-compact-100k-performance.json`. Launcher16592/actual worker4716, session90955; clean HEADs/product hashes rechecked before start. Six100k groups/180 measured requests plus six warmups completed; actual exit0, owner12 retained. Same guarded runtime/history policy/sequential settings, no10k repeat.

Raw helper kind labels remain `acceptance` and `diagnostic-preflight` (the latter because sizes contains only100000); no raw JSON relabeling or fake single-process exit0. Public manifest explicitly combines two actual segments under F53, actual exits1/0, all12 groups and every raw file SHA256.

## Public evidence and verification

Backend docs/validation/p1-08/artifacts/task21/final-compact-* contains raw JSON/stdout/exits/process identities, source snapshots, bounded cleanup observations/cancel/rollback counts, manifest and verification. Mobile has exact mirror under artifacts/task21/final-compact plus current docs/validation/p1-08/performance.json. Original full-format performance.json preserved byte-for-byte as performance-historical-full.json BEFORE replacement. All historical raw logs/test counts retained.

Archive artifacts/task21/final-source-review preserves whole-branch/fix/scoped/residual reports, actual focused RED/GREEN/covering/refinement/tsc/i18n logs, native ABI/aligned execution and BOTH F50/F51 alignment proof/correction reports. archive-index.json lists SHA256. No requested report/log missing. Public execution-decisions retains F50–52 and exact parent F53. Acceptance/rollout current assertions coherently identify approved source, actual scoped aligned-native images/MMKV, final body-lifetime deadline fix, historical expanded timings and current segmented compact evidence. They do not claim current blanket native/full-app/release approval.

Validation actual outcome PASS:12 unique(size,run,case) groups ×30=360 samples;12 separate warmups; every median and nearest-rank p95 recomputed exactly, response byte/sql maxima match samples. Exercise12/comparison13 SELECTs including auth constant at10k/100k satisfy12/20 budgets. Per-response frozen helper assertions validate owner/ready/rules/revision agreement, all18-column rows, registry bounds,100workouts, exact unique selected observation counts10000/100000 and disjoint/exhaustive50workout comparison halves/exact half counts. Final source revisions in raw group metadata are actual captured values; rules p1-08-v1. Default expanded compatibility uses existing tests, not this compact series.

Raw hashes matched both preserved copies; private credential material checked absent from new archives without printing values. Authored acceptance/rollout/performance/execution-decisions staged whitespace check exit0; raw logs intentionally retained verbatim. git commit exited0 for final evidence commits, both status clean. No extra test suite/profiling run.

| Sets | Run | Endpoint | Median ms | p95 ms | SQL | Bytes |
|---|---|---|---|---|---|---|
| 10000 | 1 | legacy | 732.355000 | 1111.802400 | 107 | 259313 |
| 10000 | 1 | exercise | 1770.386300 | 2150.062700 | 12 | 1195945 |
| 10000 | 1 | comparison | 1970.202500 | 2359.352600 | 13 | 1208082 |
| 10000 | 2 | legacy | 705.711700 | 810.284900 | 107 | 259313 |
| 10000 | 2 | exercise | 1788.656550 | 2279.640400 | 12 | 1195945 |
| 10000 | 2 | comparison | 1977.415800 | 2527.609400 | 13 | 1208082 |
| 100000 | 1 | legacy | 5906.321200 | 7175.965500 | 107 | 2509413 |
| 100000 | 1 | exercise | 18060.864400 | 20203.573300 | 12 | 10908466 |
| 100000 | 1 | comparison | 20020.030850 | 21664.277600 | 13 | 10920704 |
| 100000 | 2 | legacy | 6217.104750 | 6866.056800 | 107 | 2509413 |
| 100000 | 2 | exercise | 17748.681250 | 19157.225900 | 12 | 10908466 |
| 100000 | 2 | comparison | 18135.531000 | 20405.503700 | 13 | 10920704 |

## Residual disposition and acceptance gaps

100k exercise p95 ratios2.815/2.790; comparison3.019/2.972 to same-run legacy.10k exercise1.934/2.813; comparison2.122/3.119. Seven of eight analytics p95 comparisons exceed2x diagnosis trigger. Identified repeated normalization/history/serialization/hydration causes were addressed in frozen product source; complete100k-fact computation/serialization still costs roughly18–22s and10.9MB. This is a documented residual for parent/same final reviewer adjudication, not a unilateral waiver or invented absolute SLA. ASGI includes routing/serialization/DB/app-user lookup but excludes physical network/device/native fetch/memory. Final helper retains complete selected facts, no truncation.

Native scoped startup/parent-examined panels/settings/maps and actual100k compact MMKV cold-process/offline persistence stand only within F50–52 fixture scope. Full authenticated application/history/routes/block comparison, other manual/device/iOS/physical memory/performance/production-release-equivalence rows remain UNRUN. Historical pre-JS hybrid crash remains historical, not current supported-release defect. Baseline failures and overlapping source-test counts remain qualified. Task21/full readiness NOT declared complete. Same final-review seat evidence supplement remains parent's next action.
