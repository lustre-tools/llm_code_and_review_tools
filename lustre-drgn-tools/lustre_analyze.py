#!/usr/bin/env python3
from __future__ import annotations
"""Lustre vmcore analysis using drgn.

Provides deep, structured analysis of Lustre crash dumps that
goes beyond what crash can do — including local variable access
from stack frames, direct struct traversal, and typed output.

Usage:
    python -m crash_tool.drgn_scripts.lustre_analyze \\
        --vmcore /path/to/vmcore \\
        --vmlinux /path/to/vmlinux \\
        --mod-dir /path/to/lustre/kos \\
        [--debug-dir /path/to/debuginfo] \\
        [analysis...]

Analyses:
    overview    System info and crash summary
    backtrace   Full backtrace with source locations
    locals      Local variables from each Lustre frame
    obd         OBD device list
    all         Run all analyses (default)
"""

import argparse
import glob
import json
import os
import sys
from typing import Any


def load_program(
    vmcore: str,
    vmlinux: str,
    mod_dir: str | None = None,
    debug_dir: str | None = None,
) -> "drgn.Program":
    """Load a vmcore into a drgn Program with Lustre debug info."""
    import drgn

    prog = drgn.Program()
    prog.set_core_dump(vmcore)

    debug_files = [vmlinux]
    if mod_dir:
        all_ko = glob.glob(
            os.path.join(mod_dir, "**", "*.ko"), recursive=True
        )
        # Filter out build artifacts and test modules
        debug_files += [
            ko for ko in all_ko
            if "kconftest" not in ko
            and "/conftest.ko" not in ko
            and "/kunit/" not in ko
        ]
    if debug_dir:
        debug_files += glob.glob(
            os.path.join(debug_dir, "**", "*.debug"), recursive=True
        )

    prog.load_debug_info(debug_files)
    return prog


def analyze_overview(prog: "drgn.Program") -> dict[str, Any]:
    """System info and crash summary."""
    uts = prog["init_uts_ns"].name
    nr_cpus = prog["nr_cpu_ids"].value_()

    thread = prog.crashed_thread()
    task = thread.object

    return {
        "analysis": "overview",
        "system": {
            "nodename": uts.nodename.string_().decode(),
            "release": uts.release.string_().decode(),
            "version": uts.version.string_().decode(),
            "cpus": nr_cpus,
            "platform": str(prog.platform),
        },
        "crashed_task": {
            "pid": thread.tid,
            "comm": task.comm.string_().decode(),
            "cpu": task.cpu.value_(),
        },
    }


def analyze_backtrace(prog: "drgn.Program") -> dict[str, Any]:
    """Full backtrace with source file and line info."""
    thread = prog.crashed_thread()
    trace = prog.stack_trace(thread.object)

    frames = []
    for i, frame in enumerate(trace):
        frame_info: dict[str, Any] = {
            "index": i,
            "description": str(frame),
        }
        # Extract source info if available
        name = str(frame)
        if " in " in name:
            parts = name.split(" in ", 1)
            if " at " in parts[1]:
                func_source = parts[1].split(" at ", 1)
                frame_info["function"] = func_source[0].strip()
                frame_info["source"] = func_source[1].strip()
            else:
                frame_info["function"] = parts[1].strip()

        # Tag Lustre frames
        for mod in ["libcfs", "obdclass", "osc", "ptlrpc", "lustre",
                     "lnet", "mdc", "lov", "lmv", "fid", "fld", "mgc",
                     "ko2iblnd", "ldiskfs"]:
            if f"[{mod}]" in name or f"/{mod}/" in name:
                frame_info["lustre_module"] = mod
                break

        frames.append(frame_info)

    return {
        "analysis": "backtrace",
        "pid": thread.tid,
        "comm": thread.object.comm.string_().decode(),
        "frames": frames,
    }


def _obj_to_dict(obj: "drgn.Object", depth: int = 0, max_depth: int = 2) -> Any:
    """Convert a drgn Object to a JSON-friendly dict, with depth limit."""
    import drgn

    if depth > max_depth:
        return str(obj)

    kind = obj.type_.kind

    if kind == drgn.TypeKind.INT or kind == drgn.TypeKind.BOOL:
        return obj.value_()
    elif kind == drgn.TypeKind.ENUM:
        try:
            return str(obj.value_())
        except Exception:
            return int(obj)
    elif kind == drgn.TypeKind.POINTER:
        addr = obj.value_()
        if addr == 0:
            return "NULL"
        return f"0x{addr:x}"
    elif kind == drgn.TypeKind.ARRAY:
        type_name = str(obj.type_)
        if "char" in type_name:
            try:
                return obj.string_().decode(errors="replace")
            except Exception:
                return str(obj)
        return str(obj)
    elif kind == drgn.TypeKind.STRUCT or kind == drgn.TypeKind.UNION:
        result = {}
        try:
            for member in obj.type_.members:
                if member.name is None:
                    continue
                try:
                    result[member.name] = _obj_to_dict(
                        obj.member_(member.name),
                        depth + 1,
                        max_depth,
                    )
                except (drgn.FaultError, drgn.ObjectAbsentError):
                    result[member.name] = "<fault>"
        except Exception:
            return str(obj)
        return result
    else:
        return str(obj)


def analyze_locals(prog: "drgn.Program") -> dict[str, Any]:
    """Extract local variables from Lustre stack frames."""
    import drgn

    thread = prog.crashed_thread()
    trace = prog.stack_trace(thread.object)

    frames_with_locals = []
    for i, frame in enumerate(trace):
        name = str(frame)
        # Only extract locals from Lustre module frames
        is_lustre = any(
            mod in name
            for mod in [
                "libcfs", "obdclass", "osc", "ptlrpc", "lustre",
                "lnet", "mdc", "lov", "lmv", "fid", "fld",
                "ko2iblnd", "ldiskfs",
            ]
        )
        if not is_lustre:
            continue

        # Probe for known variable names
        var_names = [
            # cl_page related
            "page", "cp", "env", "ref",
            # osc related
            "oap", "cmd", "rc", "ext", "osc",
            # ptlrpc related
            "req", "set", "imp",
            # ldlm related
            "lock", "res", "ns",
            # general
            "obd", "exp",
        ]

        found_vars: dict[str, Any] = {}
        for var_name in var_names:
            try:
                val = frame[var_name]
                found_vars[var_name] = _obj_to_dict(val, max_depth=1)
            except (KeyError, drgn.ObjectAbsentError):
                pass
            except drgn.FaultError:
                found_vars[var_name] = "<memory fault>"

        if found_vars:
            frames_with_locals.append({
                "frame": i,
                "description": name,
                "locals": found_vars,
            })

    return {
        "analysis": "locals",
        "frames": frames_with_locals,
    }


def _obd_is_set_up(obd: "drgn.Object") -> bool:
    """Check if OBD device is set up, handling both old and new layouts.

    Handles both the old bitfield layout (obd.obd_set_up) and the
    newer DECLARE_BITMAP layout (test_bit(OBDF_SET_UP, obd.obd_flags)).
    """
    try:
        return bool(obd.obd_set_up)
    except AttributeError:
        pass
    # New layout: OBDF_SET_UP bit in obd_flags bitmap
    try:
        import drgn
        prog = obd.prog_
        bit = int(prog.constant("OBDF_SET_UP"))
        flags_addr = obd.obd_flags.address_of_().value_()
        word = drgn.Object(
            prog, "unsigned long",
            address=flags_addr + (bit // 64) * 8,
        )
        return bool(word.value_() & (1 << (bit % 64)))
    except (LookupError, AttributeError, Exception):
        return False


def analyze_obd_devices(prog: "drgn.Program") -> dict[str, Any]:
    """List active OBD devices."""
    import drgn
    try:
        from . import lustre_helpers as lh
    except ImportError:
        import lustre_helpers as lh

    try:
        obd_list = lh.get_obd_devices(prog)
    except (KeyError, LookupError):
        return {
            "analysis": "obd_devices",
            "error": "obd_devs symbol not found (obdclass not loaded?)",
        }

    devices = []
    for i, obd in obd_list:
        try:
            name = obd.obd_name.string_().decode(errors="replace")
            uuid = obd.obd_uuid.uuid.string_().decode(errors="replace")
            dev_type = ""
            try:
                type_ptr = obd.obd_type.value_()
                if type_ptr != 0:
                    typ = obd.obd_type[0]
                    dev_type = typ.typ_kobj.name.string_().decode(errors="replace")
            except (drgn.FaultError, AttributeError):
                pass

            devices.append({
                "index": i,
                "name": name,
                "uuid": uuid,
                "type": dev_type,
                "active": _obd_is_set_up(obd),
            })
        except (drgn.FaultError, drgn.ObjectAbsentError):
            continue

    return {
        "analysis": "obd_devices",
        "count": len(devices),
        "devices": devices,
    }


def run_analyses(
    prog: "drgn.Program",
    analyses: list[str],
) -> list[dict[str, Any]]:
    """Run the specified analyses and return results."""
    dispatch = {
        "overview": analyze_overview,
        "backtrace": analyze_backtrace,
        "locals": analyze_locals,
        "obd": analyze_obd_devices,
    }

    if "all" in analyses or not analyses:
        analyses = list(dispatch.keys())

    results = []
    for name in analyses:
        if name not in dispatch:
            results.append({
                "analysis": name,
                "error": f"Unknown analysis. Available: {', '.join(dispatch.keys())}",
            })
            continue
        try:
            results.append(dispatch[name](prog))
        except Exception as e:
            results.append({
                "analysis": name,
                "error": str(e),
            })

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Lustre vmcore analysis using drgn",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--vmcore", required=True, help="Path to vmcore file")
    parser.add_argument("--vmlinux", required=True, help="Path to vmlinux")
    parser.add_argument(
        "--mod-dir", default=None,
        help="Directory with Lustre .ko files",
    )
    parser.add_argument(
        "--debug-dir", default=None,
        help="Directory with Lustre .debug files",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print JSON output",
    )
    parser.add_argument(
        "analyses", nargs="*", default=["all"],
        help="Analyses to run: overview, backtrace, locals, obd, all",
    )

    args = parser.parse_args()

    try:
        import drgn  # noqa: F401
    except ImportError:
        print(json.dumps({
            "error": "drgn not installed. Install with: pip install drgn",
        }))
        sys.exit(1)

    prog = load_program(
        args.vmcore, args.vmlinux, args.mod_dir, args.debug_dir,
    )

    results = run_analyses(prog, args.analyses)

    indent = 2 if args.pretty else None
    print(json.dumps(results, indent=indent, default=str))


if __name__ == "__main__":
    main()
