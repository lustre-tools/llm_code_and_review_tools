#!/usr/bin/env python3
"""Port of epython obd_devs.py to drgn.

Displays the contents of global 'obd_devs' — lists active OBD devices
with import state, NID, connection count, and optional RPC stats.

Original: contrib/debug_tools/epython_scripts/obd_devs.py
Authors: Ann Koehler (Cray Inc.), ported to drgn by Claude.
"""

import argparse
import json
import sys

import drgn

try:
    from . import lustre_helpers as lh
except ImportError:
    import lustre_helpers as lh


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


def get_import_info(obd):
    """Extract import state, NID, and connection info from an OBD device."""
    result = {
        "nid": "LNET_NID_ANY",
        "imp_state": "--",
        "ish_time": 0,
        "index": -1,
        "connect_cnt": 0,
        "inflight": 0,
    }

    try:
        imp_ptr = obd.u.cli.cl_import.value_()
        if imp_ptr == 0:
            return result

        imp = obd.u.cli.cl_import[0]
        state_val = imp.imp_state.value_()
        result["imp_state"] = IMP_STATE.get(state_val, f"?({state_val})")
        result["index"] = imp.imp_state_hist_idx.value_() - 1
        idx = result["index"]
        if 0 < idx < 16:
            result["ish_time"] = imp.imp_state_hist[idx].ish_time.value_()
        result["inflight"] = imp.imp_inflight.counter.value_()
        result["connect_cnt"] = imp.imp_conn_cnt.value_()

        # Get NID
        if not imp.imp_invalid.value_():
            conn_ptr = imp.imp_connection.value_()
            if conn_ptr != 0:
                nid_obj = imp.imp_connection[0].c_peer.nid
                result["nid"] = lh.nid2str(nid_obj)
    except (drgn.FaultError, AttributeError):
        pass

    return result


def print_one_device(obd):
    """Print info for one OBD device."""
    name = lh.obd_name(obd)
    info = get_import_info(obd)

    try:
        cli_addr = obd.u.cli.address_of_().value_()
        imp_addr = obd.u.cli.cl_import.value_()
    except (drgn.FaultError, AttributeError):
        cli_addr = 0
        imp_addr = 0

    print(
        f"0x{obd.address_of_().value_():<17x} {name:<22s}\t{info['nid']:<22s}\t "
        f"0x{cli_addr:<17x} 0x{imp_addr:<17x} {info['imp_state']:<10s} "
        f"{info['ish_time']:<10d} {info['index']:>5d} {info['connect_cnt']:>5d}"
    )


def print_devices_text(prog, obd_addr=None):
    """Print all OBD devices in text format (matching epython output)."""
    print(
        f"{'obd_device':<19s} {'obd_name':<22s} \t{'ip_address':<22s} "
        f"{'client_obd':<19s} {'obd_import':<19s} {'imp_state':<12s} "
        f"{'ish_time':<10s} {'index':<7s} {'conn_cnt':<10s}"
    )
    print("=" * 152)

    if obd_addr:
        obd = drgn.Object(prog, "struct obd_device", address=obd_addr)
        print_one_device(obd)
    else:
        for _idx, obd in lh.get_obd_devices(prog):
            print_one_device(obd)

    print("=" * 152)


def get_devices_json(prog, obd_addr=None):
    """Return OBD device info as structured data."""
    devices = []

    if obd_addr:
        obd_list = [(0, drgn.Object(prog, "struct obd_device", address=obd_addr))]
    else:
        obd_list = lh.get_obd_devices(prog)

    for idx, obd in obd_list:
        try:
            name = lh.obd_name(obd)
            uuid = lh.obd_uuid(obd)
        except (drgn.FaultError, drgn.ObjectAbsentError):
            continue

        info = get_import_info(obd)

        dev_type = ""
        try:
            type_ptr = obd.obd_type.value_()
            if type_ptr != 0:
                dev_type = obd.obd_type[0].typ_name.string_().decode(errors="replace")
        except (drgn.FaultError, AttributeError):
            pass

        devices.append({
            "index": idx,
            "address": f"0x{obd.address_of_().value_():x}",
            "name": name,
            "uuid": uuid,
            "type": dev_type,
            "nid": info["nid"],
            "imp_state": info["imp_state"],
            "connect_cnt": info["connect_cnt"],
            "inflight": info["inflight"],
        })

    return {"analysis": "obd_devices", "count": len(devices), "devices": devices}


def main():
    try:
        from .lustre_analyze import load_program
    except ImportError:
        from lustre_analyze import load_program

    parser = argparse.ArgumentParser(
        description="Displays the contents of global 'obd_devs'",
    )
    parser.add_argument("--vmcore", required=True)
    parser.add_argument("--vmlinux", required=True)
    parser.add_argument("--mod-dir", default=None)
    parser.add_argument("--debug-dir", default=None)
    parser.add_argument("--text", action="store_true", help="Text output (default is JSON)")
    parser.add_argument("--pretty", action="store_true",
                        help="Pretty-print JSON (also set LUSTRE_DRGN_PRETTY=1)")
    parser.add_argument("obd_device", nargs="?", default=None, type=lambda x: int(x, 0))
    args = parser.parse_args()

    prog = load_program(args.vmcore, args.vmlinux, args.mod_dir, args.debug_dir)

    if args.text:
        print_devices_text(prog, args.obd_device)
    else:
        result = get_devices_json(prog, args.obd_device)
        lh.json_output(result, args)


if __name__ == "__main__":
    main()
