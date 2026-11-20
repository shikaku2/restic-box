# Restic Box 0.6.0: Remaining Fixes and Verification

**Status: all 9 items implemented and verified in 0.7.0** — see
`0.7.0fixes.md`. Verification was headless `unittest` (no network, disposable
temp restic repos only) plus real restic 0.19.1 integration runs.

This checklist captures the outstanding items from the review of the AI-generated fixes bundle (`restic-box-fixes.zip`). It does not repeat the issues that the previous review judged fixed.

## Priority 1: Fix before relying on unattended backups

### 1. Re-check cancellation before launching repository statistics

**Location:** `restic_indicator.py`, `_run_backup()`

**Problem:** Cancellation is checked around backup and retention, but after retention is stopped the worker can still proceed to launch `run_raw_data_size()`. This is read-only, so it is not equivalent in risk to an unwanted backup or prune, but it violates the expected Stop All behavior and can keep work running after cancellation.

**Change:** Check the shared stop/cancellation token immediately after retention and immediately before launching repository statistics. Audit the remaining post-cancellation path for any other subprocess launches.

**Acceptance checks:**
- Start a backup that reaches retention.
- Trigger Stop All while retention is running.
- Verify no repository-statistics command or other follow-on subprocess starts.
- Verify the operation ends in the cancelled/stopped state rather than success.

### 2. Keep repository initialization cleanup reliable when setup/logging fails

**Location:** `restic_settings.py`, `_do_init_repo()`

**Problem:** Diagnostic calls such as `runner.repo_url(cfg)` execute before the worker's `try` block. If one raises, the worker can exit without calling `_init_repo_done()`, potentially leaving the Initialize button disabled and the UI in a stale state.

**Change:** Move all setup and diagnostic operations that can raise inside the `try` block. Ensure the completion callback runs on both success and failure, preferably through a `finally`-style completion path or a single result handoff to the GTK main thread.

**Acceptance checks:**
- Supply invalid repository settings that cause URL construction to fail.
- Verify the error is surfaced to the UI.
- Verify the Initialize button is re-enabled and the dialog remains usable.

### 3. Make the retention ownership policy explicit and correct

**Location:** Retention implementation and its documentation/configuration

**Problem:** The review found documentation claiming that Restic's default grouping includes `(host, tag, paths)`. The default grouping described by the review is host and paths; tags are only part of grouping when explicitly requested. Per-run UUID tags therefore do not, by themselves, create separate retention groups. In addition, the current retention operation is repository-wide unless filters are supplied, so it can affect snapshots created by other applications or machines sharing the repository.

**Change:**
- Correct the grouping documentation and comments.
- Decide and document whether Restic Box owns the entire repository or only a defined subset of snapshots.
- If shared repositories are supported, implement an explicit, tested filter/ownership strategy for retention rather than assuming per-run tags isolate snapshots.
- Do not add `--group-by tag` or other grouping changes without verifying the resulting retention semantics and compatibility with existing snapshots.

**Acceptance checks:**
- Create snapshots representing Restic Box and an unrelated backup in the same repository.
- Run the configured retention operation.
- Verify the unrelated snapshots are retained unless the documented policy explicitly says the application owns the whole repository.
- Verify per-run grouping behaves as documented across multiple split units and multiple backup runs.

## Priority 2: Correctness and compatibility improvements

### 4. Do not make a raw TCP probe a mandatory SSH connectivity gate

**Location:** SSH reachability check / connection-test path

**Problem:** `is_host_reachable()` uses a direct TCP socket probe. That can reject valid SSH configurations using `ProxyCommand`, `ProxyJump`, or other OpenSSH transport configuration even when the actual SSH command would work.

**Change:** Either remove the TCP probe as a mandatory precondition or replace it with a probe that uses the same SSH configuration and transport as the real Restic operation. Keep connection testing and backups consistent.

**Acceptance checks:**
- Test a normal direct SSH connection.
- Test a working `ProxyJump` or `ProxyCommand` configuration.
- Verify the connection test and actual backup use equivalent SSH options and both succeed.
- Verify a genuinely unreachable host still produces a useful error.

### 5. Take configuration snapshots under the configuration lock

**Location:** `restic_indicator.py`, operation configuration snapshot creation

**Problem:** The worker deep-copies configuration before an operation, but the copy is made without acquiring `_cfg_lock`, while settings saves and other updates use that lock. A concurrent update could therefore produce an inconsistent snapshot.

**Change:** Acquire `_cfg_lock` around each copy of the live configuration, including any direct-call fallback path that snapshots `self._cfg`.

**Acceptance checks:**
- Exercise a settings save concurrently with starting an operation.
- Verify the operation receives one internally consistent configuration snapshot.
- Verify subsequent settings changes affect only later operations.

### 6. Bound unusually large individual output records

**Location:** `restic_runner.py` output capture and `restic_status.py` log window

**Problem:** The updated code bounds retained output and the visible log window, but the review noted that a single exceptionally large output line may still exceed the intended memory bound. Rotating diagnostic log files were also not implemented.

**Change:** Enforce a maximum record/line size before retaining output. Consider writing detailed diagnostics to a rotating file while keeping only bounded recent output in memory. Routine progress JSON should be parsed for progress and then discarded.

**Acceptance checks:**
- Feed a very large single output line and a long stream of progress records to the output handler.
- Verify memory-retained output remains bounded.
- Verify useful diagnostic context remains available and log rotation enforces a disk-space limit if implemented.

## Test-suite and integration verification

These are verification blockers or test-harness fixes, not necessarily product-code defects.

### 7. Fix the pytest helper accidentally collected as a test

**Location:** `tests/test_suite_hygiene.py`, `test_sources()`

**Problem:** Pytest discovers the generator helper because its name starts with `test_`, then rejects its `yield` during test collection.

**Change:** Rename the helper to a non-test name such as `_test_sources()` and update its callers. Keep actual test functions named with the normal pytest convention.

**Acceptance checks:**
- Run pytest collection for the full suite.
- Verify the helper is not collected as a test and collection completes successfully.

### 8. Run the GTK-dependent test suite in an environment with PyGObject

**Problem:** The previous review environment lacked the `gi` module, preventing GTK-dependent tests from importing. This is an environment limitation, not evidence that those tests pass or fail.

**Change:** Run the relevant tests on a Linux environment with the project's required GTK/PyGObject dependencies installed. Fix any genuine failures found there.

**Acceptance checks:**
- Record the Python, GTK, and PyGObject versions used.
- Run the complete test suite, not only the headless subset.
- Confirm there are no collection errors and report all skips with their reasons.

### 9. Run Restic-dependent integration tests with the target executable

**Problem:** The previous review could not exercise the Restic-dependent tests because the `restic` executable was unavailable. A filesystem-permission test may also need an environment that enforces the expected permissions.

**Change:** Run the integration tests using the same Restic version intended for deployment. Use a disposable test repository; do not point destructive retention tests at a production repository.

**Acceptance checks:**
- Record `restic version` and the test environment.
- Run all Restic-dependent integration tests against a disposable local repository.
- Exercise complete backup, partial backup, cancellation, retention, repository checks, and snapshot-view behavior where covered by the suite.
- Verify partial or failed backups do not trigger automatic retention.
- Verify the tests do not modify any real user repository or credentials.

## Suggested execution order

1. Fix cancellation before follow-on commands.
2. Fix initialization exception handling.
3. Decide and test the repository-retention ownership policy.
4. Fix the SSH connectivity gate.
5. Lock configuration snapshot creation.
6. Fix pytest helper discovery.
7. Run the full GTK suite and Restic integration suite.
8. Address the output-record memory bound.

## Previous review limitations

The previous review reported 120 passing tests and 5 skipped tests in the runnable subset, but full-suite verification was blocked by missing PyGObject and the pytest generator-helper collection problem. Restic-dependent integration behavior was not verified against a real Restic executable. Treat those results as partial evidence, not as a release sign-off.
