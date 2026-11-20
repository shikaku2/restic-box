# Restic Box: Additional Fixes and Follow-up Work

This checklist captures the additional findings from the second source
review. It is separate from the earlier `restic-box-remaining-fixes.md`
checklist.

## Priority 1: High

### \[x\] 1. Prevent backup results from being recorded against a different configuration

**Area:** `restic_indicator.py` --- `_run_backup()`, `_run_check()`,
`_merge_cfg()`

An operation uses a configuration snapshot, but its completion metadata
can be merged into the currently selected configuration after the user
changes repositories or source directories.

Example: a backup starts against repository A; the user changes settings
to repository B; the backup finishes against A; its success timestamp is
then applied to B. The scheduler may consequently believe B was backed
up when it was not.

**Required changes** - Associate operation results with the exact
repository and source-configuration identity used by that operation. -
Only update bookkeeping for the matching configuration identity. -
Preferably persist backup/check history per repository and source
configuration. - Apply the same rule to integrity-check results.

**Acceptance tests** - Change repository A to B during a backup; verify
B receives no success timestamp. - Change source directories during a
backup; verify the new source configuration is not marked backed up. -
Change repositories during a check; verify the result is recorded only
against the repository actually checked.

## Priority 2: Significant reliability issues

### \[x\] 2. Exclude visible ZFS snapshot directories from actual backups

**Area:** `restic_split.py` scanning; `restic_runner.py` backup command
construction

The split scanner skips `.zfs`, but the actual Restic command may not.
On systems where ZFS snapshot directories are visible, Restic may
traverse `.zfs/snapshot` and back up historical filesystem contents
unintentionally.

**Required changes** - Ensure `.zfs` is excluded from actual Restic
backup traversal, not merely ignored by the planner. - Handle dataset
roots and nested paths correctly. - Keep planner behavior and actual
Restic exclusions consistent.

**Acceptance tests** - Create a test source containing a visible
`.zfs/snapshot` directory. - Verify the planner does not treat its
contents as ordinary source data. - Verify the generated Restic command
excludes it. - If practical, verify with a real Restic integration test
on ZFS.

This is conditional on ZFS configuration, but the consequences can
include substantial duplicate data and repository growth.

### \[x\] 3. Make cancellation and scheduling consistent across all operation paths

**Area:** `restic_indicator.py` --- `_op_worker()`, `_run_check()`,
`_run_full_check()`, `_backup_due()`, `_schedule_backup()`

Cancellation and scheduling need to cover the entire operation
lifecycle, including connection preflight and integrity checks. Failed
or cancelled scheduled operations must not immediately retry on every
scheduler tick.

**Required changes** - Use a shared cancellation token throughout
planning, preflight, execution, retention, and follow-up work. -
Register every subprocess centrally so Stop All can terminate the
process that is actually running. - Check cancellation immediately
before every subprocess launch. - Track attempt timestamps separately
from successful completion timestamps. - Add retry backoff for failed
preflight and cancelled operations. - Ensure cancellation cannot
accidentally launch a later statistics, check, backup, or retention
command.

**Acceptance tests** - Stop during SSH preflight; verify no backup/check
command launches afterward. - Stop during a check; verify the process
terminates and no subsequent operation starts. - Cancel a scheduled
backup; verify it does not immediately requeue. - Fail SSH preflight;
verify retry backoff is applied. - Stop during retention; verify no
follow-up statistics command launches.

### \[x\] 4. Serialize snapshot-browser destructive operations with other repository operations

**Area:** `restic_indicator.py` --- `SnapshotBrowser` Forget/Prune
operations and operation coordination

Snapshot-browser Forget and Prune operations must not run concurrently
with one another or with operations requiring exclusive repository
access. A single `_current_proc` reference is insufficient if multiple
worker threads can launch processes.

**Required changes** - Route Forget and Prune through the shared
operation coordinator, or implement an equivalent central
exclusive-operation lock and subprocess registry. - Reserve the
operation slot atomically before starting a worker. - Ensure Stop All
can terminate every active operation process, not merely the most
recently registered one. - Define and enforce how browser operations
interact with backups, checks, and mounts.

**Acceptance tests** - Attempt Forget and Prune simultaneously; verify
only one starts. - Attempt a browser destructive operation during a
backup/check; verify it is queued or rejected safely. - Stop an active
browser operation; verify the correct process is terminated. - Verify
operation state is restored after success, failure, and cancellation.

### \[x\] 5. Make mount operations use a consistent configuration and lifecycle

**Area:** `restic_indicator.py` --- `_mount_backup()`, `_run_mount()`,
`_build_friendly_views()`

Mounting is separate from the normal operation queue. A mount can remain
associated with repository A while settings change to repository B, and
later friendly-view generation may consult the new configuration. Mount
activity also needs to be considered when deciding whether
repository-destructive operations can run.

**Required changes** - Capture an immutable configuration snapshot when
mounting begins. - Pass that snapshot through mount startup, repository
listing, and friendly-view generation. - Prevent Forget/Prune or other
incompatible repository operations while a mount is active, unless
explicitly supported. - Integrate mounting with the operation
coordinator or provide equivalent lifecycle and mutual-exclusion
guarantees. - Ensure unmount and error cleanup release all associated
state.

**Acceptance tests** - Mount repository A, change settings to B, and
verify all mount-related work continues to use A consistently. - Attempt
Forget/Prune while mounted; verify documented safe behavior. - Verify
cancellation, mount failure, and unmount clean up process and UI state.

### \[x\] 6. Detect incomplete Restic query output and preserve existing friendly views on failure

**Area:** `restic_runner.py` --- `_LineTail`, `run_ls_json()`,
`run_ls_children()`, `run_snapshots_json()`; `restic_views.py` view
builders

Bounded output retention is useful, but query functions must not
silently interpret truncated output or command failures as complete,
empty results. Friendly-view rebuilding must not destroy a previously
valid view before a complete replacement is ready.

**Required changes** - Make truncation explicit in query results. -
Prefer streaming parsing or temporary-file spooling for large
listings. - Distinguish successful empty results from command failure or
incomplete output. - Build replacement views in a temporary location. -
Replace the existing view only after the complete listing and
replacement view have been generated successfully. - Preserve the
previous valid view when rebuilding fails.

**Acceptance tests** - Simulate output larger than the retained-output
limit; verify truncation is detected or the full listing is parsed. -
Simulate a failed snapshot listing; verify it is not treated as an empty
repository. - Simulate a failed or truncated friendly-view rebuild;
verify the previous view remains intact. - Verify a successful empty
listing is represented correctly.

### \[x\] 7. Test the actual SFTP subsystem for SFTP-only SSH servers

**Area:** `restic_runner.py` --- `ssh_probe_command()`,
`ssh_transport_ok()`

A probe that executes a remote command such as `echo OK` does not prove
that the SFTP subsystem is available. An SSH account may permit SFTP
while disallowing ordinary remote command execution.

**Required changes** - Prefer testing the SFTP subsystem using the same
SSH options and configuration used by Restic. - Alternatively, remove
the mandatory preflight and let Restic establish the actual SFTP
connection. - Ensure connection testing and backup transport use
consistent connection mechanisms.

**Acceptance tests** - Test against an SSH account that permits SFTP but
rejects remote shell commands. - Verify the preflight accepts a working
SFTP transport, or that Restic performs the authoritative connection
attempt. - Verify host, port, identity file, ProxyJump, and other
supported SSH options are applied consistently.

## Priority 3: Scalability and status improvements

### \[x\] 8. Improve directory scanning memory behavior without imposing file-count limits

**Area:** `restic_split.py` --- `_listdir()`, `scan()`

The current `_listdir()` materializes an entire directory with
`list(os.scandir(path))`. This can create substantial temporary memory
overhead for directories containing millions of entries.

**Important design constraint** - Do not impose a hard maximum on the
number of files that can be backed up. - Keep the existing planner
fallback behavior that leaves excess files in the parent `rest` unit. -
A large directory must remain eligible for a complete backup.

**Potential future improvement** - Stream `os.scandir()` entries rather
than materializing the entire directory. - If the planner needs to
retain or revisit directory-entry lists, consider a spill-to-cache
design. - Record scan errors explicitly rather than silently treating an
inaccessible directory as empty. - Preserve post-order directory-size
accounting and cancellation behavior.

**Acceptance tests** - Scan a directory with a very large number of
entries and verify all entries remain represented in backup coverage. -
Measure peak RSS and scan duration. - Verify inaccessible directories
produce visible scan errors rather than being treated as empty. - Verify
cancellation closes outstanding directory iterators.

### \[x\] 9. Ensure failed preflight and skipped operations cannot leave a misleading success indicator

**Area:** `restic_indicator.py` --- `_op_worker()` and status updates

When a scheduled operation is skipped because SSH preflight fails, the
UI must not continue presenting an old successful state as though the
latest attempt succeeded.

**Required changes** - Record failed attempts separately from the last
successful backup/check. - Display a warning or error state when the
latest scheduled attempt fails or is skipped. - Preserve the timestamp
of the last actual successful backup/check. - Distinguish success,
partial success, failure, cancellation, and skipped operations.

**Acceptance tests** - Start from a successful state, fail SSH
preflight, and verify the UI shows a warning/error. - Verify the
last-success timestamp remains unchanged. - Verify a later successful
operation clears the warning appropriately.

## Deferred future update: Zstandard-compressed file-list cache

**Status: Deferred. Not part of the current fix batch.**

A future scalability update may add a Zstandard level 1 cache for
serialized directory-entry/file-list data, either in memory or in a
cache directory on disk.

The motivation is that large filename lists contain substantial
redundancy: repeated path prefixes, separators, and common filename
patterns. Compressing serialized records may reduce the memory or disk
footprint of retained planning data.

Potential design considerations: - Use binary, length-prefixed records;
filenames can legally contain newline characters. - Store
parent-directory prefixes once and encode child names relative to them
where practical. - Use independent Zstandard frames plus an offset index
if random access is required. - Bound in-memory compressed buffers and
spill to a private cache directory when appropriate. - Use restrictive
cache-directory and file permissions. - Clean up cache data on normal
completion, cancellation, and exceptions. - Benchmark against both the
current implementation and streaming `os.scandir()`.

Do not add an arbitrary file-count cap as a substitute. Do not make
compression a prerequisite for the current correctness fixes.

## Suggested implementation order

1.  Configuration-identity-safe operation bookkeeping.
2.  Actual-backup `.zfs` exclusion.
3.  Unified cancellation and retry scheduling.
4.  Snapshot-browser operation serialization.
5.  Mount configuration and lifecycle consistency.
6.  Safe handling of truncated/failed query output and friendly-view
    replacement.
7.  SFTP-compatible SSH preflight.
8.  Directory-scanning memory improvement, without file-count
    restrictions.
9.  Accurate failure/skipped status reporting.

The deferred Zstandard cache should be designed and benchmarked
separately after the correctness and lifecycle fixes are complete.

## Verification notes

A source-level review and headless tests are useful, but they do not
replace integration testing with the actual Restic executable.
GTK-dependent tests also need to run in an environment with PyGObject
installed.

Before release, run: - The complete test suite, including GTK-dependent
tests. - Restic integration tests against a real executable. - SSH/SFTP
tests against ordinary and SFTP-only server configurations. - ZFS
exclusion tests where ZFS is available. - Large-directory tests that
measure memory use without limiting backup coverage.
