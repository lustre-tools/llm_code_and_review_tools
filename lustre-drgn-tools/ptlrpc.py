#!/usr/bin/env python3
"""Port of epython ptlrpc.py to drgn.

Dumps the Lustre RPC queues for all ptlrpcd_XX threads, showing
sent and pending requests with phase, flags, NID, opcode, and xid.

Original: contrib/debug_tools/epython_scripts/ptlrpc.py
Authors: Ann Koehler (Cray Inc.), ported to drgn by Claude.
"""

import argparse
import json
import sys

import drgn
from drgn.helpers.linux.list import list_for_each_entry

try:
    from . import lustre_helpers as lh
except ImportError:
    import lustre_helpers as lh


# ── RPC phases ───────────────────────────────────────────────

RQ_PHASE_NEW = 0xEBC0DE00
RQ_PHASE_RPC = 0xEBC0DE01
RQ_PHASE_BULK = 0xEBC0DE02
RQ_PHASE_INTERPRET = 0xEBC0DE03
RQ_PHASE_COMPLETE = 0xEBC0DE04
RQ_PHASE_UNREG_RPC = 0xEBC0DE05
RQ_PHASE_UNREG_BULK = 0xEBC0DE06
RQ_PHASE_UNDEFINED = 0xEBC0DE07

PHASES = {
    RQ_PHASE_NEW: "NEW",
    RQ_PHASE_RPC: "RPC",
    RQ_PHASE_BULK: "BULK",
    RQ_PHASE_INTERPRET: "NtrPrt",
    RQ_PHASE_COMPLETE: "COMP",
    RQ_PHASE_UNREG_RPC: "UNREG",
    RQ_PHASE_UNREG_BULK: "UNBULK",
    RQ_PHASE_UNDEFINED: "UNDEF",
}

LP_POISON = 0x5A5A5A5A5A5A5A5A
LUSTRE_MSG_MAGIC_V2 = 0x0BD00BD3


def phase2str(phase):
    return PHASES.get(phase & 0xFFFFFFFF, f"?{phase}")


def get_phase_flags(req):
    """Return phase:flags string for a request."""
    phase = req.rq_phase.value_()
    phasestr = phase2str(phase)

    flags = ""
    flag_fields = [
        ("rq_intr", "I"),
        ("rq_replied", "R"),
        ("rq_err", "E"),
        ("rq_net_err", "e"),
        ("rq_timedout", "X"),
        ("rq_resend", "S"),
        ("rq_restart", "T"),
        ("rq_replay", "P"),
        ("rq_no_resend", "N"),
        ("rq_waiting", "W"),
        ("rq_wait_ctx", "C"),
        ("rq_hp", "H"),
        ("rq_committed", "M"),
        ("rq_req_unlinked", "q"),
        ("rq_reply_unlinked", "u"),
    ]

    for field, char in flag_fields:
        try:
            if getattr(req, field).value_():
                flags += char
        except (AttributeError, drgn.FaultError):
            pass

    return f"{phasestr}:{flags}"


def _size_round(val):
    return (val + 7) & ~0x7


def get_ptlrpc_body(prog, req):
    """Get the ptlrpc_body_v2 from a request message, if valid."""
    try:
        msg_ptr = req.rq_reqmsg.value_()
        if msg_ptr == 0:
            return None

        msg = req.rq_reqmsg[0]
        if msg.lm_magic.value_() != LUSTRE_MSG_MAGIC_V2:
            return None

        bufcount = msg.lm_bufcount.value_()
        if bufcount < 1:
            return None

        buflen = msg.lm_buflens[0].value_()
        pb_size = prog.type("struct ptlrpc_body_v2").size
        if buflen < pb_size:
            return None

        # Calculate offset to first buffer
        lm_buflens_off = prog.type("struct lustre_msg_v2").member("lm_buflens").offset
        uint_size = prog.type("unsigned int").size
        offset = lm_buflens_off + uint_size * bufcount
        offset = _size_round(offset)

        addr = msg_ptr + offset
        if addr == 0:
            return None

        return drgn.Object(prog, "struct ptlrpc_body_v2", address=addr)
    except (drgn.FaultError, AttributeError):
        return None


def _obj_addr(obj):
    """Get address of a drgn Object, whether it's a pointer or by-value."""
    try:
        return obj.address_of_().value_()
    except ValueError:
        return obj.value_()


def get_request_info(prog, req):
    """Extract key info from a ptlrpc_request."""
    info = {
        "address": f"0x{_obj_addr(req):x}",
        "xid": req.rq_xid.value_(),
        "phase_flags": get_phase_flags(req),
        "bulk_rw": f"{req.rq_bulk_read.value_()}:{req.rq_bulk_write.value_()}",
        "sent_deadline": f"{req.rq_sent.value_()}/{req.rq_deadline.value_()}",
        "opc": -1,
        "status": -1,
        "nid": "LNET_NID_ANY",
        "obd_name": "Invalid Import",
        "pb_address": "0x0",
    }

    # Get ptlrpc_body info
    pb = get_ptlrpc_body(prog, req)
    if pb:
        try:
            info["status"] = pb.pb_status.value_()
            info["opc"] = pb.pb_opc.value_()
            info["opc_name"] = lh.opc2str(info["opc"])
            info["pb_address"] = f"0x{pb.address_of_().value_():x}"
        except (drgn.FaultError, AttributeError):
            pass

    # Get NID from import
    try:
        imp_ptr = req.rq_import.value_()
        if imp_ptr != 0 and imp_ptr != 0xFFFFFFFFFFFFFFFF and imp_ptr != LP_POISON:
            imp = req.rq_import[0]
            try:
                info["obd_name"] = lh.obd_name(imp.imp_obd[0])
            except (drgn.FaultError, AttributeError):
                pass

            if not imp.imp_invalid.value_() and imp.imp_connection.value_() != 0:
                nid = imp.imp_connection[0].c_peer.nid.value_()
                info["nid"] = lh.nid2str(nid)
    except (drgn.FaultError, AttributeError):
        pass

    return info


def print_request_header(title=None):
    """Print the column header for RPC request lists."""
    if title:
        print(f"\n{title}")
    print(
        f"{'thread':<14s} {'pid':<6s} {'ptlrpc_request':<19s} "
        f"{'xid':<18s} {'nid':<19s} {'opc':<4s} "
        f"{'phase:flags':<14s} {'R:W':<4s} {'sent/deadline':<22s} "
        f"{'ptlrpc_body':<19s}"
    )
    print("=" * 148)


def print_one_request(sthread, info):
    """Print one RPC request in text format."""
    print(
        f"{sthread:<14s} {info['status']:<6} 0x{info['address'][2:]:<17s} "
        f"{info['xid']:<18d} {info['obd_name']:<19s} {info['opc']:<4d} "
        f"{info['phase_flags']:<14s} {info['bulk_rw']:<4s} "
        f"{info['sent_deadline']:<22s} {info['pb_address']:<19s}"
    )


def walk_request_list(prog, sthread, list_head_addr, link_member):
    """Walk a list of ptlrpc_request and print each one."""
    requests = []
    try:
        for req in list_for_each_entry(
            "struct ptlrpc_request", list_head_addr, link_member
        ):
            info = get_request_info(prog, req)
            requests.append((sthread, info))
            print_one_request(sthread, info)
    except (drgn.FaultError, drgn.ObjectAbsentError):
        pass
    return requests


def foreach_ptlrpcd_ctl(prog, callback, *args):
    """Iterate over all ptlrpcd_ctl structures."""
    try:
        pinfo_rpcds = prog["ptlrpcds"]
        pinfo_count = prog["ptlrpcds_num"].value_()
    except (KeyError, LookupError):
        return

    for idx in range(pinfo_count):
        ptlrpcd = pinfo_rpcds[idx]
        nthreads = ptlrpcd.pd_nthreads.value_()
        for jdx in range(nthreads):
            pd = ptlrpcd.pd_threads[jdx]
            callback(prog, pd, *args)

    try:
        pd = prog["ptlrpcd_rcv"]
        callback(prog, pd, *args)
    except (KeyError, LookupError):
        pass


def dump_daemon_rpclists_text(prog):
    """Dump sent and pending RPC lists for all ptlrpcd daemons."""
    sent_rpcs = []
    pend_rpcs = []

    def collect_listhdrs(prog, pd, sent, pend):
        name = pd.pc_name.string_().decode(errors="replace")
        sent.append((name, pd.pc_set.set_requests.address_of_()))
        pend.append((name, pd.pc_set.set_new_requests.address_of_()))

    foreach_ptlrpcd_ctl(prog, collect_listhdrs, sent_rpcs, pend_rpcs)

    # The set_chain link is inside ptlrpc_cli_req, which is inside
    # the rq_cli union member of ptlrpc_request
    link_member = "rq_cli.cr_set_chain"

    print_request_header("Sent RPCS: ptlrpc_request_set.set_requests->")
    for sthread, lhdr in sent_rpcs:
        walk_request_list(prog, sthread, lhdr, link_member)

    print_request_header("Pending RPCS: ptlrpc_request_set.set_new_requests->")
    for sthread, lhdr in pend_rpcs:
        walk_request_list(prog, sthread, lhdr, link_member)
    print_request_header("")


def dump_overview_text(prog):
    """Print overview of ptlrpcd threads."""
    def print_entry(prog, pd):
        name = pd.pc_name.string_().decode(errors="replace")
        pd_addr = _obj_addr(pd)
        pc_set_addr = _obj_addr(pd.pc_set)
        print(f"{name + ':':<14s}  ptlrpcd_ctl 0x{pd_addr:x}   "
              f"ptlrpc_request_set 0x{pc_set_addr:x}")

    foreach_ptlrpcd_ctl(prog, print_entry)


def dump_pcsets_text(prog):
    """Print RPC counts per ptlrpc_request_set."""
    print(f"{'thread':<14s} {'ptlrpc_request_set':<19s} {'ref':<4s} "
          f"{'new':<4s} {'remain':<6s}")
    print("=" * 52)

    def print_stats(prog, pd):
        new_count = pd.pc_set.set_new_count.counter.value_()
        remaining = pd.pc_set.set_remaining.counter.value_()
        if new_count != 0 or remaining != 0:
            name = pd.pc_name.string_().decode(errors="replace")
            set_addr = _obj_addr(pd.pc_set)
            refcount = pd.pc_set.set_refcount.counter.value_()
            print(f"{name + ':':<13s} 0x{set_addr:<18x} {refcount:<4d} "
                  f"{new_count:<4d} {remaining:<6d}")

    foreach_ptlrpcd_ctl(prog, print_stats)


def get_rpcs_json(prog):
    """Return all RPC queue info as structured data."""
    sent_rpcs = []
    pend_rpcs = []

    def collect_listhdrs(prog, pd, sent, pend):
        name = pd.pc_name.string_().decode(errors="replace")
        sent.append((name, pd.pc_set.set_requests.address_of_()))
        pend.append((name, pd.pc_set.set_new_requests.address_of_()))

    foreach_ptlrpcd_ctl(prog, collect_listhdrs, sent_rpcs, pend_rpcs)

    link_member = "rq_cli.cr_set_chain"

    result = {"analysis": "ptlrpc", "sent": [], "pending": []}

    for sthread, lhdr in sent_rpcs:
        try:
            for req in list_for_each_entry(
                "struct ptlrpc_request", lhdr, link_member
            ):
                info = get_request_info(prog, req)
                info["thread"] = sthread
                result["sent"].append(info)
        except (drgn.FaultError, drgn.ObjectAbsentError):
            pass

    for sthread, lhdr in pend_rpcs:
        try:
            for req in list_for_each_entry(
                "struct ptlrpc_request", lhdr, link_member
            ):
                info = get_request_info(prog, req)
                info["thread"] = sthread
                result["pending"].append(info)
        except (drgn.FaultError, drgn.ObjectAbsentError):
            pass

    return result


def main():
    try:
        from .lustre_analyze import load_program
    except ImportError:
        from lustre_analyze import load_program

    parser = argparse.ArgumentParser(
        description="Displays the RPC queues of the Lustre ptlrpcd daemons",
    )
    parser.add_argument("--vmcore", required=True)
    parser.add_argument("--vmlinux", required=True)
    parser.add_argument("--mod-dir", default=None)
    parser.add_argument("--debug-dir", default=None)
    parser.add_argument("-o", dest="oflag", action="store_true",
                        help="Print overview of ptlrpcd threads")
    parser.add_argument("-s", dest="sflag", action="store_true",
                        help="Print RPC counts per ptlrpc_request_set")
    parser.add_argument("--text", action="store_true", help="Text output (default is JSON)")
    parser.add_argument("--pretty", action="store_true",
                        help="Pretty-print JSON (also set LUSTRE_DRGN_PRETTY=1)")
    args = parser.parse_args()

    prog = load_program(args.vmcore, args.vmlinux, args.mod_dir, args.debug_dir)

    if args.text:
        if args.oflag:
            dump_overview_text(prog)
        elif args.sflag:
            dump_pcsets_text(prog)
        else:
            dump_daemon_rpclists_text(prog)
    else:
        result = get_rpcs_json(prog)
        lh.json_output(result, args)


if __name__ == "__main__":
    main()
