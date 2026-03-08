# VoiceKey Requirement Traceability Matrix

This matrix maps requirements to backlog and verification status, including pending coverage where implementation is not complete.

---

## A. Canonical FR Coverage (FR-*)

| Requirement | Backlog Story Coverage | Verification Method |
|-------------|------------------------|---------------------|
| FR-A01 | E01-S01 | audio capture integration tests + dropped-frame notification coverage (`tests/unit/test_capture.py`) |
| FR-A02 | E01-S02 | VAD unit + integration tests + Silero chunking/no-wrap regressions (`tests/unit/test_vad.py`) |
| FR-A03 | E01-S03 | ASR adapter tests + single-resample regression coverage (`tests/unit/test_asr.py`) |
| FR-A04 | E01-S03 | transcript event contract tests |
| FR-A05 | E01-S04 | threshold behavior tests |
| FR-A06 | E01-S03 | model profile switch tests |
| FR-A07 | E01-S05 | hybrid routing tests (`tests/unit/test_asr_router.py`, `tests/unit/test_cli.py`, `tests/unit/test_config_manager.py`, `tests/integration/test_hybrid_asr.py`) + runtime integration regression suite |
| FR-W01 | E02-S01 | wake phrase config tests |
| FR-W02 | E02-S01 | wake-no-type safety tests |
| FR-W03 | E02-S01, E03-S02 | wake timeout timer tests |
| FR-W04 | E01-S02, E02-S01 | false-trigger mitigation tests |
| FR-C01 | E02-S02 | parser suffix detection tests |
| FR-C02 | E02-S02 | unknown-command literal fallback tests |
| FR-C03 | E02-S02 | alias/case-insensitive matching tests |
| FR-C04 | E02-S03 | fuzzy on/off guardrail tests |
| FR-C05 | E02-S04, E03-S03 | special phrase control-plane tests |
| FR-M01 | E03-S01 | mode default transition tests |
| FR-M02 | E03-S01, E04-S02 | toggle hotkey backend + runtime coordinator hotkey transition tests + Linux hotkey listener restart-cycle tests (`tests/unit/test_hotkey_linux_backend.py`) |
| FR-M03 | E03-S01, E03-S02 | continuous-mode warning/behavior tests |
| FR-M04 | E03-S02 | inactivity auto-pause tests + toggle-mode timeout transition coverage (`tests/unit/test_runtime_coordinator.py`) |
| FR-M05 | E03-S02 | timer default/config tests + coordinator timer wiring from config (`tests/unit/test_cli.py`) |
| FR-M06 | E03-S03 | paused output suppression tests |
| FR-M07 | E03-S03 | paused resume phrase + hotkey resume channel tests |
| FR-S01 | E05-S03, E10-S02 | tray-daemon startup tests + integration harness (`tests/integration/test_tray_integration.py`) |
| FR-S02 | E05-S02, E05-S03, E10-S02 | indicator state mapping tests + tray state synchronization tests (`tests/integration/test_tray_integration.py`) |
| FR-S03 | E05-S03, E10-S02 | tray action integration tests + full lifecycle tray state transitions (`tests/integration/test_tray_integration.py`) |
| FR-S04 | E04-S03, E10-S02 | autostart platform tests + integration harness (`tests/integration/test_autostart_integration.py`) |
| FR-S05 | E05-S03, E10-S02 | start-minimized tests + daemon session behavior integration tests |
| FR-O01 | E06-S03 | onboarding e2e step validation |
| FR-O02 | E06-S03 | wake test step checks |
| FR-O03 | E06-S03 | hotkey step checks |
| FR-O04 | E06-S03 | autostart preference persistence checks |
| FR-O05 | E06-S03 | tutorial completion checks |
| FR-G01 | E06-S01, E06-S02 | schema + migration tests |
| FR-G02 | E05-S01 | functional CLI config get/set/reset/edit persistence tests (`test_cli.py`) |
| FR-G03 | E06-S04 | custom command loader tests |
| FR-G04 | E06-S05 | snippet expansion tests |
| FR-G05 | E06-S06 | per-app profile resolution tests |
| FR-G06 | E06-S07 | portable-mode smoke tests |
| FR-D01 | E07-S01 | PyPI install smoke |
| FR-D02 | E07-S02, E08-S03, E10-S04 | complete - release workflow builds/publishes canonical Windows artifacts with post-publish smoke hooks; signing scripts use HTTPS timestamps; distribution verification tests validate installer/portable naming and checksums; production code-signing certificate path is environment-configurable |
| FR-D03 | E07-S03, E08-S03, E10-S04 | complete - release workflow builds/publishes AppImage with post-publish smoke; distribution verification tests validate naming, checksums, x86_64 targeting (`test_distribution_verification.py`) |
| FR-D04 | E07-S04, E08-S03 | release workflow generates SHA256SUMS and detached signature bundle with integrity artifact attachment |
| FR-D05 | E07-S05 | model catalog/checksum/downloader tests (`test_model_catalog.py`, `test_model_checksum.py`, `test_model_downloader.py`) |
| FR-D06 | E06-S07 | portable artifact validation |
| FR-D07 | E07-S04, E08-S03 | complete - signed tags and checksum-bundle signing enforced in release workflow; GPG key configuration via repository secrets (`VOICEKEY_GPG_PRIVATE_KEY`, `VOICEKEY_GPG_KEY_ID`); platform code-signing certificate is environment-configurable |
| FR-D08 | E07-S04, E08-S03 | release workflow generates and publishes CycloneDX SBOM in integrity bundle |
| FR-D09 | E07-S04, E08-S03 | release workflow generates and publishes provenance metadata in integrity bundle |
| FR-CI01 | E08-S01 | CI workflow required checks (`.github/workflows/ci.yml`) + integration guardrail script coverage (`test_check_perf_guardrails_script.py`) |
| FR-CI02 | E08-S01 | full Linux/Windows Python matrix execution in unit/integration jobs (`.github/workflows/ci.yml`) |
| FR-CI03 | E08-S01 | strict dependency vulnerability scan gate (`pip-audit -r requirements-dev.txt` in `.github/workflows/ci.yml`) |
| FR-CI04 | E08-S01 | performance guardrail job + enforcement toggle (`scripts/ci/check_perf_guardrails.py`, `test_check_perf_guardrails_script.py`) |
| FR-CI05 | E08-S02 | semantic tag trigger + signed-tag verification in release workflow (`.github/workflows/release.yml`) |
| FR-CI06 | E08-S02, E08-S03 | complete - isolated tagged Python build with OIDC PyPI publish; release workflow builds all channels (wheel, sdist, AppImage, Windows installer/portable) in isolated job with signed tag verification |
| FR-CI07 | E08-S02 | changelog + commit-metadata release notes generation (`generate_release_notes.py`, `test_generate_release_notes_script.py`) |
| FR-CI08 | E08-S03, E10-S04 | complete - release workflow Linux/Windows post-publish smoke jobs; distribution verification tests validate PyPI/Windows/Linux artifacts (`test_distribution_verification.py`, 25 tests) |
| FR-CI09 | E08-S03 | rollback/yank guidance automation hook and test coverage (`generate_rollback_guidance.py`, `test_generate_rollback_guidance_script.py`) |
| FR-CI10 | E08-S02 | PyPI trusted publishing via OIDC (`pypa/gh-action-pypi-publish` in `.github/workflows/release.yml`) |
| FR-OSS01 | E00-S01 | repository policy audit |
| FR-OSS02 | E00-S01 | governance file audit |
| FR-OSS03 | E00-S01 | template presence check |
| FR-OSS04 | E00-S02 | semver/changelog policy check |
| FR-OSS05 | E11-S02 | complete - compatibility matrix (`docs/compatibility-matrix.md`) with OS/Python versions, platform notes; developer docs validation script (`scripts/docs/validate_developer_docs.py`) with 23 checks; integration tests (`tests/integration/test_validate_developer_docs_script.py`) with 12 passing tests |
| FR-OSS06 | E00-S03 | security policy SLA check |
| FR-OSS07 | E00-S01 | DCO workflow check |

---

## B. Non-ID Requirement Coverage

| Source Requirement (No explicit FR ID) | Backlog Coverage | Verification |
|----------------------------------------|------------------|--------------|
| Built-in command sets in section 4.4 | E02-S05, E04-S04 | command registry and parser tests |
| Productivity commands feature-gated until P1 | E02-S05, E04-S04, E06-S05 | default-config + feature-flag tests |
| Config path defaults and override channels (`--config`, `VOICEKEY_CONFIG`) | E06-S08 | config resolution precedence tests |
| Environment variable runtime controls (`VOICEKEY_MODEL_DIR`, `VOICEKEY_LOG_LEVEL`, `VOICEKEY_DISABLE_TRAY`) | E06-S08 | startup env parsing tests |
| Hot reload semantics (safe-to-reload vs restart-required keys) | E06-S08 | reload contract tests |
| Onboarding accessibility and keyboard-only operation | E06-S09 | onboarding accessibility e2e tests |
| Onboarding skip flow writes safe defaults | E06-S09 | skip-path config safety tests |
| Performance targets (wake, ASR, parse, p50/p95) | E10-S03 | complete - `tests/perf/benchmark_runner.py` measures latency for wake detection (p50<1ms), command parsing (p50<1ms), ASR simulated (p50<1ms), state machine (p50<0.1ms); all pass p50<=200ms, p95<=350ms thresholds; 29 perf tests + 11 integration tests passing |
| Resource budgets (CPU/memory/disk) | E10-S03 | complete - `tests/perf/resource_monitor.py` measures CPU/memory during idle/active states; validates idle CPU<=5%, active CPU<=35%, memory<=300MB budgets; resource budget tests passing |
| Reliability bullets (single-instance, reconnect, crash-safe shutdown, bounded retries) | E03-S04, E03-S05, E10-S05 | complete - E03-S04 resilience tests (`tests/unit/test_runtime_resilience.py`); E03-S05 single-instance/shutdown tests (`tests/unit/test_single_instance.py`, `tests/unit/test_shutdown.py`) plus status lock-probing regressions (`tests/unit/test_cli.py`); E10-S05 reliability/soak tests (`tests/integration/test_reliability.py`, `tests/perf/test_soak.py`, 44 tests covering reconnect, rapid toggles, paused-resume race conditions, state machine stress, memory leak detection, long-duration operation simulation) |
| Privacy bullets (local-default runtime, opt-in hybrid cloud fallback, no telemetry, no raw audio persistence, no transcript logs by default) | E09-S01, E09-S02, E09-S03, E01-S05 | complete for privacy defaults and hybrid controls: cloud mode remains explicit opt-in, cloud-primary fails closed without credentials, and hybrid mode downgrades safely to local-only with warning |
| Incident response flow (redacted diagnostics, pause/disable autostart) | E09-S02 | `docs/incident-response.md` + diagnostics security tests |
| Usability targets (first setup <=5 min, first sentence <=2 min) | E06-S03, E10-S02 | complete - onboarding timing evidence exists; integration harness tests validate end-to-end pipeline flow (`tests/integration/test_pipeline_integration.py`) |
| Linux support target (Ubuntu 22.04/24.04 x64, X11 full, Wayland best-effort) | E04-S03, E10-S04 | complete - adapter diagnostics + distribution verification tests (`tests/integration/test_distribution_verification.py`, 25 tests for AppImage naming, checksums, x86_64 targeting) |
| Windows support target (10/11 x64, standard/admin behavior) | E04-S03, E10-S04 | complete - adapter diagnostics + distribution verification tests (`tests/integration/test_distribution_verification.py`, 25 tests for installer/portable naming, checksums, x64 targeting) |
| Distribution policy (x64 public scope, artifact naming convention, one-major migration path) | E07-S06 | release policy validator unit/integration checks (`test_release_policy.py`, `test_validate_distribution_policy_script.py`) |
| CI hardening controls (secret scan, license scan, branch protection, CODEOWNERS, pinned actions, least-privilege permissions, CI observability) | E08-S04 | complete - CODEOWNERS file, CI metrics export (`export_ci_metrics.py`), branch protection validation (`check_branch_protection.py`), pinned actions by SHA, least-privilege workflow permissions (`test_export_ci_metrics_script.py`, `test_check_branch_protection_script.py`) |
| Error and edge scenarios table (no mic, disconnect, unknown command, hotkey conflict, checksum fail, keyboard block) | E03-S04, E04-S02, E07-S05 | integration/error-path tests |
| Unit test baseline (parser, FSM, config migration, backend capability checks) | E10-S01 | complete - 516 unit tests passing with 87% coverage; parser 99%, FSM 100%, config migration 99%, backend bases 97-100% (`tests/unit/test_parser.py`, `tests/unit/test_state_machine.py`, `tests/unit/test_config_migration.py`, `tests/unit/test_keyboard_backends.py`, `tests/unit/test_window_backends.py`, `tests/unit/test_hotkey_backends.py`, `tests/unit/test_autostart_adapters.py`) |
| Test matrix governance (Ubuntu/Windows + Python version matrix coverage) | E10-S06 | complete - matrix coverage assertion script (`scripts/ci/check_matrix_coverage.py`), report generator (`scripts/ci/generate_matrix_report.py`), matrix-coverage CI job with artifact upload, 20 integration tests passing (`test_check_matrix_coverage_script.py`); validates ubuntu-22.04/24.04 + windows-2022 with Python 3.11/3.12 |
| P2 ecosystem roadmap (plugin SDK, language packs, advanced automation plugins) | E12-S01..E12-S03 | roadmap feature test suites |
| Acceptance criteria section 9 | E10-S01..E10-S06 | complete - E10-S01 (unit baseline: parser 99%, FSM 100%, config migration 99%, backend bases 97-100%); E10-S02 (integration harness: 55 tests for pipeline, tray, autostart); E10-S03 (performance harness: 29 perf tests + 11 integration tests, benchmarks + resource monitor); E10-S04 (distribution verification: 25 tests for PyPI/Windows/Linux naming, checksums, policy); E10-S05 (reliability/soak: 44 tests for reconnect, toggles, race conditions, stress, memory leaks); E10-S06 (matrix coverage: 20 integration tests, assertion + report scripts, CI job) |
| Required implementation artifacts in sections 11 and 15 | E11-S01..E11-S03 | complete - E11-S01 complete (user docs validation: `scripts/docs/validate_user_docs.py`, 11 integration tests `test_validate_user_docs_script.py`, all required sections validated - installation, onboarding, commands, troubleshooting); E11-S02 complete (developer docs validation: `scripts/docs/validate_developer_docs.py`, `docs/compatibility-matrix.md`, 23 validation checks, 12 integration tests passing); E11-S03 complete (traceability validation: `scripts/release/validate_traceability.py`, `scripts/release/check_release_gate.py`, `tests/integration/test_traceability_scripts.py`, 22 integration tests passing) |
| Backlog execution and traceability maintenance rules | E11-S03 | complete - traceability validator script (`scripts/release/validate_traceability.py`), release gate check script (`scripts/release/check_release_gate.py`), 22 integration tests (`tests/integration/test_traceability_scripts.py`); validates FR requirements coverage, backlog story mapping, P0 story completion, and release readiness |

---

## C. Coverage Gate Rule

Release candidate is blocked if any row in section A or B lacks:

1. backlog story mapping,
2. passing verification evidence,
3. updated documentation pointers.
