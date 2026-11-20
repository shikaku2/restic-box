# Scan memory: what the split planner costs, measured

Measured 2026-11-20 on this box with `qwen3.8-flash-next-iq3_xxs` **unloaded**
(49 GiB available), so nothing in these runs was compressed into zram or swapped:
`zramctl` showed 1.5 GiB of pre-existing zram data and `/proc/vmstat` recorded
2068 swap-in pages, **0 swap-out pages** across the whole measurement.

Two different costs are being measured, and only one of them scales with the
number of files.

## 1. Walking the tree (what fix #8 changed)

Synthetic lazy trees, 228-character paths, all directories past `MAX_LISTED_FILES`
(50,000). `tracemalloc` peak = the most the walk ever held at one moment.

| tree | files | walk peak | after the walk | seconds |
|---|---|---|---|---|
| 10 dirs x 100,000 | 1,000,000 | 17.9 MiB | 0.1 MiB | 26.9 |
| 100 dirs x 100,000 | **10,000,000** | **18.1 MiB** | 0.4 MiB | 275.8 |

A ten-times-bigger tree costs the same memory. The streamed walk's peak is set by
`MAX_LISTED_FILES` times the path length — it builds a list of up to 50,000
`(path, size)` tuples before discovering the directory is too big to plan
individually, then drops it. It is not set by how big the directory is.

Peak RSS (`VmHWM`, fresh child process per walk, short-named 200k-file pool):
streamed 7.7 MiB against 98 MiB materialized. Long-named 8 x 55,000:
streamed 17.6 MiB against 41.6 MiB materialized. Both walks agree on bytes and
directory counts.

## 2. The planner's own file lists (fix #8 left this alone, on purpose)

Every directory **under** the cap keeps one `(path, size)` tuple per small file,
and those tuples carry full paths. This grows linearly with the file count:

| tree (228-char paths, every dir under the cap) | files | peak = retained | per file | seconds |
|---|---|---|---|---|
| 200 dirs x 1,000 | 200,000 | 72 MiB | 378 B | 6.1 |
| 1,000 dirs x 1,000 | 1,000,000 | 372 MiB | 390 B | 32.1 |
| 2,000 dirs x 1,000 | 2,000,000 | 774 MiB | 406 B | 64.1 |

Where the bytes are at 2,000,000 files (`tracemalloc` line attribution):

| retained | count | what it is |
|---|---|---|
| 513.1 MiB | 2,000,000 | the full path strings inside the tuples (~256 B each) |
| 138.9 MiB | 2,002,000 | the `(path, size)` tuples themselves (~69 B each) |
| 61.0 MiB | 2,000,000 | per-entry stat-derived values |
| 59.1 MiB | 4,194 | **test harness bookkeeping**, not app cost |

So roughly two thirds of the retention is path text, and the per-file cost is
about `1.1 x path length + 140` bytes.

## 3. How many files before it hurts

Extrapolating the measured ~400 B/file (arithmetic, not measurement):

| files in directories under the cap | planner retention |
|---|---|
| 1M | 0.4 GiB |
| 2M | 0.8 GiB (measured) |
| 5M | ~2.0 GiB |
| 10M | ~4.0 GiB |
| 20M | ~8.0 GiB |

Against this box: 62 GiB total, 49 GiB free with the served model unloaded, but
only about **4 GiB free while the served model is loaded**. A backup over a tree
of a few million medium-path files therefore runs comfortably with the model
unloaded and starts to crowd the machine with the model loaded. Longer paths make
it proportionally worse: at 400-character paths the same 10M files cost ~6 GiB.

The one shape that stays safe at any size is a single enormous directory: 10M
loose files peaked at 18 MiB, because the cap drops the detail list (and coverage
is unaffected — `tests/test_split_streaming.py` proves every file still lands in
the repository).

## 4. A real directory, not a synthetic one

`tests/test_split_streaming.py` builds a real 60,000-file directory: three
long-named levels, 60-character file names, **326-character paths**, plus a 4 MiB
carved child. Built on tmpfs, because the planner measures `st_blocks * 512` and
the ZFS scratch dataset (compression=on) does not report allocated bytes truthfully
(a fresh 4 MiB file there reports 140 KiB, and random data 1 KiB). A guard test in
that module fails if the fixture's filesystem stops accounting blocks honestly.

| walk | peak RSS | cap-derived ceiling | seconds |
|---|---|---|---|
| streamed (the app) | 21 MiB | 26 MiB | 0.19 |
| materialized (pre-fix) | 78 MiB | — | 0.26 |

The ceiling is `MAX_LISTED_FILES × (path length + 240)`: before the walk discovers a
directory is too big to plan file by file it has already appended up to 50,000
`(path, size)` tuples, and dropping them is the peak. That is why the streamed
walk's memory is bounded by the cap and the path length rather than by the size of
the directory — and why the real-fixture numbers sit just under the ceiling.

## 5. Ways to make the list cheaper, with expected savings

1. **Share the path prefix** — store `(name, size)` per directory and join the
   directory back on when an exclude pattern or an argv target is built. Targets
   the dominant 513 MiB term: at 2M files/228-char paths, expected
   774 MiB → ~400 MiB. Local to `scan()`/`_carve()`/`_pack()`, no new caps.
2. **Carve bottom-up during the walk** so each directory's list is released as
   soon as it is consumed. Bounds *scan* retention to the widest single directory.
   Does not help a directory that gets *grouped*: `group` units carry full
   `paths` lists (that is the group unit's backup argv), and `save_plan` writes
   every one of those paths to `split-plans/*.json` anyway.
3. **Spill to disk and re-read lazily** — the deferred Zstandard file-list cache.
   Bounded memory, more moving parts, has to survive changed trees.

`_pack()` sorts by size (first-fit-decreasing), so any directory that is actually
grouped needs its whole list at that moment: pure lazy streaming does not help
there either.

## 6. Deferred on purpose

Option 1 (prefix-sharing) is the change that flattens the linear term in section 2 —
it attacks the 513 MiB of path text at 2M files. Deferred 2026-11-20 so that this
batch stays a batch of bug and oversight fixes. Option 2 is deferred with it, for the
reason in option 2's own paragraph: grouped directories have to remember their paths
either way.
