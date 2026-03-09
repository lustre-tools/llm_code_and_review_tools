#!/usr/bin/env python3
"""Port of epython uniqueStacktrace.py to drgn.

Groups all tasks by unique stack trace and prints them sorted
by frequency (least common first). This is invaluable for
identifying which tasks are stuck and what they're waiting on.

Original: contrib/debug_tools/epython_scripts/uniqueStacktrace.py
Authors: Ann Koehler (Cray Inc.), ported to drgn by Claude.
"""

import argparse
import json
import sys
from collections import defaultdict

import drgn
from drgn.helpers.linux.pid import for_each_task


def get_all_stack_traces(prog, include_swapper=False, task_filter=None):
    """Collect stack traces for all tasks, grouped by unique trace.

    Returns a dict mapping stack_trace_key -> list of (pid, comm, task_addr).
    """
    traces = defaultdict(list)

    for task in for_each_task(prog):
        try:
            comm = task.comm.string_().decode(errors="replace")
            pid = task.pid.value_()
            try:
                task_addr = task.address_of_().value_()
            except ValueError:
                task_addr = task.value_()

            # Skip swapper threads unless requested
            if not include_swapper and comm.startswith("swapper/"):
                continue

            # Apply task filter if given (e.g., "UN" for uninterruptible)
            if task_filter:
                try:
                    from drgn.helpers.linux.sched import task_state_to_char
                    state_char = task_state_to_char(task)
                except (ImportError, Exception):
                    state_char = "?"

                if task_filter == "UN" and state_char != "D":
                    continue
                elif task_filter == "RU" and state_char != "R":
                    continue

            # Get stack trace
            try:
                trace = prog.stack_trace(task)
                frames = []
                for frame in trace:
                    # Extract function name and source
                    desc = str(frame)
                    # Simplify: just get the function-relevant parts
                    if " in " in desc:
                        parts = desc.split(" in ", 1)
                        func_part = parts[1].strip()
                        # Remove module info for grouping
                        frames.append(func_part)
                    else:
                        frames.append(desc.strip())

                trace_key = "\n\t".join(frames)
                traces[trace_key].append((pid, comm, task_addr))
            except (drgn.FaultError, drgn.ObjectAbsentError, ValueError):
                traces["<unavailable>"].append((pid, comm, task_addr))

        except (drgn.FaultError, drgn.ObjectAbsentError):
            continue

    return traces


def print_traces_text(traces, print_pid=False, print_ptr=False):
    """Print unique stack traces sorted by frequency (ascending)."""
    sorted_traces = sorted(traces.items(), key=lambda x: len(x[1]))

    for trace_key, task_list in sorted_traces:
        if print_pid and not print_ptr:
            pids = ", ".join(str(p[0]) for p in task_list)
            print(f"PID: {pids}")
        elif print_pid and print_ptr:
            items = ", ".join(f"{p[0]}: 0x{p[2]:x}" for p in task_list)
            print(f"PID, TSK: {items}")
        elif not print_pid and print_ptr:
            addrs = ", ".join(f"0x{p[2]:x}" for p in task_list)
            print(f"TSK: {addrs}")

        print(f"TASKS: {len(task_list)}")
        print(f"\t{trace_key}\n")


def get_traces_json(traces):
    """Return unique stack traces as structured JSON."""
    result = {
        "analysis": "unique_stacktraces",
        "unique_traces": len(traces),
        "total_tasks": sum(len(v) for v in traces.values()),
        "traces": [],
    }

    sorted_traces = sorted(traces.items(), key=lambda x: len(x[1]), reverse=True)

    for trace_key, task_list in sorted_traces:
        result["traces"].append({
            "count": len(task_list),
            "tasks": [
                {"pid": p[0], "comm": p[1], "address": f"0x{p[2]:x}"}
                for p in task_list
            ],
            "stack": trace_key.split("\n\t"),
        })

    return result


def main():
    from .lustre_analyze import load_program

    parser = argparse.ArgumentParser(
        description="Print unique stack traces grouped by frequency.",
    )
    parser.add_argument("--vmcore", required=True)
    parser.add_argument("--vmlinux", required=True)
    parser.add_argument("--mod-dir", default=None)
    parser.add_argument("--debug-dir", default=None)
    parser.add_argument("-p", "--print-pid", action="store_true",
                        help="Print PIDs for each stack trace")
    parser.add_argument("-q", "--print-taskpntr", action="store_true",
                        help="Print task pointers for each stack trace")
    parser.add_argument("-s", "--swapper", action="store_true",
                        help="Include swapper processes")
    parser.add_argument("-f", "--filter", default=None,
                        choices=["UN", "RU"],
                        help="Filter tasks by state (UN=uninterruptible, RU=running)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    prog = load_program(args.vmcore, args.vmlinux, args.mod_dir, args.debug_dir)

    traces = get_all_stack_traces(
        prog,
        include_swapper=args.swapper,
        task_filter=args.filter,
    )

    if args.json:
        result = get_traces_json(traces)
        indent = 2 if args.pretty else None
        print(json.dumps(result, indent=indent, default=str))
    else:
        print_traces_text(traces, args.print_pid, args.print_taskpntr)


if __name__ == "__main__":
    main()
