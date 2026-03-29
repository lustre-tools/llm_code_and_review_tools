#!/usr/bin/env python3
"""Lustre vmcore triage — one-shot analysis for any Lustre vmcore.

Runs all available analyses and produces a single structured report
suitable for an LLM or engineer to quickly understand the crash.

Usage:
    python3 lustre_triage.py --vmcore <path> --vmlinux <path> \
        --mod-dir <ko_dir> [--debug-dir <debug_dir>] [--pretty]

Output sections:
    1. System overview (hostname, kernel, CPUs, platform)
    2. Crash summary (crashed task, backtrace, trigger)
    3. Local variables from Lustre frames
    4. Active OBD devices with import states
    5. In-flight RPCs
    6. LDLM namespace summary
    6b. OSC grant/dirty page stats (interesting devices only)
    6c. Lustre wait queue analysis (D-state tasks in Lustre code)
    7. Unique stack traces (top 10 most common)
    8. DK log tail (last 50 lines)
    9. Diagnosis hints (automated pattern matching)
"""

import argparse
import json
import sys
import time

try:
    from . import lustre_helpers as lh
    from .lustre_analyze import load_program, analyze_overview, analyze_backtrace, analyze_locals, analyze_obd_devices
except ImportError:
    import lustre_helpers as lh
    from lustre_analyze import load_program, analyze_overview, analyze_backtrace, analyze_locals, analyze_obd_devices

import drgn
from drgn.helpers.linux.list import list_for_each_entry


# ── Diagnosis patterns ───────────────────────────────────────


DIAGNOSIS_PATTERNS = [
    {
        "pattern": "LBUG",
        "description": "Lustre assertion failure (LBUG)",
        "advice": "Check the assertion message in the backtrace. This is a deliberate panic triggered by a failed LASSERT/LBUG.",
    },
    {
        "pattern": "cl_page_put",
        "description": "cl_page reference count issue",
        "advice": "Check cp_state and cp_ref in locals. Common in page lifecycle bugs.",
    },
    {
        "pattern": "ptlrpc_send_new_req",
        "description": "RPC send path",
        "advice": "Check import state and connection. May indicate recovery or eviction issues.",
    },
    {
        "pattern": "ldlm_lock_cancel",
        "description": "Lock cancellation path",
        "advice": "Check for lock mode conflicts, resource name, and whether this is during eviction.",
    },
    {
        "pattern": "osc_io_submit",
        "description": "OSC I/O submission",
        "advice": "Check for quota errors (EDQUOT=-122), grant issues, or OST connection problems.",
    },
    {
        "pattern": "EDQUOT",
        "description": "Quota exceeded",
        "advice": "A quota limit was hit. Check which OST/user/group and the quota settings.",
    },
    {
        "pattern": "ll_readpage",
        "description": "Lustre readpage path",
        "advice": "Check readahead state, extent locks, and page cache behavior.",
    },
    {
        "pattern": "mdc_reint",
        "description": "MDC reintegration (metadata update)",
        "advice": "Metadata operation in progress. Check the reint opcode and MDT connection.",
    },
]


def diagnose_from_backtrace(backtrace_result):
    """Match backtrace frames against known diagnosis patterns."""
    hints = []
    all_frames = " ".join(
        f.get("description", "") + " " + f.get("function", "")
        for f in backtrace_result.get("frames", [])
    )

    for pattern in DIAGNOSIS_PATTERNS:
        if pattern["pattern"].lower() in all_frames.lower():
            hints.append({
                "pattern": pattern["pattern"],
                "description": pattern["description"],
                "advice": pattern["advice"],
            })

    return hints


def diagnose_from_locals(locals_result):
    """Extract diagnosis info from local variables."""
    hints = []
    for frame in locals_result.get("frames", []):
        lvars = frame.get("locals", {})

        # Check for rc values
        if "rc" in lvars:
            rc = lvars["rc"]
            if isinstance(rc, int) and rc < 0:
                errno_map = {
                    -1: "EPERM", -2: "ENOENT", -5: "EIO",
                    -12: "ENOMEM", -22: "EINVAL", -28: "ENOSPC",
                    -110: "ETIMEDOUT", -111: "ECONNREFUSED",
                    -122: "EDQUOT", -131: "ECONNRESET",
                }
                name = errno_map.get(rc, f"errno {-rc}")
                hints.append({
                    "pattern": f"rc={rc}",
                    "description": f"Error return code: {name}",
                    "frame": frame.get("description", ""),
                })

    return hints


# ── Namespace summary ────────────────────────────────────────


def get_namespace_summary(prog):
    """Get a summary of LDLM namespaces (count, total locks)."""
    namespaces = []

    for ns_sym, side in [
        ("ldlm_cli_active_namespace_list", "client"),
        ("ldlm_cli_inactive_namespace_list", "inactive"),
        ("ldlm_srv_namespace_list", "server"),
    ]:
        try:
            ns_list = prog[ns_sym]
        except (KeyError, LookupError):
            continue

        for ns in list_for_each_entry(
            "struct ldlm_namespace", ns_list.address_of_(), "ns_list_chain"
        ):
            try:
                name = lh.obd_name(ns.ns_obd[0]) if ns.ns_obd.value_() != 0 else "?"
                granted = ns.ns_pool.pl_granted.counter.value_()
                unused = ns.ns_nr_unused.value_()
                if granted > 0 or unused > 0:
                    namespaces.append({
                        "name": name,
                        "side": side,
                        "granted": granted,
                        "unused": unused,
                    })
            except (drgn.FaultError, drgn.ObjectAbsentError):
                continue

    return {
        "total_namespaces": len(namespaces),
        "namespaces_with_locks": [ns for ns in namespaces if ns["granted"] > 0],
    }


# ── Unique stack trace summary ───────────────────────────────


def get_stack_trace_summary(prog, max_traces=10):
    """Get top N most common stack traces."""
    from collections import defaultdict

    try:
        from drgn.helpers.linux.pid import for_each_task
        from drgn.helpers.linux.sched import task_state_to_char
    except ImportError:
        return {"error": "drgn helpers not available"}

    traces = defaultdict(list)

    for task in for_each_task(prog):
        try:
            comm = task.comm.string_().decode(errors="replace")
            pid = task.pid.value_()

            if comm.startswith("swapper/"):
                continue

            try:
                state_char = task_state_to_char(task)
            except Exception:
                state_char = "?"

            try:
                trace = prog.stack_trace(task)
                frames = []
                for frame in trace:
                    desc = str(frame)
                    if " in " in desc:
                        parts = desc.split(" in ", 1)
                        frames.append(parts[1].strip())
                    else:
                        frames.append(desc.strip())
                key = "\n".join(frames[:5])  # Top 5 frames as key
            except (drgn.FaultError, drgn.ObjectAbsentError, ValueError):
                key = "<unavailable>"

            traces[key].append({"pid": pid, "comm": comm, "state": state_char})

        except (drgn.FaultError, drgn.ObjectAbsentError):
            continue

    # Sort by count descending, return top N
    sorted_traces = sorted(traces.items(), key=lambda x: len(x[1]), reverse=True)

    return {
        "unique_traces": len(sorted_traces),
        "total_tasks": sum(len(v) for v in traces.values()),
        "top_traces": [
            {
                "count": len(tasks),
                "states": {s: sum(1 for t in tasks if t["state"] == s) for s in set(t["state"] for t in tasks)},
                "sample_comms": list(set(t["comm"] for t in tasks))[:5],
                "top_frames": key.split("\n")[:3],
            }
            for key, tasks in sorted_traces[:max_traces]
        ],
    }


# ── DK log tail ──────────────────────────────────────────────


def get_dk_tail(prog, max_lines=50):
    """Get the last N dk log lines."""
    try:
        try:
            from .dk import walk_trace_data
        except ImportError:
            from dk import walk_trace_data

        lines, error = walk_trace_data(prog)
        if error:
            return {"error": error}
        if not lines:
            return {"count": 0, "lines": []}

        # Return last N lines
        tail = lines[-max_lines:]
        return {
            "total_lines": len(lines),
            "showing_last": len(tail),
            "lines": [line for _, line in tail],
        }
    except Exception as e:
        return {"error": str(e)}


# ── RPC summary ──────────────────────────────────────────────


def get_rpc_summary(prog):
    """Get summary of in-flight RPCs."""
    try:
        try:
            from .ptlrpc import foreach_ptlrpcd_ctl, get_request_info
        except ImportError:
            from ptlrpc import foreach_ptlrpcd_ctl, get_request_info

        sent_count = 0
        pend_count = 0
        rpcs = []

        def count_rpcs(prog, pd, sent, pend):
            name = pd.pc_name.string_().decode(errors="replace")
            try:
                new = pd.pc_set.set_new_count.counter.value_()
                remaining = pd.pc_set.set_remaining.counter.value_()
                if new > 0 or remaining > 0:
                    rpcs.append({
                        "thread": name,
                        "new": new,
                        "remaining": remaining,
                    })
            except (drgn.FaultError, AttributeError):
                pass

        foreach_ptlrpcd_ctl(prog, count_rpcs, sent_count, pend_count)

        return {
            "threads_with_rpcs": len(rpcs),
            "details": rpcs,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Main triage ──────────────────────────────────────────────


def run_triage(prog, verbose=False):
    """Run full triage and return structured report."""
    report = {"triage_version": "1.0"}
    t0 = time.time()

    # 1. Overview
    try:
        report["overview"] = analyze_overview(prog)
    except Exception as e:
        report["overview"] = {"error": str(e)}

    # 2. Backtrace
    try:
        report["backtrace"] = analyze_backtrace(prog)
    except Exception as e:
        report["backtrace"] = {"error": str(e)}

    # 3. Local variables
    try:
        report["locals"] = analyze_locals(prog)
    except Exception as e:
        report["locals"] = {"error": str(e)}

    # 4. OBD devices
    try:
        report["obd_devices"] = analyze_obd_devices(prog)
    except Exception as e:
        report["obd_devices"] = {"error": str(e)}

    # 5. RPC summary
    try:
        report["rpcs"] = get_rpc_summary(prog)
    except Exception as e:
        report["rpcs"] = {"error": str(e)}

    # 6. Namespace summary
    try:
        report["namespaces"] = get_namespace_summary(prog)
    except Exception as e:
        report["namespaces"] = {"error": str(e)}

    # 6b. OSC grant/dirty stats
    try:
        try:
            from .osc_stats import get_osc_stats
        except ImportError:
            from osc_stats import get_osc_stats
        osc = get_osc_stats(prog)
        # Only include devices with non-zero dirty pages or lost grant
        interesting = [d for d in osc["devices"]
                       if d["dirty_pages"] > 0 or d["lost_grant"] > 0]
        report["osc_stats"] = {
            "total_osc_devices": osc["count"],
            "devices_with_dirty_pages": len([d for d in osc["devices"] if d["dirty_pages"] > 0]),
            "devices_with_lost_grant": len([d for d in osc["devices"] if d["lost_grant"] > 0]),
            "interesting_devices": interesting,
        }
    except Exception as e:
        report["osc_stats"] = {"error": str(e)}

    # 6c. Lustre wait queue analysis (D-state tasks in Lustre)
    try:
        try:
            from .lustre_waitq import find_lustre_waiters
        except ImportError:
            from lustre_waitq import find_lustre_waiters
        report["lustre_waiters"] = find_lustre_waiters(prog)
    except Exception as e:
        report["lustre_waiters"] = {"error": str(e)}

    # 7. Stack trace summary
    try:
        report["stack_traces"] = get_stack_trace_summary(prog)
    except Exception as e:
        report["stack_traces"] = {"error": str(e)}

    # 8. DK log tail
    try:
        report["dk_log"] = get_dk_tail(prog)
    except Exception as e:
        report["dk_log"] = {"error": str(e)}

    # 8b. Kernel log (printk) tail
    try:
        from drgn.helpers.linux.printk import get_printk_records
        records = list(get_printk_records(prog))
        tail = records[-80:]
        report["kernel_log"] = {
            "total_records": len(records),
            "showing_last": len(tail),
            "lines": [
                f"[{r.timestamp / 1e9:.6f}] {r.text.decode(errors='replace')}"
                for r in tail
            ],
        }
    except Exception as e:
        report["kernel_log"] = {"error": str(e)}

    # 9. Diagnosis
    hints = []
    if "backtrace" in report and "error" not in report["backtrace"]:
        hints.extend(diagnose_from_backtrace(report["backtrace"]))
    if "locals" in report and "error" not in report["locals"]:
        hints.extend(diagnose_from_locals(report["locals"]))
    report["diagnosis"] = {"hints": hints}

    report["elapsed_seconds"] = round(time.time() - t0, 1)
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Lustre vmcore triage — one-shot analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--vmcore", required=True)
    parser.add_argument("--vmlinux", required=True)
    parser.add_argument("--mod-dir", default=None,
                        help="Directory with Lustre .ko files")
    parser.add_argument("--debug-dir", default=None,
                        help="Directory with Lustre .debug files")
    parser.add_argument("--pretty", action="store_true",
                        help="Pretty-print JSON output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    prog = load_program(
        args.vmcore, args.vmlinux, args.mod_dir, args.debug_dir,
    )

    report = run_triage(prog, verbose=args.verbose)
    lh.json_output(report, args)


if __name__ == "__main__":
    main()
