#!/usr/bin/env python3
"""Port of epython dk.py to drgn.

Dumps and sorts the Lustre dk (debug kernel) logs from a vmcore.
Reads cfs_trace_data arrays, walks trace pages, parses ptldebug_header
records, and outputs sorted dk-format log lines.

Original: contrib/debug_tools/epython_scripts/dk.py
Authors: Ann Koehler (Cray Inc.), ported to drgn by Claude.
"""

import argparse
import json
import sys
from operator import itemgetter

import drgn
from drgn.helpers.linux.list import list_for_each_entry


def page_to_virt(prog, page_obj):
    """Convert a struct page pointer to its virtual address.

    Uses vmemmap-based calculation: the page's position in the
    vmemmap array gives its PFN, which gives its physical address,
    which we convert to virtual.
    """
    try:
        # Get vmemmap base and page struct size
        vmemmap_base = prog["vmemmap_base"].value_()
    except (KeyError, LookupError):
        # aarch64 or other arch — try the vmemmap symbol directly
        try:
            vmemmap_base = prog["vmemmap"].value_()
        except (KeyError, LookupError):
            # Fallback: use page_to_phys helper if available
            vmemmap_base = None

    page_addr = page_obj.value_() if hasattr(page_obj, 'value_') else int(page_obj)
    page_size = prog.type("struct page").size

    if vmemmap_base is not None:
        pfn = (page_addr - vmemmap_base) // page_size
    else:
        # Try PHYS_OFFSET-based calculation for aarch64
        try:
            memstart_addr = prog["memstart_addr"].value_()
        except (KeyError, LookupError):
            memstart_addr = 0
        # On aarch64, vmemmap is at a fixed offset
        # Fallback: use the direct mapping
        try:
            vmemmap = prog.symbol("vmemmap")
            vmemmap_base = vmemmap.address
            pfn = (page_addr - vmemmap_base) // page_size
        except LookupError:
            raise RuntimeError(
                f"Cannot determine PFN from page address 0x{page_addr:x}"
            )

    physaddr = pfn * prog["PAGE_SIZE"].value_() if "PAGE_SIZE" in dir(prog) else pfn * 4096

    # phys_to_virt: on x86_64, virt = phys + PAGE_OFFSET
    # on aarch64, similar linear mapping
    try:
        page_offset = prog["PAGE_OFFSET"].value_()
    except (KeyError, LookupError):
        page_offset = 0xFFFF000000000000  # aarch64 typical

    return physaddr + page_offset, pfn


def page_to_pfn_simple(prog, page_obj):
    """Simpler PFN calculation using drgn's built-in page helpers."""
    try:
        from drgn.helpers.linux.mm import page_to_pfn as _page_to_pfn
        return _page_to_pfn(page_obj)
    except ImportError:
        pass

    # Manual fallback
    page_addr = page_obj.value_() if hasattr(page_obj, 'value_') else int(page_obj)
    page_size = prog.type("struct page").size

    try:
        vmemmap_base = prog["vmemmap_base"].value_()
    except (KeyError, LookupError):
        try:
            vmemmap_sym = prog.symbol("vmemmap")
            vmemmap_base = vmemmap_sym.address
        except LookupError:
            raise RuntimeError("Cannot find vmemmap base")

    return (page_addr - vmemmap_base) // page_size


def dump_dk_line(prog, lines, pfn, used):
    """Parse dk log entries from a trace page."""
    try:
        from drgn.helpers.linux.mm import pfn_to_virt
        vaddr = pfn_to_virt(prog, pfn).value_()
    except (ImportError, Exception):
        # Manual: physaddr = pfn << PAGE_SHIFT, vaddr via direct map
        physaddr = pfn * 4096  # PAGE_SIZE
        try:
            page_offset = prog["PAGE_OFFSET"].value_()
        except (KeyError, LookupError):
            page_offset = 0xFFFF800000000000  # x86_64 typical
        vaddr = physaddr + page_offset

    hdr_size = prog.type("struct ptldebug_header").size
    remaining = used

    while remaining > 0 and remaining >= hdr_size:
        try:
            hdr = drgn.Object(prog, "struct ptldebug_header", address=vaddr)

            ph_len = hdr.ph_len.value_()
            if ph_len <= hdr_size or ph_len > 4096:
                break

            # Read the text data after the header
            data_addr = vaddr + hdr_size
            data_len = ph_len - hdr_size

            try:
                data = prog.read(data_addr, data_len)
            except drgn.FaultError:
                break

            # Parse: filename\0function\0text
            parts = data.split(b"\x00", 2)
            if len(parts) >= 3:
                filename = parts[0].decode(errors="replace")
                function = parts[1].decode(errors="replace")
                text = parts[2].decode(errors="replace").rstrip()
            else:
                filename = "?"
                function = "?"
                text = data.decode(errors="replace").rstrip()

            ph_flags = hdr.ph_flags.value_()
            prefix = (
                f"{hdr.ph_subsys.value_():08x}:{hdr.ph_mask.value_():08x}:"
                f"{hdr.ph_cpu_id.value_()}.{hdr.ph_type.value_()}"
                f"{'F' if (ph_flags & 1) else ''}:"
                f"{hdr.ph_sec.value_()}.{hdr.ph_usec.value_()}"
            )

            line = (
                f"{prefix}:{hdr.ph_stack.value_():06d}:{hdr.ph_pid.value_()}:"
                f"{hdr.ph_extern_pid.value_()}:({filename}:"
                f"{hdr.ph_line_num.value_()}:{function}()) {text}"
            )

            # Use (sec, usec) as sort key
            sort_key = (hdr.ph_sec.value_(), hdr.ph_usec.value_())
            lines.append((sort_key, line))

            remaining -= ph_len
            vaddr += ph_len

        except (drgn.FaultError, drgn.ObjectAbsentError):
            break


def walk_trace_pages(prog, lines, list_head, trace_page_struct):
    """Walk a list of cfs_trace_page/trace_page and extract dk lines."""
    try:
        for tp in list_for_each_entry(trace_page_struct, list_head, "linkage"):
            try:
                page_ptr = tp.page
                used = tp.used.value_()
                if used <= 0 or used > 4096:
                    continue
                pfn = page_to_pfn_simple(prog, page_ptr)
                dump_dk_line(prog, lines, pfn, used)
            except (drgn.FaultError, drgn.ObjectAbsentError):
                continue
    except (drgn.FaultError, drgn.ObjectAbsentError):
        pass


def walk_trace_data(prog):
    """Walk the cfs_trace_data array and extract all dk log lines."""
    lines = []

    # Try both symbol names (lustre 2.x uses cfs_ prefix)
    try:
        cfs_trace_data = prog["cfs_trace_data"]
        trace_page_struct = "struct cfs_trace_page"
    except (KeyError, LookupError):
        try:
            cfs_trace_data = prog["trace_data"]
            trace_page_struct = "struct trace_page"
        except (KeyError, LookupError):
            return None, "cfs_trace_data symbol not found (Lustre modules not loaded?)"

    # Get number of CPUs
    try:
        nr_cpus = prog["nr_cpu_ids"].value_()
    except (KeyError, LookupError):
        nr_cpus = 256  # safe upper bound

    # cfs_trace_data is union cfs_trace_data_union (*[TCD_TYPE_MAX])[NR_CPUS]
    # i.e., array of pointers to per-CPU arrays of unions.
    # Access pattern: cfs_trace_data[type][0][cpu].tcd
    for tcd_type in range(3):  # TCD_TYPE_MAX
        try:
            tcd_ptr = cfs_trace_data[tcd_type]
            if tcd_ptr.value_() == 0:
                continue

            # Deref the pointer to get the array, then index by CPU
            tcd_array = tcd_ptr[0]

            for cpu in range(nr_cpus):
                try:
                    u = tcd_array[cpu]
                    tcd = u.tcd
                    # tcd_pages list
                    walk_trace_pages(
                        prog, lines,
                        tcd.tcd_pages.address_of_(),
                        trace_page_struct,
                    )
                    # tcd_stock_pages list
                    walk_trace_pages(
                        prog, lines,
                        tcd.tcd_stock_pages.address_of_(),
                        trace_page_struct,
                    )
                except (drgn.FaultError, drgn.ObjectAbsentError):
                    continue
        except (drgn.FaultError, drgn.ObjectAbsentError, IndexError):
            continue

    # Sort by timestamp
    lines.sort(key=itemgetter(0))
    return lines, None


def dump_dk_text(prog, output_file=None):
    """Dump dk logs in text format."""
    lines, error = walk_trace_data(prog)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if not lines:
        print("No debug log entries found.")
        return 0

    if output_file:
        with open(output_file, "w") as f:
            for _, line in lines:
                f.write(line + "\n")
        print(f"Wrote {len(lines)} dk log lines to {output_file}")
    else:
        for _, line in lines:
            print(line)
        print(f"\n--- {len(lines)} dk log lines ---", file=sys.stderr)

    return 0


def dump_dk_json(prog):
    """Return dk log entries as structured JSON."""
    lines, error = walk_trace_data(prog)
    if error:
        return {"analysis": "dk", "error": error}

    return {
        "analysis": "dk",
        "count": len(lines),
        "lines": [line for _, line in lines] if lines else [],
    }


def main():
    from .lustre_analyze import load_program

    parser = argparse.ArgumentParser(
        description="Dump and sort the Lustre dk logs from a vmcore.",
        epilog="NOTE: the Lustre kernel modules must be loaded.",
    )
    parser.add_argument("--vmcore", required=True)
    parser.add_argument("--vmlinux", required=True)
    parser.add_argument("--mod-dir", default=None)
    parser.add_argument("--debug-dir", default=None)
    parser.add_argument("-o", "--output", default=None,
                        help="Write dk log to file instead of stdout")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    prog = load_program(args.vmcore, args.vmlinux, args.mod_dir, args.debug_dir)

    if args.json:
        result = dump_dk_json(prog)
        indent = 2 if args.pretty else None
        print(json.dumps(result, indent=indent, default=str))
    else:
        sys.exit(dump_dk_text(prog, args.output))


if __name__ == "__main__":
    main()
