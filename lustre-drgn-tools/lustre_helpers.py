"""Lustre helper functions for drgn — port of epython lustrelib.py.

Provides utilities for navigating Lustre data structures in vmcore
files: NID formatting, OBD device access, cfs_hash traversal,
rb-tree iteration, and linked list helpers.

Original: contrib/debug_tools/epython_scripts/lustrelib.py
Authors: Ann Koehler (Cray Inc.), ported to drgn by Claude.
"""

import json
import os

import drgn
from drgn.helpers.linux.list import (
    hlist_for_each_entry,
    list_for_each_entry,
    list_empty,
)


# ── NID formatting ────────────────────────────────────────────


LNET_NID_ANY = 0xFFFFFFFFFFFFFFFF

SOCKLND = 2
PTLLND = 4
O2IBLND = 5
GNILND = 13
KFILND = 14


def lnet_nidaddr(nid: int) -> int:
    return nid & 0xFFFFFFFF


def lnet_nidnet(nid: int) -> int:
    return (nid >> 32) & 0xFFFFFFFF


def lnet_nettyp(net: int) -> int:
    return (net >> 16) & 0xFFFF


def lnet_netnum(net: int) -> int:
    return net & 0xFFFF


def nid2str(nid) -> str:
    """Convert a NID to a string like '10.0.0.1@tcp0'.

    Accepts either:
    - int: legacy lnet_nid_t (64-bit packed value)
    - drgn Object: struct lnet_nid (new large-NID format)
    """
    lnd_names = {
        SOCKLND: "tcp",
        O2IBLND: "o2ib",
        PTLLND: "ptl",
        GNILND: "gni",
        KFILND: "kfi",
    }

    # Handle drgn Object (struct lnet_nid)
    if not isinstance(nid, int):
        try:
            nid_type = int(nid.nid_type)
            # nid_num is __be16, read raw bytes
            nid_num = int(nid.nid_num)
            # nid_addr[0] is __be32 with the IPv4 address
            addr_be = int(nid.nid_addr[0])
            ip = "{}.{}.{}.{}".format(
                (addr_be >> 24) & 0xFF,
                (addr_be >> 16) & 0xFF,
                (addr_be >> 8) & 0xFF,
                addr_be & 0xFF,
            )
            name = lnd_names.get(nid_type, f"unknown{nid_type}")
            s = f"{ip}@{name}"
            if nid_num != 0:
                s += str(nid_num)
            return s
        except Exception:
            return "?"

    # Legacy int path
    if nid == LNET_NID_ANY:
        return "LNET_NID_ANY"

    addr = lnet_nidaddr(nid)
    net = lnet_nidnet(nid)
    lnd = lnet_nettyp(net)
    nnum = lnet_netnum(net)

    ip = "{}.{}.{}.{}".format(
        (addr >> 24) & 0xFF,
        (addr >> 16) & 0xFF,
        (addr >> 8) & 0xFF,
        addr & 0xFF,
    )

    if lnd in lnd_names:
        s = f"{ip}@{lnd_names[lnd]}" if lnd in (SOCKLND, O2IBLND) else f"{addr}@{lnd_names[lnd]}"
        if nnum != 0:
            s += str(nnum)
        return s

    return f"{addr}@unknown{lnd}"


# ── OBD device access ────────────────────────────────────────


OBD_DEVICE_MAGIC = 0xAB5CD6EF


def get_obd_devices(prog: drgn.Program) -> list:
    """Return list of (index, obd_device Object) for all active devices.

    obd_devs is an xarray (since Lustre moved from a flat array).
    Falls back to flat array iteration for older Lustre versions.
    """
    try:
        obd_devs_obj = prog["obd_devs"]
    except (KeyError, LookupError):
        return []

    # Try xarray iteration (current Lustre)
    type_str = str(obd_devs_obj.type_)
    if "xarray" in type_str:
        from drgn.helpers.linux.xarray import xa_for_each
        devices = []
        for index, entry in xa_for_each(obd_devs_obj.address_of_()):
            try:
                obd = drgn.cast("struct obd_device *", entry)
                if obd.obd_magic.value_() != OBD_DEVICE_MAGIC:
                    continue
                devices.append((index, obd[0]))
            except (drgn.FaultError, drgn.ObjectAbsentError):
                continue
        return devices

    # Fallback: flat array (older Lustre)
    sym = prog.symbol("obd_devs")
    arr = drgn.Object(
        prog, prog.type("struct obd_device *[8192]"),
        address=sym.address,
    )
    devices = []
    for i in range(8192):
        try:
            ptr = arr[i].value_()
            if ptr == 0 or ptr < 0xffff000000000000:
                continue
            obd = drgn.Object(prog, "struct obd_device", address=ptr)
            if obd.obd_magic.value_() != OBD_DEVICE_MAGIC:
                continue
            devices.append((i, obd))
        except (drgn.FaultError, drgn.ObjectAbsentError):
            continue
    return devices


def obd_name(obd: drgn.Object) -> str:
    """Get OBD device name."""
    return obd.obd_name.string_().decode(errors="replace")


def obd_uuid(obd: drgn.Object) -> str:
    """Get OBD device UUID."""
    return obd.obd_uuid.uuid.string_().decode(errors="replace")


def obd2nidstr(obd: drgn.Object) -> str:
    """Get NID string for an OBD device's import connection."""
    try:
        imp = obd.u.cli.cl_import[0]
        if imp.imp_invalid:
            return "LNET_NID_ANY"
        conn = imp.imp_connection
        if conn.value_() == 0:
            return "LNET_NID_ANY"
        nid = conn[0].c_peer.nid.value_()
        return nid2str(nid)
    except (drgn.FaultError, AttributeError):
        return "LNET_NID_ANY"


# ── cfs_hash traversal ────────────────────────────────────────


CFS_HASH_ADD_TAIL = 1 << 4
CFS_HASH_DEPTH = 1 << 12
CFS_HASH_TYPE_MASK = CFS_HASH_ADD_TAIL | CFS_HASH_DEPTH


def cfs_hash_nbkt(hsh: drgn.Object) -> int:
    return 1 << (hsh.hs_cur_bits.value_() - hsh.hs_bkt_bits.value_())


def cfs_hash_bkt_nhlist(hsh: drgn.Object) -> int:
    return 1 << hsh.hs_bkt_bits.value_()


def cfs_hash_for_each_node(prog: drgn.Program, hsh: drgn.Object):
    """Iterate over all hlist_node entries in a cfs_hash.

    Yields (bucket_index, offset, hlist_node) tuples.
    """
    hs_type = hsh.hs_flags.value_() & CFS_HASH_TYPE_MASK

    # Determine the head struct and field name based on hash type
    type_info = {
        0: ("struct cfs_hash_head", "hh_head"),
        CFS_HASH_DEPTH: ("struct cfs_hash_head_dep", "hd_head"),
        CFS_HASH_ADD_TAIL: ("struct cfs_hash_dhead", "dh_head"),
        CFS_HASH_DEPTH | CFS_HASH_ADD_TAIL: ("struct cfs_hash_dhead_dep", "dd_head"),
    }

    if hs_type not in type_info:
        return

    dt_struct, hd_field = type_info[hs_type]

    nbkt = cfs_hash_nbkt(hsh)
    nhlist = cfs_hash_bkt_nhlist(hsh)

    for bkt_idx in range(nbkt):
        bkt_ptr = hsh.hs_buckets[bkt_idx]
        if bkt_ptr.value_() == 0:
            continue

        for offset in range(nhlist):
            try:
                # Navigate to the head structure
                hsb_head_off = prog.type("struct cfs_hash_bucket").member("hsb_head").offset
                dt_size = prog.type(dt_struct).size
                head_addr = bkt_ptr.value_() + hsb_head_off + offset * dt_size

                head = drgn.Object(prog, dt_struct, address=head_addr)
                hd_off = prog.type(dt_struct).member(hd_field).offset
                hlist = drgn.Object(
                    prog, "struct hlist_head",
                    address=head_addr + hd_off,
                )

                # Walk the hlist
                node = hlist.first
                while node.value_() != 0:
                    yield bkt_idx, offset, node
                    try:
                        node = node[0].next
                    except drgn.FaultError:
                        break

            except (drgn.FaultError, drgn.ObjectAbsentError):
                continue


# ── Red-black tree helpers ────────────────────────────────────


def rb_first(root: drgn.Object):
    """Get the first (leftmost) node in an rb-tree."""
    n = root.rb_node
    if n.value_() == 0:
        return None
    while n[0].rb_left.value_() != 0:
        n = n[0].rb_left
    return n


def rb_next(prog: drgn.Program, node: drgn.Object):
    """Get the next node in rb-tree order."""
    # parent is encoded in __rb_parent_color with low bits as color
    parent_color = node[0].__rb_parent_color.value_() if hasattr(node[0], '__rb_parent_color') else 0

    def rb_parent(n):
        pc = n[0].__rb_parent_color.value_()
        addr = pc & ~3
        if addr == 0:
            return None
        return drgn.Object(prog, "struct rb_node *",
                           value=addr).read_()

    # If right child exists, go right then all the way left
    if node[0].rb_right.value_() != 0:
        node = node[0].rb_right
        while node[0].rb_left.value_() != 0:
            node = node[0].rb_left
        return node

    # Otherwise go up until we come from a left child
    parent = rb_parent(node)
    while parent is not None and node.value_() == parent[0].rb_right.value_():
        node = parent
        parent = rb_parent(node)

    return parent


# ── LDLM lock mode helpers ───────────────────────────────────


LDLM_LOCK_MODES = {
    0: "--",
    1: "EX",
    2: "PW",
    4: "PR",
    8: "CW",
    16: "CR",
    32: "NL",
    64: "GROUP",
}


def lockmode2str(mode: int) -> str:
    return LDLM_LOCK_MODES.get(mode, f"??({mode})")


# ── RPC opcode helpers ────────────────────────────────────────


# Common Lustre RPC opcodes
RPC_OPCODES = {
    1: "OST_REPLY",
    2: "OST_GETATTR",
    3: "OST_SETATTR",
    4: "OST_READ",
    5: "OST_WRITE",
    6: "OST_CREATE",
    7: "OST_DESTROY",
    8: "OST_GET_INFO",
    9: "OST_CONNECT",
    10: "OST_DISCONNECT",
    11: "OST_PUNCH",
    12: "OST_OPEN",
    13: "OST_CLOSE",
    14: "OST_STATFS",
    16: "OST_SYNC",
    17: "OST_SET_INFO",
    18: "OST_QUOTACHECK",
    19: "OST_QUOTACTL",
    20: "OST_QUOTA_ADJUST_QUNIT",
    21: "OST_LADVISE",
    22: "OST_FALLOCATE",
    23: "OST_SEEK",
    33: "MDS_GETATTR",
    34: "MDS_GETATTR_NAME",
    35: "MDS_CLOSE",
    36: "MDS_REINT",
    37: "MDS_READPAGE",
    38: "MDS_CONNECT",
    39: "MDS_DISCONNECT",
    40: "MDS_GET_ROOT",
    41: "MDS_STATFS",
    42: "MDS_PIN",
    43: "MDS_UNPIN",
    44: "MDS_SYNC",
    45: "MDS_DONE_WRITING",
    46: "MDS_SET_INFO",
    47: "MDS_QUOTACHECK",
    48: "MDS_QUOTACTL",
    49: "MDS_GETXATTR",
    50: "MDS_SWAP_LAYOUTS",
    51: "MDS_RMFID",
    52: "MDS_BATCH",
    101: "LDLM_ENQUEUE",
    102: "LDLM_CONVERT",
    103: "LDLM_CANCEL",
    104: "LDLM_BL_CALLBACK",
    105: "LDLM_CP_CALLBACK",
    106: "LDLM_GL_CALLBACK",
    107: "LDLM_SET_INFO",
    400: "MGS_CONNECT",
    401: "MGS_DISCONNECT",
    402: "MGS_EXCEPTION",
    403: "MGS_TARGET_REG",
    404: "MGS_TARGET_DEL",
    405: "MGS_SET_INFO",
    406: "MGS_CONFIG_READ",
    501: "OBD_PING",
    502: "OBD_LOG_CANCEL",
    503: "OBD_QC_CALLBACK",
    504: "OBD_IDX_READ",
    506: "LLOG_ORIGIN_HANDLE_CREATE",
    507: "LLOG_ORIGIN_HANDLE_NEXT_BLOCK",
    508: "LLOG_ORIGIN_HANDLE_READ_HEADER",
    510: "LLOG_ORIGIN_HANDLE_CLOSE",
    513: "LLOG_CATINFO",
    601: "SEQ_QUERY",
    700: "SEC_CTX_INIT",
    701: "SEC_CTX_INIT_CONT",
    702: "SEC_CTX_FINI",
    801: "FLD_QUERY",
    802: "FLD_READ",
    900: "OUT_UPDATE",
    1000: "LFSCK_NOTIFY",
    1001: "LFSCK_QUERY",
}


def opc2str(opc: int) -> str:
    return RPC_OPCODES.get(opc, f"UNKNOWN({opc})")


# ── Import state ─────────────────────────────────────────────

IMP_STATE = {
    1: "CLOSED",
    2: "NEW",
    3: "DISCON",
    4: "CONNECTING",
    5: "REPLAY",
    6: "REPLAY_LOCKS",
    7: "REPLAY_WAIT",
    8: "RECOVER",
    9: "FULL",
    10: "EVICTED",
    11: "IDLE",
}


def imp_state2str(state: int) -> str:
    return IMP_STATE.get(state, f"?({state})")


# ── Output helpers ───────────────────────────────────────────


def is_pretty(args=None):
    """Check if pretty output is requested via --pretty flag or env var.

    Checks (in order):
    1. args.pretty if args is provided and has the attribute
    2. LUSTRE_DRGN_PRETTY env var (any truthy value: 1, true, yes)
    """
    if args is not None and getattr(args, "pretty", False):
        return True
    env = os.environ.get("LUSTRE_DRGN_PRETTY", "").lower()
    return env in ("1", "true", "yes")


def json_output(data, args=None, pretty=None):
    """Print data as JSON. Default output format for all scripts.

    Args:
        data: dict/list to serialize
        args: argparse namespace (checked for .pretty)
        pretty: explicit override (True/False), or None to auto-detect
    """
    if pretty is None:
        pretty = is_pretty(args)
    indent = 2 if pretty else None
    print(json.dumps(data, indent=indent, default=str))


# ── FID-to-path resolution ───────────────────────────────────


def dentry_path(prog: drgn.Program, dentry: drgn.Object) -> str:
    """Walk d_parent chain to build full path string.

    Stops at mount root (where d_parent == self). Returns the
    reconstructed path or "/" if dentry is the root.
    """
    components = []
    seen = set()
    d = dentry
    try:
        while True:
            addr = d.value_() if hasattr(d, 'value_') else int(d)
            if addr in seen:
                break
            seen.add(addr)

            parent = d.d_parent
            parent_addr = parent.value_() if hasattr(parent, 'value_') else int(parent)

            # At mount root, d_parent points to self
            if parent_addr == addr:
                break

            name = d.d_name.name.string_().decode(errors="replace")
            if name:
                components.append(name)
            d = parent
    except drgn.FaultError:
        pass

    if not components:
        return "/"
    components.reverse()
    return "/" + "/".join(components)


def inode_to_path(prog: drgn.Program, inode: drgn.Object) -> str:
    """Given a VFS inode, try to find a dentry via i_dentry hlist.

    Returns path string or None if no cached dentry exists.
    """
    try:
        for dentry in hlist_for_each_entry(
            "struct dentry", inode.i_dentry, "d_u.d_alias"
        ):
            path = dentry_path(prog, dentry)
            if path:
                return path
    except (drgn.FaultError, AttributeError, TypeError):
        pass
    return None


def build_fid_path_cache(prog: drgn.Program,
                         max_inodes: int = 10000) -> dict:
    """Iterate all Lustre superblocks and build FID->path cache.

    Walks s_inodes for each superblock whose s_type->name is
    "lustre", extracts the FID from ll_inode_info.lli_fid, and
    resolves paths via the dentry cache.

    Returns dict mapping "seq:oid:ver" -> path string.
    Only includes inodes that have cached dentries.
    """
    cache = {}
    count = 0

    try:
        # Iterate the global super_blocks list
        super_blocks = prog["super_blocks"]
    except (KeyError, drgn.FaultError):
        return cache

    try:
        for sb in list_for_each_entry(
            "struct super_block", super_blocks.address_of_(), "s_list"
        ):
            # Check if this is a Lustre filesystem
            try:
                fs_name = sb.s_type.name.string_().decode(errors="replace")
                if fs_name != "lustre":
                    continue
            except (drgn.FaultError, AttributeError):
                continue

            # Walk s_inodes for this superblock
            try:
                for inode in list_for_each_entry(
                    "struct inode", sb.s_inodes.address_of_(), "i_sb_list"
                ):
                    if count >= max_inodes:
                        break
                    count += 1

                    try:
                        # container_of(inode, struct ll_inode_info,
                        #              lli_vfs_inode)
                        lli = drgn.container_of(inode, "struct ll_inode_info",
                                                "lli_vfs_inode")
                        fid = lli.lli_fid
                        seq = fid.f_seq.value_()
                        oid = fid.f_oid.value_()
                        ver = fid.f_ver.value_()

                        # Skip zero FIDs
                        if seq == 0 and oid == 0:
                            continue

                        path = inode_to_path(prog, inode)
                        if path is None:
                            continue

                        fid_str = f"{seq:#x}:{oid:#x}:{ver:#x}"
                        cache[fid_str] = path
                    except (drgn.FaultError, AttributeError):
                        continue
            except (drgn.FaultError, AttributeError):
                continue

            if count >= max_inodes:
                break
    except (drgn.FaultError, AttributeError):
        pass

    return cache


def fid_to_path(fid_cache: dict, seq: int, oid: int, ver: int) -> str:
    """Look up a FID in the cache dict.

    Returns the path string or None if the FID is not cached.
    """
    fid_str = f"{seq:#x}:{oid:#x}:{ver:#x}"
    return fid_cache.get(fid_str)


def resource_name_to_fid_str(res_name_parts: list) -> str:
    """Convert LDLM resource name to a FID string.

    For MDT resources, the resource name IS the FID:
      name[0] = f_seq, name[1] = f_oid, name[2] = f_ver.

    Args:
        res_name_parts: list/tuple of the 4 __u64 resource name values

    Returns:
        FID string in "seq:oid:ver" hex format
    """
    seq = int(res_name_parts[0])
    oid = int(res_name_parts[1])
    ver = int(res_name_parts[2])
    return f"{seq:#x}:{oid:#x}:{ver:#x}"
