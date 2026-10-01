#!/usr/bin/env python3
"""Show who sent the MDT request that an MDT service thread was handling.

Typical use: an MDS crashed with
    osd_olc_save()) ASSERTION( ln->ln_namelen <= 255 + 1 ) failed
(DDN-7132, LU-18783, LU-19409). The script finds the crashed mdt thread
in the vmcore and prints the client NID, the UID/GID, the job ID, the
operation, the names, and the parent directory FIDs.

The script reads these structures:
    task -> struct kthread .data -> struct ptlrpc_thread .t_env
    lu_env.le_ctx  [mdt_thread_key] -> struct mdt_thread_info
    lu_env.le_ses  [lu_ucred_key]   -> struct lu_ucred
    mdt_thread_info.mti_pill->rc_req -> struct ptlrpc_request

Usage (needs the drgn Python module, built with libkdumpfile to read
compressed kdump files):
    python3 mdt_request_owner.py --vmcore vmcore --vmlinux vmlinux \\
        --debug-dir /usr/lib/debug/lib/modules/<kver>/extra
    ... --pid 19984          # a specific mdt thread
    ... --scan               # all mdt threads with a name > 255 bytes

On a live MDS (as root, with lustre debuginfo installed):
    python3 mdt_request_owner.py --live --scan
"""

import argparse
import datetime
import glob
import logging
import os
import sys

import drgn
from drgn import cast
from drgn.helpers.linux.pid import find_task, for_each_task

NAME_MAX = 255

LND = {2: "tcp", 4: "ptl", 5: "o2ib", 13: "gni", 14: "kfi"}

OPC = {33: "MDS_GETATTR", 34: "MDS_GETATTR_NAME", 35: "MDS_CLOSE",
       36: "MDS_REINT", 37: "MDS_READPAGE", 41: "MDS_SYNC",
       49: "MDS_GETXATTR", 101: "LDLM_ENQUEUE", 59: "MDS_SWAP_LAYOUTS",
       62: "MDS_RMFID", 63: "MDS_BATCH"}

REINT = {1: "SETATTR", 2: "CREATE", 3: "LINK", 4: "UNLINK", 5: "RENAME",
         6: "OPEN", 7: "SETXATTR", 8: "RMENTRY", 9: "MIGRATE",
         10: "RESYNC"}


def load(args):
    # drgn logs one line per .ko that is not loaded in the dump.
    logging.getLogger("drgn").setLevel(logging.ERROR)
    prog = drgn.Program()
    if args.live:
        prog.set_kernel()
    else:
        prog.set_core_dump(args.vmcore)
    files = [args.vmlinux] if args.vmlinux else []
    for d in args.debug_dir or []:
        for pat in ("*.ko", "*.ko.debug", "*.debug"):
            files += glob.glob(os.path.join(d, "**", pat), recursive=True)
    try:
        # Also search the default places (/usr/lib/debug, /lib/modules).
        prog.load_debug_info(files, default=True)
    except drgn.MissingDebugInfoError as e:
        # Missing debug info for unrelated modules is not a problem.
        print(f"warning: {str(e).splitlines()[0]}", file=sys.stderr)
    for sym in ("mdt_thread_key", "lu_ucred_key"):
        try:
            prog[sym]
        except LookupError:
            sys.exit(f"error: no debug info for '{sym}'. Give --debug-dir "
                     "with the lustre .ko or .ko.debug files that match "
                     "the crashed server build.")
    return prog


def nid2str(nid):
    """Format a 64-bit lnet_nid_t or a struct lnet_nid."""
    if nid.type_.kind == drgn.TypeKind.STRUCT:
        typ, num = nid.nid_type.value_(), nid.nid_num.value_()
        num = ((num & 0xff) << 8) | (num >> 8)          # __be16
        a = nid.nid_addr[0].value_()
        a = int.from_bytes(a.to_bytes(4, "little"), "big")  # __be32
    else:
        v = nid.value_()
        if v == 0xFFFFFFFFFFFFFFFF:
            return "LNET_NID_ANY"
        typ, num, a = (v >> 48) & 0xffff, (v >> 32) & 0xffff, v & 0xffffffff
    ip = ".".join(str((a >> s) & 0xff) for s in (24, 16, 8, 0))
    return f"{ip}@{LND.get(typ, f'lnd{typ}')}{num if num else ''}"


def fid2str(fid):
    if not fid.value_():
        return "-"
    f = fid[0]
    return "[0x%x:0x%x:0x%x]" % (f.f_seq.value_(), f.f_oid.value_(),
                                 f.f_ver.value_())


def read_name(prog, ln):
    n = ln.ln_namelen.value_()
    if not ln.ln_name.value_() or n <= 0:
        return 0, ""
    raw = prog.read(ln.ln_name.value_(), min(n, 4096))
    return n, raw.decode("utf-8", "backslashreplace")


def key_value(prog, ctx, key):
    idx = prog[key].lct_index.value_()
    if idx < 0 or not ctx.lc_value.value_():
        return None
    v = ctx.lc_value[idx]
    return v if v.value_() else None


def task_ptlrpc_thread(prog, task):
    """Return the ptlrpc_thread that ptlrpc_main() got as its argument."""
    PF_KTHREAD = 0x00200000
    if not task.flags.value_() & PF_KTHREAD or \
            not task.comm.string_().startswith(b"mdt"):
        raise ValueError(f"'{task.comm.string_().decode()}' is not an mdt "
                         "service thread; try --scan")
    try:
        kt = cast("struct kthread *", task.worker_private)   # >= 5.17
    except AttributeError:
        kt = cast("struct kthread *", task.set_child_tid)    # < 5.17
    thr = cast("struct ptlrpc_thread *", kt.data)
    if thr.t_pid.value_() != task.pid.value_():
        raise ValueError("task is not a ptlrpc service thread")
    return thr


def req_body(prog, req):
    """Return the request ptlrpc_body (buffer 0), or None."""
    try:
        msg = req.rq_pill.rc_reqmsg     # rq_reqmsg is a macro for this
    except AttributeError:
        msg = req.rq_reqmsg
    if not msg.value_():
        return None
    cnt = msg.lm_bufcount.value_()
    hdr = drgn.offsetof(prog.type("struct lustre_msg_v2"), "lm_buflens")
    hdr = (hdr + 4 * cnt + 7) & ~7
    body = drgn.Object(prog, "struct ptlrpc_body_v3",
                       address=msg.value_() + hdr)
    if msg.lm_buflens[0].value_() < drgn.sizeof(body):
        return None
    return body


def inspect(prog, task):
    out = {"pid": task.pid.value_(), "comm": task.comm.string_().decode()}
    thr = task_ptlrpc_thread(prog, task)
    env = thr.t_env
    mti = key_value(prog, env.le_ctx, "mdt_thread_key")
    if mti is None:
        raise ValueError("thread has no mdt_thread_info")
    mti = cast("struct mdt_thread_info *", mti)
    out["mdt_thread_info"] = hex(mti.value_())
    # mdt_thread_info_fini() clears mti_pill when a request completes,
    # but leaves mti_rr. Without mti_pill, the names are from an
    # earlier request and the UID/NID fields are not available.
    out["state"] = ("request in progress" if mti.mti_pill.value_() else
                    "idle: names are from an EARLIER, finished request")

    rr = mti.mti_rr
    out["reint_op"] = REINT.get(rr.rr_opcode.value_(), rr.rr_opcode.value_())
    out["name_len"], out["name"] = read_name(prog, rr.rr_name)
    out["tgt_name_len"], out["tgt_name"] = read_name(prog, rr.rr_tgt_name)
    out["fid1 (parent of name)"] = fid2str(rr.rr_fid1)
    out["fid2 (parent of tgt_name)"] = fid2str(rr.rr_fid2)

    # mdt_*_unpack() copies the client fsuid/fsgid here before the
    # nodemap maps them, so this is the UID as the client sees it.
    attr = mti.mti_attr.ma_attr
    out["client uid"] = attr.la_uid.value_()
    out["client gid"] = attr.la_gid.value_()
    ct = attr.la_ctime.value_()
    if ct > 0:
        out["client time"] = datetime.datetime.fromtimestamp(
            ct, datetime.timezone.utc).isoformat()

    if not mti.mti_pill.value_():
        return out
    if env.le_ses.value_():
        uc = key_value(prog, env.le_ses[0], "lu_ucred_key")
        if uc is not None:
            uc = cast("struct lu_ucred *", uc)
            out["fs uid (after nodemap)"] = uc.uc_fsuid.value_()
            out["fs gid (after nodemap)"] = uc.uc_fsgid.value_()
            out["ucred jobid"] = uc.uc_jobid.string_().decode(
                errors="replace")
            out["ucred nid"] = nid2str(uc.uc_nid)

    req = mti.mti_pill.rc_req if mti.mti_pill.value_() else None
    if req is not None and req.value_():
        out["xid"] = req.rq_xid.value_()
        out["request peer"] = nid2str(req.rq_peer.nid)
        try:
            body = req_body(prog, req)
        except drgn.FaultError:
            body = None
        if body is not None:
            opc = body.pb_opc.value_()
            out["opcode"] = OPC.get(opc, opc)
            out["request jobid"] = body.pb_jobid.string_().decode(
                errors="replace")
    exp = mti.mti_exp
    if exp.value_():
        out["client uuid"] = exp.exp_client_uuid.uuid.string_().decode()
        if exp.exp_connection.value_():
            out["export nid"] = nid2str(exp.exp_connection.c_peer.nid)
        obd = exp.exp_obd
        if obd.value_():
            out["target"] = obd.obd_name.string_().decode()
    return out


def crashed_task(prog):
    try:
        return prog.crashed_thread().object
    except (AttributeError, ValueError, LookupError):
        pass
    for t in for_each_task(prog):
        try:
            for fr in prog.stack_trace(t):
                if fr.pc and prog.symbol(fr.pc).name.startswith(
                        "lbug_with_loc"):
                    return t
        except (ValueError, LookupError, drgn.FaultError):
            continue
    sys.exit("error: no crashed thread found; use --pid")


def mdt_tasks(prog):
    for t in for_each_task(prog):
        if t.comm.string_().startswith(b"mdt"):
            yield t


def show(rec):
    w = max(len(k) for k in rec)
    for k, v in rec.items():
        flag = ""
        if k in ("name_len", "tgt_name_len") and v > NAME_MAX:
            flag = "   <-- longer than NAME_MAX (255)"
        print(f"  {k:<{w}} : {v}{flag}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vmcore")
    ap.add_argument("--vmlinux")
    ap.add_argument("--debug-dir", action="append",
                    help="dir with lustre .ko/.ko.debug files (repeatable)")
    ap.add_argument("--live", action="store_true",
                    help="read the running kernel instead of a vmcore")
    ap.add_argument("--pid", type=int, action="append",
                    help="mdt thread PID (repeatable)")
    ap.add_argument("--scan", action="store_true",
                    help="check every mdt thread; show long names")
    ap.add_argument("--min-len", type=int, default=NAME_MAX + 1,
                    help="--scan shows names of this length or more "
                    "(default 256)")
    args = ap.parse_args()
    if not args.live and not args.vmcore:
        ap.error("give --vmcore (and --vmlinux) or --live")

    prog = load(args)
    if args.scan:
        tasks = list(mdt_tasks(prog))
    elif args.pid:
        tasks = [find_task(prog, p) for p in args.pid]
    else:
        tasks = [crashed_task(prog)]

    found = 0
    for t in tasks:
        try:
            rec = inspect(prog, t)
        except (ValueError, LookupError, AttributeError,
                drgn.FaultError) as e:
            if not args.scan:
                print(f"pid {t.pid.value_()}: {e}", file=sys.stderr)
            continue
        if args.scan and max(rec["name_len"],
                             rec["tgt_name_len"]) < args.min_len:
            continue
        found += 1
        show(rec)
    if args.scan and not found:
        print(f"no mdt thread holds a name of {args.min_len} bytes or more")


if __name__ == "__main__":
    main()
