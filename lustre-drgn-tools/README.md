# lustre-drgn-tools

drgn-based Lustre vmcore analysis tools. Cross-architecture capable
(e.g., analyze aarch64 vmcores on an x86_64 host).

Ported from the epython scripts in `contrib/debug_tools/epython_scripts/`
in the Lustre source tree, plus new analysis tools.

## Quick start

```bash
# Install drgn
./install-drgn.sh

# One-shot triage (runs all analyses, outputs JSON)
python3 lustre_triage.py \
    --vmcore /path/to/vmcore \
    --vmlinux /path/to/vmlinux \
    --mod-dir /path/to/lustre/*.ko \
    --debug-dir /path/to/lustre/*.debug \
    --pretty

# Individual scripts
python3 obd_devs.py --vmcore ... --vmlinux ... --pretty
python3 ldlm_dumplocks.py --vmcore ... --vmlinux ... --pretty
python3 ptlrpc.py --vmcore ... --vmlinux ... --pretty
python3 dk.py --vmcore ... --vmlinux ... --pretty
```

## Scripts

### Triage / analysis

| Script | Description |
|--------|-------------|
| `lustre_triage.py` | One-shot triage: runs all analyses, outputs a single JSON report with overview, backtrace, locals, OBD devices, RPCs, namespaces, OSC stats, wait queues, stack traces, dk log, and diagnosis hints |
| `lustre_analyze.py` | Core analysis engine: overview, backtrace, locals, OBD devices. Used by triage and as a library |

### Ported from epython

| Script | Original | Description |
|--------|----------|-------------|
| `obd_devs.py` | `obd_devs.py` | OBD device listing with import state, NIDs, connection count |
| `ldlm_dumplocks.py` | `ldlm_dumplocks.py` | LDLM lock dump per namespace: granted/waiting locks, extents, modes, flags |
| `ptlrpc.py` | `ptlrpc.py` | RPC queue inspection: ptlrpcd threads, sent/pending requests, phases |
| `dk.py` | `dk.py` | Debug kernel log extraction from in-memory trace buffers |
| `uniqueStacktrace.py` | `uniqueStacktrace.py` | Unique stack trace grouping by frequency |
| `dump_lustre_hashes.py` | `cfs_hashes.py` | Hash table summary (cfs_hash and rhashtable) for all Lustre subsystems |

### New tools

| Script | Description |
|--------|-------------|
| `osc_stats.py` | OSC grant, dirty pages, and lost grant per OST connection |
| `lustre_waitq.py` | Find D-state tasks blocked in Lustre code, grouped by wait point |

### Libraries

| File | Description |
|------|-------------|
| `lustre_helpers.py` | Shared helpers: NID formatting, OBD device iteration, cfs_hash traversal, rb-tree, LDLM modes, RPC opcodes, output formatting |

## Output format

All scripts default to JSON output. Options:

- `--pretty` — indented JSON
- `--text` — human-readable text output
- `LUSTRE_DRGN_PRETTY=1` — environment variable, same as `--pretty`

## Arguments

All scripts accept:

```
--vmcore PATH      Path to vmcore file
--vmlinux PATH     Path to vmlinux (with debug info)
--mod-dir PATH     Directory containing Lustre .ko files (optional)
--debug-dir PATH   Directory containing Lustre .debug files (optional)
```

The `--mod-dir` and `--debug-dir` are needed for Lustre symbol resolution.
Without them, you'll get kernel-only analysis.

## Install

```bash
./install-drgn.sh           # Full install (deps + drgn)
./install-drgn.sh --deps    # System dependencies only
./install-drgn.sh --check   # Verify installation
```

Supports Rocky/RHEL 8+, Ubuntu/Debian, and macOS. Works on x86_64 and aarch64.

## License

BSD 2-Clause. See [LICENSE](LICENSE).
