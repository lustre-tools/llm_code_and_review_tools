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
    dmesg       Kernel log (printk) records
    all         Run all analyses (default)
"""

import argparse
import json
import sys
import traceback

import drgn
from drgn.helpers.linux.pid import for_each_task


def load_program(vmcore, vmlinux):
    """Load a vmcore into a drgn Program."""
    prog = drgn.Program()
    prog.set_core_dump(vmcore)
    exc = None
    try:
        prog.load_debug_info([vmlinux], default=True)
    except drgn.MissingDebugInfoError as e:
        # Missing MODULE debug info is fine for kernel-level analysis,
        # but a missing/mismatched vmlinux is not -- check below.
        exc = e
    _verify_kernel_symbols(prog, vmlinux, exc)
    return prog

def _verify_kernel_symbols(prog, vmlinux, exc=None):
    """Fail loudly if the vmlinux did not actually load.

    drgn refuses a vmlinux whose GNU build ID does not match the
    vmcore ("did not match any loaded modules; ignoring") and only
    raises MissingDebugInfoError, which callers used to swallow.  The
    Program then has no kernel symbols and every later lookup dies
    with a baffling "could not find 'init_uts_ns'".  Turn that into
    one actionable message naming both build IDs.
    """
    try:
        prog["init_uts_ns"]
        return
    except Exception:
        pass

    def _build_id(path):
        try:
            import subprocess
            out = subprocess.run(["readelf", "-n", path],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                if "Build ID" in line:
                    return line.split(":")[-1].strip()
        except Exception:
            pass
        return "unknown"

    raise RuntimeError(
        "vmlinux does not match this vmcore, so no kernel symbols "
        "loaded.\n"
        "  vmlinux:  %s (build ID %s)\n"
        "  The vmcore's kernel has a different build ID -- a rebuilt "
        "kernel keeps the same version string but changes the build "
        "ID, so 'uname -r' matching is not sufficient.\n"
        "  Find the matching vmlinux (check the VM image the vmcore "
        "came from, or /usr/lib/debug/lib/modules/<kver>/vmlinux), or "
        "read vmcore-dmesg.txt from the crash dir instead.%s"
        % (vmlinux, _build_id(vmlinux),
           "\n  original drgn error: %s" % exc if exc else "")
    )



def _get_task_state_str(task):
    """Get task state as a string, with fallback."""
    try:
        from drgn.helpers.linux.sched import get_task_state
        return get_task_state(task)
    except Exception:
        return "?"


def _get_stack_frames(prog, task):
    """Get backtrace frames for a task."""
    frames = []
    try:
        trace = prog.stack_trace(task)
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
    except Exception:
        pass
    return frames


def _task_info(task):
    """Extract comm and pid from a task."""
    return {
        "comm": task.comm.string_().decode(errors="replace"),
        "pid": int(task.pid),
    }


# ── Overview ─────────────────────────────────────────────────


def analyze_overview(prog):
    """System info, uptime, panic message, task summary."""
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

    # Uptime -- try multiple approaches
    try:
        from drgn.helpers.linux.ktime import ktime_get_seconds
        secs = int(ktime_get_seconds(prog))
    except Exception:
        try:
            from drgn.helpers.linux.sched import uptime
            secs = int(uptime(prog))
        except Exception:
            secs = None

    if secs is not None:
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs_r = divmod(rem, 60)
        result["uptime_seconds"] = secs
        result["uptime_pretty"] = (
            f"{days}d {hours}h {mins}m {secs_r}s"
        )

    # Panic message
    try:
        from drgn.helpers.linux.boot import panic_message
        msg = panic_message(prog)
        if msg:
            result["panic_message"] = msg
    except Exception:
        pass

    # Panic task -- find via crashing_cpu or panic_cpu
    try:
        from drgn.helpers.linux.boot import panic_task
        task = panic_task(prog)
        if task:
            result["panic_task"] = _task_info(task)
    except Exception:
        # Fallback: find the task on crashing_cpu
        try:
            from drgn.helpers.linux.sched import cpu_curr
            cpu = int(prog["crashing_cpu"])
            task = cpu_curr(prog, cpu)
            result["panic_task"] = _task_info(task)
            result["panic_task"]["cpu"] = cpu
        except Exception:
            pass

    # Task state summary
    try:
        states = {}
        for task in for_each_task(prog):
            state = _get_task_state_str(task)
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

    # Find panic/crashing task
    panic_found = False
    try:
        from drgn.helpers.linux.boot import panic_task
        task = panic_task(prog)
        if task:
            info = _task_info(task)
            info["backtrace"] = _get_stack_frames(prog, task)
            result["panic_task"] = info
            panic_found = True
    except Exception:
        pass

    if not panic_found:
        try:
            from drgn.helpers.linux.sched import cpu_curr
            cpu = int(prog["crashing_cpu"])
            task = cpu_curr(prog, cpu)
            info = _task_info(task)
            info["cpu"] = cpu
            info["backtrace"] = _get_stack_frames(prog, task)
            result["panic_task"] = info
        except Exception as e:
            result["panic_task_error"] = str(e)

    # Per-CPU current task backtraces
    try:
        from drgn.helpers.linux.cpumask import for_each_online_cpu
        from drgn.helpers.linux.sched import cpu_curr

        cpu_traces = []
        for cpu in for_each_online_cpu(prog):
            try:
                task = cpu_curr(prog, cpu)
                info = _task_info(task)
                info["cpu"] = cpu
                info["backtrace"] = _get_stack_frames(prog, task)
                cpu_traces.append(info)
            except Exception as e:
                cpu_traces.append({"cpu": cpu, "error": str(e)})
        result["cpu_backtraces"] = cpu_traces
    except Exception as e:
        result["cpu_backtraces_error"] = str(e)

    return result


# ── Memory ───────────────────────────────────────────────────


def analyze_memory(prog):
    """Memory usage and slab cache stats."""
    result = {}

    # Page size
    try:
        page_size = int(prog.constant("PAGE_SIZE"))
    except Exception:
        page_size = 4096

    # Total memory
    try:
        from drgn.helpers.linux.mm import totalram_pages
        total_pages = int(totalram_pages(prog))
        result["total_ram_bytes"] = total_pages * page_size
        result["total_ram_mb"] = total_pages * page_size // (1024 * 1024)
    except Exception as e:
        result["total_ram_error"] = str(e)

    # Free pages
    try:
        from drgn.helpers.linux.mm import nr_free_pages
        free = int(nr_free_pages(prog))
        result["free_pages"] = free
        result["free_mb"] = free * page_size // (1024 * 1024)
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
                obj_size = int(cache.size)
                active = int(usage.active_objs)
                total = int(usage.num_objs)
                slabs = int(usage.num_slabs)
                est_bytes = active * obj_size
                total_slab_bytes += est_bytes
                caches.append({
                    "name": name,
                    "active_objects": active,
                    "total_objects": total,
                    "object_size": obj_size,
                    "slabs": slabs,
                    "estimated_bytes": est_bytes,
                })
            except Exception:
                continue
        # Sort by estimated size, show top 20
        caches.sort(
            key=lambda x: x["estimated_bytes"], reverse=True,
        )
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
        from drgn.helpers.linux.block import for_each_disk, disk_name
        disks = []
        for disk in for_each_disk(prog):
            try:
                name = disk_name(disk)
                if isinstance(name, bytes):
                    name = name.decode(errors="replace")
                disks.append(name)
            except Exception:
                disks.append("?")
        result["block_devices"] = disks
    except Exception as e:
        result["block_devices_error"] = str(e)

    # D-state (uninterruptible sleep) tasks
    try:
        hung_tasks = []
        for task in for_each_task(prog):
            try:
                state = _get_task_state_str(task)
                if "D" in state or "UN" in state:
                    info = _task_info(task)
                    info["state"] = state
                    info["backtrace"] = _get_stack_frames(
                        prog, task,
                    )
                    hung_tasks.append(info)
            except Exception:
                continue
        result["d_state_tasks"] = hung_tasks
        result["d_state_count"] = len(hung_tasks)
    except Exception as e:
        result["d_state_error"] = str(e)

    return result


# ── Kernel log ───────────────────────────────────────────────


def analyze_dmesg(prog, tail=100):
    """Extract kernel log (printk) records with timestamps."""
    try:
        from drgn.helpers.linux.printk import get_printk_records
        records = list(get_printk_records(prog))
        lines = []
        for r in records:
            try:
                ts_ns = int(r.timestamp)
                ts_s = ts_ns / 1e9
                text = r.text
                if isinstance(text, bytes):
                    text = text.decode(errors="replace")
                lines.append(f"[{ts_s:12.6f}] {text}")
            except Exception:
                try:
                    lines.append(str(r))
                except Exception:
                    continue
        total = len(lines)
        if tail and len(lines) > tail:
            lines = lines[-tail:]
        return {"dmesg": lines, "total_records": total}
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
            result[name] = ANALYSES[name](prog)
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
