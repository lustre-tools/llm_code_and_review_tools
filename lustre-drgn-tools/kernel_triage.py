#!/usr/bin/env python3
"""Generic kernel vmcore triage using drgn.

Replaces the crash-binary overview/backtrace/memory/io recipes
with pure drgn equivalents.  No crash binary required.

Usage:
    python3 kernel_triage.py --vmcore <path> --vmlinux <path> \
        [--pretty] [analysis...]

Analyses:
    overview    System info, uptime, panic message, task summary
    backtrace   All CPU backtraces and panic task detail
    memory      Memory usage and slab cache stats
    io          Block devices and hung (D-state) tasks
    all         Run all analyses (default)
"""

import argparse
import json
import sys
import traceback

import drgn
from drgn.helpers.linux.pid import for_each_task, find_task


def load_program(vmcore, vmlinux):
    """Load a vmcore into a drgn Program."""
    prog = drgn.Program()
    prog.set_core_dump(vmcore)
    prog.load_debug_info([vmlinux], default=True)
    return prog


# ── Overview ─────────────────────────────────────────────────


def analyze_overview(prog):
    """System info, uptime, panic message, task summary."""
    from drgn.helpers.linux.pid import for_each_task

    result = {}

    # Basic system info
    try:
        uts = prog["init_uts_ns"].name
        result["hostname"] = uts.nodename.string_().decode()
        result["kernel"] = uts.release.string_().decode()
        result["machine"] = uts.machine.string_().decode()
    except Exception as e:
        result["system_error"] = str(e)

    # CPU count
    try:
        from drgn.helpers.linux.cpumask import (
            num_online_cpus,
            num_possible_cpus,
        )
        result["cpus_online"] = num_online_cpus(prog)
        result["cpus_possible"] = num_possible_cpus(prog)
    except Exception:
        pass

    # Uptime
    try:
        from drgn.helpers.linux.ktime import (
            ktime_get_seconds,
        )
        secs = int(ktime_get_seconds(prog))
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs_r = divmod(rem, 60)
        result["uptime_seconds"] = secs
        result["uptime_pretty"] = (
            f"{days}d {hours}h {mins}m {secs_r}s"
        )
    except Exception:
        pass

    # Panic message
    try:
        from drgn.helpers.linux.boot import panic_message
        msg = panic_message(prog)
        if msg:
            result["panic_message"] = msg
    except Exception:
        pass

    # Panic task
    try:
        from drgn.helpers.linux.boot import panic_task
        task = panic_task(prog)
        if task:
            result["panic_task"] = {
                "comm": task.comm.string_().decode(),
                "pid": int(task.pid),
            }
    except Exception:
        pass

    # Task state summary
    try:
        states = {}
        for task in for_each_task(prog):
            try:
                from drgn.helpers.linux.sched import (
                    get_task_state,
                )
                state = get_task_state(task)
            except Exception:
                state = "?"
            states[state] = states.get(state, 0) + 1
        result["task_states"] = states
        result["task_count"] = sum(states.values())
    except Exception as e:
        result["task_error"] = str(e)

    return result


# ── Backtrace ────────────────────────────────────────────────


def analyze_backtrace(prog):
    """All CPU backtraces and panic task detail."""
    result = {}

    # Panic task backtrace
    try:
        from drgn.helpers.linux.boot import panic_task
        task = panic_task(prog)
        if task:
            trace = prog.stack_trace(task)
            frames = []
            for frame in trace:
                entry = {"function": frame.name or "??"}
                try:
                    sl = frame.source()
                    if sl:
                        entry["file"] = sl[0]
                        entry["line"] = sl[1]
                except Exception:
                    pass
                frames.append(entry)
            result["panic_task"] = {
                "comm": task.comm.string_().decode(),
                "pid": int(task.pid),
                "backtrace": frames,
            }
    except Exception as e:
        result["panic_task_error"] = str(e)

    # Per-CPU current task backtraces
    try:
        from drgn.helpers.linux.cpumask import (
            num_online_cpus,
            for_each_online_cpu,
        )
        from drgn.helpers.linux.sched import cpu_curr

        cpu_traces = []
        for cpu in for_each_online_cpu(prog):
            try:
                task = cpu_curr(prog, cpu)
                trace = prog.stack_trace(task)
                frames = []
                for frame in trace:
                    entry = {"function": frame.name or "??"}
                    try:
                        sl = frame.source()
                        if sl:
                            entry["file"] = sl[0]
                            entry["line"] = sl[1]
                    except Exception:
                        pass
                    frames.append(entry)
                cpu_traces.append({
                    "cpu": cpu,
                    "comm": task.comm.string_().decode(),
                    "pid": int(task.pid),
                    "backtrace": frames,
                })
            except Exception as e:
                cpu_traces.append({
                    "cpu": cpu,
                    "error": str(e),
                })
        result["cpu_backtraces"] = cpu_traces
    except Exception as e:
        result["cpu_backtraces_error"] = str(e)

    return result


# ── Memory ───────────────────────────────────────────────────


def analyze_memory(prog):
    """Memory usage and slab cache stats."""
    result = {}

    # Total memory
    try:
        from drgn.helpers.linux.mm import totalram_pages
        page_size = prog["PAGE_SIZE"].value_() if "PAGE_SIZE" in prog else 4096
        try:
            page_size = int(page_size)
        except Exception:
            page_size = 4096
        total_pages = totalram_pages(prog)
        result["total_ram_bytes"] = int(total_pages) * page_size
        result["total_ram_mb"] = int(total_pages) * page_size // (1024 * 1024)
    except Exception as e:
        result["total_ram_error"] = str(e)

    # Free pages
    try:
        from drgn.helpers.linux.mm import nr_free_pages
        free = nr_free_pages(prog)
        result["free_pages"] = int(free)
        result["free_mb"] = int(free) * page_size // (1024 * 1024)
    except Exception:
        pass

    # Slab cache summary
    try:
        from drgn.helpers.linux.slab import (
            for_each_slab_cache,
            slab_cache_usage,
        )
        caches = []
        total_slab_bytes = 0
        for cache in for_each_slab_cache(prog):
            try:
                name = cache.name.string_().decode()
                usage = slab_cache_usage(cache)
                allocated = usage.allocated_objects
                total = usage.total_objects
                total_bytes = usage.total_allocated_size
                total_slab_bytes += total_bytes
                caches.append({
                    "name": name,
                    "allocated_objects": allocated,
                    "total_objects": total,
                    "size_bytes": total_bytes,
                })
            except Exception:
                continue
        # Sort by size, show top 20
        caches.sort(key=lambda x: x["size_bytes"], reverse=True)
        result["slab_caches"] = caches[:20]
        result["slab_total_mb"] = total_slab_bytes // (1024 * 1024)
        result["slab_cache_count"] = len(caches)
    except Exception as e:
        result["slab_error"] = str(e)

    return result


# ── I/O ──────────────────────────────────────────────────────


def analyze_io(prog):
    """Block devices and hung (D-state) tasks."""
    result = {}

    # Block devices
    try:
        from drgn.helpers.linux.block import for_each_disk
        disks = []
        for disk in for_each_disk(prog):
            try:
                from drgn.helpers.linux.block import disk_name
                name = disk_name(disk)
            except Exception:
                try:
                    name = disk.disk_name.string_().decode()
                except Exception:
                    name = "?"
            disks.append(name)
        result["block_devices"] = disks
    except Exception as e:
        result["block_devices_error"] = str(e)

    # D-state (uninterruptible sleep) tasks
    try:
        from drgn.helpers.linux.sched import get_task_state

        hung_tasks = []
        for task in for_each_task(prog):
            try:
                state = get_task_state(task)
                if state in ("D", "UN"):
                    # Get backtrace
                    frames = []
                    try:
                        trace = prog.stack_trace(task)
                        for frame in trace:
                            entry = {
                                "function": frame.name or "??",
                            }
                            try:
                                sl = frame.source()
                                if sl:
                                    entry["file"] = sl[0]
                                    entry["line"] = sl[1]
                            except Exception:
                                pass
                            frames.append(entry)
                    except Exception:
                        pass
                    hung_tasks.append({
                        "comm": task.comm.string_().decode(),
                        "pid": int(task.pid),
                        "backtrace": frames,
                    })
            except Exception:
                continue
        result["d_state_tasks"] = hung_tasks
        result["d_state_count"] = len(hung_tasks)
    except Exception as e:
        result["d_state_error"] = str(e)

    return result


# ── Kernel log ───────────────────────────────────────────────


def analyze_dmesg(prog, tail=50):
    """Extract kernel log (printk) records."""
    try:
        from drgn.helpers.linux.printk import get_printk_records
        records = list(get_printk_records(prog))
        lines = []
        for r in records:
            try:
                lines.append(r.text.decode(errors="replace"))
            except Exception:
                try:
                    lines.append(str(r))
                except Exception:
                    continue
        if tail and len(lines) > tail:
            lines = lines[-tail:]
        return {"dmesg": lines, "total_records": len(records)}
    except Exception as e:
        return {"dmesg_error": str(e)}


# ── Main ─────────────────────────────────────────────────────


ANALYSES = {
    "overview": analyze_overview,
    "backtrace": analyze_backtrace,
    "memory": analyze_memory,
    "io": analyze_io,
    "dmesg": analyze_dmesg,
}


def run_triage(prog, analyses=None):
    """Run requested analyses and return combined result."""
    if analyses is None or "all" in analyses:
        analyses = list(ANALYSES.keys())

    result = {}
    for name in analyses:
        if name not in ANALYSES:
            result[name] = {"error": f"unknown analysis: {name}"}
            continue
        try:
            fn = ANALYSES[name]
            if name == "dmesg":
                result[name] = fn(prog)
            else:
                result[name] = fn(prog)
        except Exception as e:
            result[name] = {
                "error": str(e),
                "traceback": traceback.format_exc(),
            }
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Generic kernel vmcore triage using drgn",
    )
    parser.add_argument(
        "--vmcore", required=True, help="Path to vmcore",
    )
    parser.add_argument(
        "--vmlinux", required=True, help="Path to vmlinux",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print JSON output",
    )
    parser.add_argument(
        "analyses", nargs="*", default=["all"],
        help="Analyses to run (overview, backtrace, memory, "
             "io, dmesg, all). Default: all",
    )
    args = parser.parse_args()

    prog = load_program(args.vmcore, args.vmlinux)
    result = run_triage(prog, args.analyses)

    indent = 2 if args.pretty else None
    json.dump(result, sys.stdout, indent=indent, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
