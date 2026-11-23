# Lens reviews: per-bug benchmark results

Which known bug each review variant found, and in how many of its runs.
These are the per-bug numbers behind `lens-review-evaluation.md`, which
explains the variants, the case sets and how the runs were made: Sonnet 5.5
reviewer at `--effort high` unless a column says otherwise, `--lean`,
`--preload`, offline, judged by `lreview bench report --judge`, November 2026.

A cell is *runs that found the bug / runs that completed*; a run that read
the later fix is left out, and `-` means the variant did not review that
case. "Lenses at high" is `--lenses --lens-effort high`; "lenses at
medium" is the `--lenses` default. "Strict verification" is the way the
reviewer settles lens candidates since 2026-11-17. The bugs are the known
bugs of `lreview/benchmark/cases.json` as it is now: `test238-setup-rc-unchecked`
was dropped on 2026-11-17 (not a bug), so totals here are one or two lower
than in tables measured before that. The appendix says what each bug is.

## Pilot round

19 cases (the `hard` and `quick` sets), three runs each.

| case (change) | bug | ordinary | + TodoWrite | + recall line | lenses at high |
|---|---|---|---|---|---|
| swap-layouts-interop ([64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2)) | swab-min-size | 3/3 | 3/3 | 3/3 | 3/3 |
| swap-layouts-interop ([64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2)) | pack-before-set-size | 3/3 | 3/3 | 3/3 | 3/3 |
| lnet-recreate-peers ([61596](https://review.whamcloud.com/c/fs/lustre-release/+/61596/17)) | sleep-in-spinlock | 3/3 | 3/3 | 3/3 | 3/3 |
| memcg-balance-dirty ([59223](https://review.whamcloud.com/c/fs/lustre-release/+/59223/11)) | unlocked-tree-dump | 0/3 | 0/3 | 1/3 | 1/3 |
| lassert-side-effects ([56115](https://review.whamcloud.com/c/fs/lustre-release/+/56115/5)) | state-change-in-assert | 3/3 | 3/3 | 3/3 | 3/3 |
| osc-kswapd-deadlock ([64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2)) | oe-memalloc-never-set-dead-check | 3/3 | 3/3 | 3/3 | 3/3 |
| osc-kswapd-deadlock ([64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2)) | comment-cites-removed-osc-flush-async-page | 3/3 | 3/3 | 3/3 | 3/3 |
| nodemap-ban-default ([60074](https://review.whamcloud.com/c/fs/lustre-release/+/60074/15)) | ban-search-unlocked | 0/3 | 1/3 | 0/3 | 1/3 |
| lnetctl-import ([65055](https://review.whamcloud.com/c/fs/lustre-release/+/65055/8)) | import-fgets-overflow | 0/3 | 0/3 | 0/3 | 0/3 |
| dne-migrate-layout ([58585](https://review.whamcloud.com/c/fs/lustre-release/+/58585/8)) | fld-range-uninit | 1/3 | 1/3 | 2/3 | 3/3 |
| osc-lu-device ([58818](https://review.whamcloud.com/c/fs/lustre-release/+/58818/8)) | device-free-null-obd | 3/3 | 2/3 | 3/3 | 3/3 |
| llog-subcommands ([56040](https://review.whamcloud.com/c/fs/lustre-release/+/56040/8)) | remove-runs-check | 3/3 | 3/3 | 3/3 | 3/3 |
| nodemap-fileset-inherit ([55614](https://review.whamcloud.com/c/fs/lustre-release/+/55614/24)) | fileset-parent-check | 0/3 | 0/3 | 0/3 | 0/3 |
| mgs-buffer-checks ([64953](https://review.whamcloud.com/c/fs/lustre-release/+/64953/4)) | skip-ipv6-uninit | 0/3 | 2/3 | 1/3 | 3/3 |
| lnd-module-loading ([57946](https://review.whamcloud.com/c/fs/lustre-release/+/57946/3)) | err-ptr-positive | 3/3 | 3/3 | 3/3 | 3/3 |
| move-include-to-top-level ([60989](https://review.whamcloud.com/c/fs/lustre-release/+/60989/9)) | fix-shrinker-debug-init-fini-i | 3/3 | 3/3 | 3/3 | 3/3 |
| relate-qmt-lvbo-free-wq-and ([56661](https://review.whamcloud.com/c/fs/lustre-release/+/56661/3)) | fix-the-order-of-freeing-qmt-l | 0/3 | 0/3 | 0/3 | 0/3 |
| lfs-find-ost-supports-index ([55902](https://review.whamcloud.com/c/fs/lustre-release/+/55902/5)) | fix-lfs-find-m-mdtidx-negation | 3/3 | 3/3 | 3/3 | 3/3 |
| support-pci-p2p-dma-mapping ([66985](https://review.whamcloud.com/c/fs/lustre-release/+/66985/26)) | fix-efa-p2p-configure-guard | 3/3 | 3/3 | 2/3 | 3/3 |
| return-error-for-verify-when ([61751](https://review.whamcloud.com/c/fs/lustre-release/+/61751/8)) | fix-resync-with-nosync-stale-c | 0/3 | 0/3 | 0/3 | 0/3 |
| enforce-file-quota-for-root ([60213](https://review.whamcloud.com/c/fs/lustre-release/+/60213/9)) | fix-silent-data-loss-for-root | 0/3 | 0/3 | 0/3 | 0/3 |
| **total** | | **37/63 (59%)** | **39/63 (62%)** | **39/63 (62%)** | **44/63 (70%)** |

## Eval round

The 32 `eval` cases, two runs each. One ordinary run of `o2iblnd-rtnl` read the later fix and is left out.

| case (change) | bug | ordinary | lenses at high |
|---|---|---|---|
| swap-layouts-interop ([64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2)) | swab-min-size | 2/2 | 2/2 |
| swap-layouts-interop ([64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2)) | pack-before-set-size | 2/2 | 2/2 |
| ping-evictor-rework ([58620](https://review.whamcloud.com/c/fs/lustre-release/+/58620/3)) | export-timer-alloc | 2/2 | 2/2 |
| lmv-qos-exclude ([60794](https://review.whamcloud.com/c/fs/lustre-release/+/60794/9)) | exclude-list-uaf | 2/2 | 2/2 |
| mgc-new-registration ([59212](https://review.whamcloud.com/c/fs/lustre-release/+/59212/8)) | legacy-nid-oob | 0/2 | 2/2 |
| shrinker-compat ([60352](https://review.whamcloud.com/c/fs/lustre-release/+/60352/7)) | shrinker-alloc-leak | 2/2 | 2/2 |
| lnet-recreate-peers ([61596](https://review.whamcloud.com/c/fs/lustre-release/+/61596/17)) | sleep-in-spinlock | 2/2 | 2/2 |
| o2iblnd-rtnl ([63038](https://review.whamcloud.com/c/fs/lustre-release/+/63038/5)) | rtnl-in-rcu | 1/1 | 2/2 |
| o2iblnd-rtnl ([63038](https://review.whamcloud.com/c/fs/lustre-release/+/63038/5)) | rtnl-under-global-lock | 1/1 | 2/2 |
| quota-nodemap ([59297](https://review.whamcloud.com/c/fs/lustre-release/+/59297/5)) | nodemap-put-early | 2/2 | 2/2 |
| mgc-nid-notify ([59868](https://review.whamcloud.com/c/fs/lustre-release/+/59868/11)) | unchecked-reply | 2/2 | 2/2 |
| socklnd-restrict-ip ([59673](https://review.whamcloud.com/c/fs/lustre-release/+/59673/15)) | waitqueue-before-init | 2/2 | 2/2 |
| socklnd-restrict-ip ([59673](https://review.whamcloud.com/c/fs/lustre-release/+/59673/15)) | callback-not-restored | 0/2 | 0/2 |
| lassert-side-effects ([56115](https://review.whamcloud.com/c/fs/lustre-release/+/56115/5)) | state-change-in-assert | 2/2 | 2/2 |
| osc-kswapd-deadlock ([64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2)) | oe-memalloc-never-set-dead-check | 2/2 | 2/2 |
| osc-kswapd-deadlock ([64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2)) | comment-cites-removed-osc-flush-async-page | 2/2 | 2/2 |
| lnet-mr-forwarding ([63582](https://review.whamcloud.com/c/fs/lustre-release/+/63582/5)) | test237-238-no-interop-version-gate | 0/2 | 1/2 |
| mount-large-nids ([57560](https://review.whamcloud.com/c/fs/lustre-release/+/57560/10)) | append-param-nul | 2/2 | 2/2 |
| fsinfo-long-nids ([59421](https://review.whamcloud.com/c/fs/lustre-release/+/59421/5)) | root-path-want | 2/2 | 2/2 |
| lu-env-add-errors ([57272](https://review.whamcloud.com/c/fs/lustre-release/+/57272/12)) | osp-thread-args-leak | 2/2 | 2/2 |
| fallocate-no-restart ([59158](https://review.whamcloud.com/c/fs/lustre-release/+/59158/4)) | restart-offset-units | 2/2 | 2/2 |
| nodemap-innermost-range ([59890](https://review.whamcloud.com/c/fs/lustre-release/+/59890/10)) | orphaned-subranges | 2/2 | 2/2 |
| fileset-llog-compat ([60006](https://review.whamcloud.com/c/fs/lustre-release/+/60006/10)) | nodemap-del-null | 2/2 | 2/2 |
| resource-id-check ([59208](https://review.whamcloud.com/c/fs/lustre-release/+/59208/6)) | getxattr-check-order | 2/2 | 1/2 |
| llog-subcommands ([56040](https://review.whamcloud.com/c/fs/lustre-release/+/56040/8)) | remove-runs-check | 2/2 | 2/2 |
| changelog-enc-names ([55916](https://review.whamcloud.com/c/fs/lustre-release/+/55916/8)) | ns-store-null-target | 1/2 | 2/2 |
| mgs-buffer-checks ([64953](https://review.whamcloud.com/c/fs/lustre-release/+/64953/4)) | skip-ipv6-uninit | 0/2 | 2/2 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | delayed-work-not-stopped | 2/2 | 2/2 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | svc-stats-unprotected | 0/2 | 0/2 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | cpu-index-math | 2/2 | 2/2 |
| lnd-module-loading ([57946](https://review.whamcloud.com/c/fs/lustre-release/+/57946/3)) | err-ptr-positive | 2/2 | 2/2 |
| grant-rpc-failure ([56547](https://review.whamcloud.com/c/fs/lustre-release/+/56547/5)) | o-grant-not-reset | 2/2 | 2/2 |
| refresh-context-vs-lbug-upon ([62467](https://review.whamcloud.com/c/fs/lustre-release/+/62467/5)) | refresh-context-vs-lbug-upon-g | 2/2 | 2/2 |
| lfs-find-ost-supports-index ([55902](https://review.whamcloud.com/c/fs/lustre-release/+/55902/5)) | fix-lfs-find-m-mdtidx-negation | 2/2 | 2/2 |
| use-nidmasks-for-ipv6-nids ([56184](https://review.whamcloud.com/c/fs/lustre-release/+/56184/10)) | fix-nidlist-leak-in-nodemap-pa | 2/2 | 2/2 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-sg-table-free-after-orig-n | 2/2 | 2/2 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-error-handling-in-dma-mapp | 1/2 | 2/2 |
| replace-client-lock-lru-with ([60162](https://review.whamcloud.com/c/fs/lustre-release/+/60162/28)) | fix-lfru-priv-ratio-limit-over | 2/2 | 2/2 |
| specify-total-count-for-mirr ([55867](https://review.whamcloud.com/c/fs/lustre-release/+/55867/11)) | fix-issues-with-llapi-get-lmm | 2/2 | 2/2 |
| **total** | | **64/76 (84%)** | **72/78 (92%)** |

## Cost round

7 cases where the lens review had beaten the ordinary one, two runs each. "Ordinary" pools the pilot and eval runs of these cases; "lenses at high" was run again here, so the cheaper variants are compared with fresh runs.

| case (change) | bug | ordinary | lenses at high | lenses at medium | 3 lenses, high | Haiku lenses |
|---|---|---|---|---|---|---|
| mgc-new-registration ([59212](https://review.whamcloud.com/c/fs/lustre-release/+/59212/8)) | legacy-nid-oob | 0/2 | 2/2 | 1/2 | 1/2 | 1/2 |
| lnet-mr-forwarding ([63582](https://review.whamcloud.com/c/fs/lustre-release/+/63582/5)) | test237-238-no-interop-version-gate | 0/2 | 1/2 | 1/2 | 0/2 | 0/2 |
| nodemap-ban-default ([60074](https://review.whamcloud.com/c/fs/lustre-release/+/60074/15)) | ban-search-unlocked | 0/3 | 0/2 | 0/2 | 0/2 | 0/2 |
| dne-migrate-layout ([58585](https://review.whamcloud.com/c/fs/lustre-release/+/58585/8)) | fld-range-uninit | 1/3 | 2/2 | 2/2 | 2/2 | 2/2 |
| changelog-enc-names ([55916](https://review.whamcloud.com/c/fs/lustre-release/+/55916/8)) | ns-store-null-target | 1/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| mgs-buffer-checks ([64953](https://review.whamcloud.com/c/fs/lustre-release/+/64953/4)) | skip-ipv6-uninit | 0/5 | 2/2 | 0/2 | 1/2 | 2/2 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-sg-table-free-after-orig-n | 2/2 | 2/2 | 2/2 | 2/2 | 1/2 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-error-handling-in-dma-mapp | 1/2 | 2/2 | 2/2 | 2/2 | 1/2 |
| **total** | | **5/21 (24%)** | **13/16 (81%)** | **10/16 (62%)** | **10/16 (62%)** | **9/16 (56%)** |

## Verification round

8 cases where lens reviews had dropped bugs that a lens had found, two runs each (one for the last column). All lens columns: lenses at medium. "Ordinary" pools the pilot and eval runs of these cases.

| case (change) | bug | ordinary | lenses, settled as before | strict verification | strict, reviewer at medium |
|---|---|---|---|---|---|
| mgc-new-registration ([59212](https://review.whamcloud.com/c/fs/lustre-release/+/59212/8)) | legacy-nid-oob | 0/2 | 0/2 | 2/2 | 1/1 |
| memcg-balance-dirty ([59223](https://review.whamcloud.com/c/fs/lustre-release/+/59223/11)) | unlocked-tree-dump | 0/3 | 0/2 | 0/2 | 0/1 |
| lnet-mr-forwarding ([63582](https://review.whamcloud.com/c/fs/lustre-release/+/63582/5)) | test237-238-no-interop-version-gate | 0/2 | 0/2 | 2/2 | 1/1 |
| nodemap-ban-default ([60074](https://review.whamcloud.com/c/fs/lustre-release/+/60074/15)) | ban-search-unlocked | 0/3 | 0/2 | 0/2 | 0/1 |
| lnetctl-import ([65055](https://review.whamcloud.com/c/fs/lustre-release/+/65055/8)) | import-fgets-overflow | 0/3 | 0/2 | 0/2 | 0/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | delayed-work-not-stopped | 2/2 | 2/2 | 2/2 | 1/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | svc-stats-unprotected | 0/2 | 0/2 | 0/2 | 0/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | cpu-index-math | 2/2 | 2/2 | 2/2 | 1/1 |
| relate-qmt-lvbo-free-wq-and ([56661](https://review.whamcloud.com/c/fs/lustre-release/+/56661/3)) | fix-the-order-of-freeing-qmt-l | 0/3 | 0/2 | 1/2 | 0/1 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-sg-table-free-after-orig-n | 2/2 | 2/2 | 2/2 | 1/1 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-error-handling-in-dma-mapp | 1/2 | 2/2 | 2/2 | 1/1 |
| **total** | | **7/26 (27%)** | **8/22 (36%)** | **13/22 (59%)** | **6/11 (55%)** |

## Full-set round

Strict verification on the full sets, and the two cost changes (the lens
prompt with lens.md and the diff, "D1"; the reviewer waiting for the
lenses, "D2"), one run each unless a cell says otherwise. Reviewer at
high, lenses at medium unless a column says otherwise.

### Pilot set and the nine aireview escapes

The 19 cases of the pilot round plus the nine cases added from changes
aireview had reviewed. "Ordinary" is the pilot round's three runs; for the
nine new cases it is the two runs recorded in each case's notes.

| case (change) | bug | ordinary | strict, lenses medium | + D1 + D2, lenses medium | + D1 + D2, lenses high |
|---|---|---|---|---|---|
| debug-vlen-always-inline ([69231](https://review.whamcloud.com/c/fs/lustre-release/+/69231/10)) | va-list-in-static-inline | 0/2 | 0/1 | 0/1 | 1/1 |
| dne-migrate-layout ([58585](https://review.whamcloud.com/c/fs/lustre-release/+/58585/8)) | fld-range-uninit | 1/3 | 1/1 | 1/1 | 1/1 |
| ec-parity-km-zero ([62906](https://review.whamcloud.com/c/fs/lustre-release/+/62906/42)) | ec-k-zero-divide | 2/2 | 1/1 | 1/1 | 1/1 |
| ec-parity-km-zero ([62906](https://review.whamcloud.com/c/fs/lustre-release/+/62906/42)) | ec-m-zero-lassert | 2/2 | 1/1 | 1/1 | 1/1 |
| enforce-file-quota-for-root ([60213](https://review.whamcloud.com/c/fs/lustre-release/+/60213/9)) | fix-silent-data-loss-for-root | 0/3 | 1/1 | 1/1 | 1/1 |
| gssiam-modprobe-loop ([65062](https://review.whamcloud.com/c/fs/lustre-release/+/65062/78)) | wireflavor2policy-loop | 0/2 | 1/1 | 1/1 | 1/1 |
| lassert-side-effects ([56115](https://review.whamcloud.com/c/fs/lustre-release/+/56115/5)) | state-change-in-assert | 3/3 | 1/1 | 1/1 | 1/1 |
| lfs-find-ost-supports-index ([55902](https://review.whamcloud.com/c/fs/lustre-release/+/55902/5)) | fix-lfs-find-m-mdtidx-negation | 3/3 | 1/1 | 1/1 | 1/1 |
| llog-skip-plain-client ([60240](https://review.whamcloud.com/c/fs/lustre-release/+/60240/27)) | skip-plain-server-only | 2/2 | 1/1 | 1/1 | 1/1 |
| llog-subcommands ([56040](https://review.whamcloud.com/c/fs/lustre-release/+/56040/8)) | remove-runs-check | 3/3 | 1/1 | 1/1 | 1/1 |
| lmv-disconnect-uaf ([64317](https://review.whamcloud.com/c/fs/lustre-release/+/64317/23)) | upcall-clear-after-disconnect | 2/2 | 1/1 | 1/1 | 1/1 |
| lnd-module-loading ([57946](https://review.whamcloud.com/c/fs/lustre-release/+/57946/3)) | err-ptr-positive | 3/3 | 1/1 | 1/1 | 1/1 |
| lnet-recreate-peers ([61596](https://review.whamcloud.com/c/fs/lustre-release/+/61596/17)) | sleep-in-spinlock | 3/3 | 1/1 | 1/1 | 1/1 |
| lnetctl-import ([65055](https://review.whamcloud.com/c/fs/lustre-release/+/65055/8)) | import-fgets-overflow | 0/3 | 0/1 | 0/1 | 0/1 |
| memcg-balance-dirty ([59223](https://review.whamcloud.com/c/fs/lustre-release/+/59223/11)) | unlocked-tree-dump | 0/3 | 0/1 | 1/1 | 0/1 |
| mgs-buffer-checks ([64953](https://review.whamcloud.com/c/fs/lustre-release/+/64953/4)) | skip-ipv6-uninit | 0/3 | 0/1 | 1/1 | 1/1 |
| move-include-to-top-level ([60989](https://review.whamcloud.com/c/fs/lustre-release/+/60989/9)) | fix-shrinker-debug-init-fini-i | 3/3 | 1/1 | 1/1 | 1/1 |
| nodemap-ban-default ([60074](https://review.whamcloud.com/c/fs/lustre-release/+/60074/15)) | ban-search-unlocked | 0/3 | 0/1 | 0/1 | 0/1 |
| nodemap-fileset-inherit ([55614](https://review.whamcloud.com/c/fs/lustre-release/+/55614/24)) | fileset-parent-check | 0/3 | 0/1 | 0/1 | 0/1 |
| o2iblnd-fastreg-gaps ([66689](https://review.whamcloud.com/c/fs/lustre-release/+/66689/3)) | mid-page-split-counted-as-gap | 0/2 | 1/1 | 1/1 | 1/1 |
| ofd-failure-domain-race ([65184](https://review.whamcloud.com/c/fs/lustre-release/+/65184/18)) | failure-domain-unlocked-read | 0/2 | 1/1 | 1/1 | 1/1 |
| osc-encrypt-unused-index ([69505](https://review.whamcloud.com/c/fs/lustre-release/+/69505/9)) | fallback-macro-drops-index | 0/2 | 1/1 | 1/1 | 1/1 |
| osc-kswapd-deadlock ([64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2)) | comment-cites-removed-osc-flush-async-page | 3/3 | 1/1 | 1/1 | 1/1 |
| osc-kswapd-deadlock ([64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2)) | oe-memalloc-never-set-dead-check | 3/3 | 1/1 | 1/1 | 1/1 |
| osc-lu-device ([58818](https://review.whamcloud.com/c/fs/lustre-release/+/58818/8)) | device-free-null-obd | 3/3 | 1/1 | 1/1 | 1/1 |
| p2pdma-per-stripe ([66987](https://review.whamcloud.com/c/fs/lustre-release/+/66987/30)) | p2p-decided-per-chunk | 2/2 | 1/1 | 1/1 | 1/1 |
| relate-qmt-lvbo-free-wq-and ([56661](https://review.whamcloud.com/c/fs/lustre-release/+/56661/3)) | fix-the-order-of-freeing-qmt-l | 0/3 | 0/1 | 1/1 | 1/1 |
| return-error-for-verify-when ([61751](https://review.whamcloud.com/c/fs/lustre-release/+/61751/8)) | fix-resync-with-nosync-stale-c | 0/3 | 0/1 | 0/1 | 0/1 |
| support-pci-p2p-dma-mapping ([66985](https://review.whamcloud.com/c/fs/lustre-release/+/66985/26)) | fix-efa-p2p-configure-guard | 3/3 | 1/1 | 1/1 | 1/1 |
| swap-layouts-interop ([64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2)) | pack-before-set-size | 3/3 | 1/1 | 1/1 | 1/1 |
| swap-layouts-interop ([64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2)) | swab-min-size | 3/3 | 1/1 | 1/1 | 1/1 |
| **total** | | **47/83 (57%)** | **23/31 (74%)** | **26/31 (84%)** | **26/31 (84%)** |

$/review: strict $1.56, + D1 + D2 $1.02, + D1 + D2 with lenses at high
$2.04.

### Eval set

| case (change) | bug | ordinary | lenses at high, before strict | strict, lenses medium |
|---|---|---|---|---|
| changelog-enc-names ([55916](https://review.whamcloud.com/c/fs/lustre-release/+/55916/8)) | ns-store-null-target | 1/2 | 2/2 | 1/1 |
| fallocate-no-restart ([59158](https://review.whamcloud.com/c/fs/lustre-release/+/59158/4)) | restart-offset-units | 2/2 | 2/2 | 1/1 |
| fileset-llog-compat ([60006](https://review.whamcloud.com/c/fs/lustre-release/+/60006/10)) | nodemap-del-null | 2/2 | 2/2 | 1/1 |
| fsinfo-long-nids ([59421](https://review.whamcloud.com/c/fs/lustre-release/+/59421/5)) | root-path-want | 2/2 | 2/2 | 1/1 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-error-handling-in-dma-mapp | 1/2 | 2/2 | 1/1 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-sg-table-free-after-orig-n | 2/2 | 2/2 | 1/1 |
| grant-rpc-failure ([56547](https://review.whamcloud.com/c/fs/lustre-release/+/56547/5)) | o-grant-not-reset | 2/2 | 2/2 | 1/1 |
| lassert-side-effects ([56115](https://review.whamcloud.com/c/fs/lustre-release/+/56115/5)) | state-change-in-assert | 2/2 | 2/2 | 1/1 |
| lfs-find-ost-supports-index ([55902](https://review.whamcloud.com/c/fs/lustre-release/+/55902/5)) | fix-lfs-find-m-mdtidx-negation | 2/2 | 2/2 | 1/1 |
| llog-subcommands ([56040](https://review.whamcloud.com/c/fs/lustre-release/+/56040/8)) | remove-runs-check | 2/2 | 2/2 | 1/1 |
| lmv-qos-exclude ([60794](https://review.whamcloud.com/c/fs/lustre-release/+/60794/9)) | exclude-list-uaf | 2/2 | 2/2 | 1/1 |
| lnd-module-loading ([57946](https://review.whamcloud.com/c/fs/lustre-release/+/57946/3)) | err-ptr-positive | 2/2 | 2/2 | 1/1 |
| lnet-mr-forwarding ([63582](https://review.whamcloud.com/c/fs/lustre-release/+/63582/5)) | test237-238-no-interop-version-gate | 0/2 | 1/2 | 1/1 |
| lnet-recreate-peers ([61596](https://review.whamcloud.com/c/fs/lustre-release/+/61596/17)) | sleep-in-spinlock | 2/2 | 2/2 | 1/1 |
| lu-env-add-errors ([57272](https://review.whamcloud.com/c/fs/lustre-release/+/57272/12)) | osp-thread-args-leak | 2/2 | 2/2 | 1/1 |
| mgc-new-registration ([59212](https://review.whamcloud.com/c/fs/lustre-release/+/59212/8)) | legacy-nid-oob | 0/2 | 2/2 | 0/1 |
| mgc-nid-notify ([59868](https://review.whamcloud.com/c/fs/lustre-release/+/59868/11)) | unchecked-reply | 2/2 | 2/2 | 1/1 |
| mgs-buffer-checks ([64953](https://review.whamcloud.com/c/fs/lustre-release/+/64953/4)) | skip-ipv6-uninit | 0/2 | 2/2 | 0/1 |
| mount-large-nids ([57560](https://review.whamcloud.com/c/fs/lustre-release/+/57560/10)) | append-param-nul | 2/2 | 2/2 | 1/1 |
| nodemap-innermost-range ([59890](https://review.whamcloud.com/c/fs/lustre-release/+/59890/10)) | orphaned-subranges | 2/2 | 2/2 | 1/1 |
| o2iblnd-rtnl ([63038](https://review.whamcloud.com/c/fs/lustre-release/+/63038/5)) | rtnl-in-rcu | 1/1 | 2/2 | 1/1 |
| o2iblnd-rtnl ([63038](https://review.whamcloud.com/c/fs/lustre-release/+/63038/5)) | rtnl-under-global-lock | 1/1 | 2/2 | 1/1 |
| osc-kswapd-deadlock ([64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2)) | comment-cites-removed-osc-flush-async-page | 2/2 | 2/2 | 1/1 |
| osc-kswapd-deadlock ([64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2)) | oe-memalloc-never-set-dead-check | 2/2 | 2/2 | 1/1 |
| ping-evictor-rework ([58620](https://review.whamcloud.com/c/fs/lustre-release/+/58620/3)) | export-timer-alloc | 2/2 | 2/2 | 1/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | cpu-index-math | 2/2 | 2/2 | 1/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | delayed-work-not-stopped | 2/2 | 2/2 | 1/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | svc-stats-unprotected | 0/2 | 0/2 | 0/1 |
| quota-nodemap ([59297](https://review.whamcloud.com/c/fs/lustre-release/+/59297/5)) | nodemap-put-early | 2/2 | 2/2 | 1/1 |
| refresh-context-vs-lbug-upon ([62467](https://review.whamcloud.com/c/fs/lustre-release/+/62467/5)) | refresh-context-vs-lbug-upon-g | 2/2 | 2/2 | 1/1 |
| replace-client-lock-lru-with ([60162](https://review.whamcloud.com/c/fs/lustre-release/+/60162/28)) | fix-lfru-priv-ratio-limit-over | 2/2 | 2/2 | 1/1 |
| resource-id-check ([59208](https://review.whamcloud.com/c/fs/lustre-release/+/59208/6)) | getxattr-check-order | 2/2 | 1/2 | 0/1 |
| shrinker-compat ([60352](https://review.whamcloud.com/c/fs/lustre-release/+/60352/7)) | shrinker-alloc-leak | 2/2 | 2/2 | 1/1 |
| socklnd-restrict-ip ([59673](https://review.whamcloud.com/c/fs/lustre-release/+/59673/15)) | callback-not-restored | 0/2 | 0/2 | 0/1 |
| socklnd-restrict-ip ([59673](https://review.whamcloud.com/c/fs/lustre-release/+/59673/15)) | waitqueue-before-init | 2/2 | 2/2 | 1/1 |
| specify-total-count-for-mirr ([55867](https://review.whamcloud.com/c/fs/lustre-release/+/55867/11)) | fix-issues-with-llapi-get-lmm | 2/2 | 2/2 | 1/1 |
| swap-layouts-interop ([64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2)) | pack-before-set-size | 2/2 | 2/2 | 1/1 |
| swap-layouts-interop ([64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2)) | swab-min-size | 2/2 | 2/2 | 1/1 |
| use-nidmasks-for-ipv6-nids ([56184](https://review.whamcloud.com/c/fs/lustre-release/+/56184/10)) | fix-nidlist-leak-in-nodemap-pa | 2/2 | 2/2 | 1/1 |
| **total** | | **64/76 (84%)** | **72/78 (92%)** | **34/39 (87%)** |

$/review: ordinary $0.50, lenses at high $2.46, strict $1.48.

### Cost variants on the verification round's cases

"Strict" is the verification round's column (two runs); "+ D1 + D2"
pools the 28-change run and a run of the other four cases.

| case (change) | bug | strict, lenses medium | + D1 | + D2 | + D1 + D2 |
|---|---|---|---|---|---|
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-error-handling-in-dma-mapp | 2/2 | 1/1 | 0/1 | 1/1 |
| gds-support-for-kfilnd ([61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2)) | fix-sg-table-free-after-orig-n | 2/2 | 1/1 | 1/1 | 1/1 |
| lnet-mr-forwarding ([63582](https://review.whamcloud.com/c/fs/lustre-release/+/63582/5)) | test237-238-no-interop-version-gate | 2/2 | 1/1 | 1/1 | 0/1 |
| lnetctl-import ([65055](https://review.whamcloud.com/c/fs/lustre-release/+/65055/8)) | import-fgets-overflow | 0/2 | 0/1 | 0/1 | 0/1 |
| memcg-balance-dirty ([59223](https://review.whamcloud.com/c/fs/lustre-release/+/59223/11)) | unlocked-tree-dump | 0/2 | 0/1 | 1/1 | 1/1 |
| mgc-new-registration ([59212](https://review.whamcloud.com/c/fs/lustre-release/+/59212/8)) | legacy-nid-oob | 2/2 | 1/1 | 1/1 | 1/1 |
| nodemap-ban-default ([60074](https://review.whamcloud.com/c/fs/lustre-release/+/60074/15)) | ban-search-unlocked | 0/2 | 0/1 | 0/1 | 0/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | cpu-index-math | 2/2 | 1/1 | 1/1 | 1/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | delayed-work-not-stopped | 2/2 | 1/1 | 1/1 | 1/1 |
| pm-qos-latency ([57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12)) | svc-stats-unprotected | 0/2 | 0/1 | 1/1 | 0/1 |
| relate-qmt-lvbo-free-wq-and ([56661](https://review.whamcloud.com/c/fs/lustre-release/+/56661/3)) | fix-the-order-of-freeing-qmt-l | 1/2 | 1/1 | 1/1 | 1/1 |
| **total** | | **13/22 (59%)** | **7/11 (64%)** | **8/11 (73%)** | **7/11 (64%)** |

$/review: strict $1.67, + D1 $1.33, + D2 $1.51, + D1 + D2 $1.18.

## Appendix: the known bugs

| case | bug | change | what the bug is | known from |
|---|---|---|---|---|
| swap-layouts-interop | swab-min-size | [64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2) | swabber_dumper_helper() sets len = rmf_size for every non-array field before calling the field's rmf_swab_len swabber. The new RMF_F_MINIMUM_SIZE field RMF_SWAP_LAYOUTS therefore always reaches lustre_swab_swap_layouts() with the 8-byte minimum, never the real buffer length, so msl_dv1/msl_dv2 ... | fixed by 5562816df2 "LU-5896 mdt: do not overwrite the actual buffer size" |
| swap-layouts-interop | pack-before-set-size | [64757](https://review.whamcloud.com/c/fs/lustre-release/+/64757/2) | mdc_ioc_swap_layouts() calls req_capsule_set_size(RMF_SWAP_LAYOUTS, 24) after mdc_prep_elc_req() has already packed the request. set_size does not resize or move packed buffers, so copying the 24-byte struct into the 8-byte RMF_SWAP_LAYOUTS buffer overwrites the start of the following ... | fixed by dac2ec9d69 "LU-20117 mdc: fix RMF_SWAP_LAYOUTS packing" |
| ping-evictor-rework | export-timer-alloc | [58620](https://review.whamcloud.com/c/fs/lustre-release/+/58620/3) | obd_export_timed_init() checks the void ** argument instead of the pointer OBD_ALLOC() wrote through it, so it returns 0 even when the allocation failed. Its callers (ptlrpc_update_export_timer(), rev_import_init()) then call obd_export_timed_add() with a NULL node while holding obd_dev_lock and ... | fixed by 3eeec9e6b2 "LU-17000 obdclass: handle export timer allocation failure" |
| lmv-qos-exclude | exclude-list-uaf | [60794](https://review.whamcloud.com/c/fs/lustre-release/+/60794/9) | lmv_qos_exclude() walks lmv_qos_exclude_list without taking lmv_lock, while the qos_exclude_patterns/qos_exclude_prefixes set_param handlers free entries under lmv_lock. A mkdir racing 'lctl set_param qos_exclude_patterns' dereferences a freed entry (use-after-free, client panic). | fixed by 3d9e884fed "LU-20495 lmv: fix use-after-free in qos exclude list" |
| mgc-new-registration | legacy-nid-oob | [59212](https://review.whamcloud.com/c/fs/lustre-release/+/59212/8) | mgc_target_register() takes the new registration path whenever the MGS export has OBD_CONNECT_MGS_NIDLIST, even when the target has only legacy NIDs and server_lsi2mti() never allocated/filled mti_nidlist (only done for LDD_F_LARGE_NID). It then reads NIDLIST_SIZE() bytes from mti_nidlist: a ... | fixed by bc07e00b07 "LU-18986 mgc: fix OOB read registering targets with legacy NIDs" |
| shrinker-compat | shrinker-alloc-leak | [60352](https://review.whamcloud.com/c/fs/lustre-release/+/60352/7) | ll_shrinker_alloc() allocates the ll_shrinker wrapper first and leaks it when shrinker_alloc()/registration fails; it also dereferences the result of kvasprintf_const() without a NULL check and frees it with kfree() instead of kfree_const(). | fixed by 2bb6a2a9f2 "LU-17000 compat: Fix resource leak in ll_shrinker_alloc" |
| lnet-recreate-peers | sleep-in-spinlock | [61596](https://review.whamcloud.com/c/fs/lustre-release/+/61596/17) | LNetPeerDiscovered() now calls lnet_peerni_by_nid_locked(), which can take a mutex and sleep, but it is called from import_select_connection() inside a spinlock-protected loop: sleeping in atomic context. | fixed by f50fb2ca3d "LU-19928 ptlrpc: don't sleep inside spinlock" |
| o2iblnd-rtnl | rtnl-in-rcu | [63038](https://review.whamcloud.com/c/fs/lustre-release/+/63038/5) | The change takes rtnl_lock() (a sleeping mutex) inside an RCU read-side critical section; only a reference on the net device should be taken under RCU and the locked work done outside. | fixed by e3a0dfd046 "LU-20102 o2iblnd: do not call rtnl_lock() in RCU read section" |
| o2iblnd-rtnl | rtnl-under-global-lock | [63038](https://review.whamcloud.com/c/fs/lustre-release/+/63038/5) | kiblnd_startup() queries the net device link status (lnet_get_link_status(), which takes rtnl_lock(), a mutex) inside the kib_global_lock write section with interrupts off: sleeping in atomic context, reported as 'scheduling while atomic' and able to stop a node booting when rtnl_mutex is contended. | fixed by fd76c0fe6c "LU-20580 o2iblnd: don't sleep in atomic in kiblnd_startup()" |
| quota-nodemap | nodemap-put-early | [59297](https://review.whamcloud.com/c/fs/lustre-release/+/59297/5) | ofd_quotactl() drops its nodemap reference with nodemap_putref() before lquotactl_slv() has finished using the nodemap, so the nodemap can be freed while still in use. | fixed by 66efbc67b5 "LU-20485 ofd: hold nodemap ref while using the nodemap object" |
| mgc-nid-notify | unchecked-reply | [59868](https://review.whamcloud.com/c/fs/lustre-release/+/59868/11) | mgc_nid_notify_interpret() reads the reply message without checking rc or that a valid reply exists, so a failed or timed-out RPC dereferences a missing reply. | fixed by d8dc4bdc07 "LU-19389 mgc: check reply in mgc_nid_notify_interpret()" |
| memcg-balance-dirty | unlocked-tree-dump | [59223](https://review.whamcloud.com/c/fs/lustre-release/+/59223/11) | The new osc_extent_tree_dump() call in osc_cache_writeback_range() (dirty-exceeded path) walks the extent rbtree without osc_object_lock(); next_extent() asserts the extent is still in the tree, so a concurrent removal panics. | fixed by dd5d669438 "LU-20078 osc: call osc_extent_tree_dump() without object lock held" |
| socklnd-restrict-ip | waitqueue-before-init | [59673](https://review.whamcloud.com/c/fs/lustre-release/+/59673/15) | lnet_acceptor_add_socket() installs lnet_acceptor_ready() as the data_ready callback on listening sockets during LND startup, before the acceptor thread initialises the wait queue it wakes (init_waitqueue_head runs in the thread). A connection completing in that window wakes an uninitialised ... | fixed by 26e01e5a10 "LU-20132 lnet: statically initialize acceptor wait queue" |
| socklnd-restrict-ip | callback-not-restored | [59673](https://review.whamcloud.com/c/fs/lustre-release/+/59673/15) | The listening socket is released without restoring its original kernel callbacks, so a late data_ready interrupt during teardown calls lnet_acceptor_ready() on freed state and crashes in __wake_up_common(). | fixed by 9bff0197f8 "LU-19528 lnet: restore kernel cb before listener sk free" |
| lassert-side-effects | state-change-in-assert | [56115](https://review.whamcloud.com/c/fs/lustre-release/+/56115/5) | Converting 'if (...) LBUG()' to LASSERT/LASSERTF moved state-changing calls into the assertion condition: atomic_dec_and_test() in dt_los_put() and test_and_set_bit()/test_and_clear_bit() on lut_client_bitmap. With --disable-libcfs-assert LASSERT does not evaluate its condition, so the reference ... | fixed by ffe1627adc "LU-20559 misc: keep updates out of assertion conditions" |
| osc-kswapd-deadlock | oe-memalloc-never-set-dead-check | [64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2) | The patch adds `\|\| ext->oe_memalloc` checks to osc_extent_find(), but on master nothing sets oe_memalloc: its only setter lived in osc_flush_async_page(), removed with ll_writepage() by LU-18675 (6e83069d98), an ancestor of this commit. Both checks are therefore always false, the 'fix' is dead ... | confirmed by review, not fixed |
| osc-kswapd-deadlock | comment-cites-removed-osc-flush-async-page | [64397](https://review.whamcloud.com/c/fs/lustre-release/+/64397/2) | The comments added in osc_extent_find() describe a kswapd -> osc_flush_async_page() -> cl_page_prep() path that was deleted by LU-18675 before this patch landed, so they document behaviour master cannot have and will mislead readers. Part of the same no-op-on-master problem. | confirmed by review, not fixed |
| lnet-mr-forwarding | test237-238-no-interop-version-gate | [63582](https://review.whamcloud.com/c/fs/lustre-release/+/63582/5) | The new sanity-lnet tests 237/238 need the router node to run this patch's code (the fix and the new fail_loc 0xe004), but they have no version check. In interop runs where the router is a b2_15/b2_16 server, test_237 fails every time ('Expected N-N >= 50'), and test_238 fails too, because old ... | confirmed by review, not fixed |
| nodemap-ban-default | ban-search-unlocked | [60074](https://review.whamcloud.com/c/fs/lustre-release/+/60074/15) | nodemap_add_range_helper() calls ban_range_search() on the ban range tree without taking nmc_ban_range_tree_lock, so the search can race with ban-range insertion or deletion. | fixed by 5a3722af08 "LU-19157 nodemap: read semaphore for ban_range_search" |
| mount-large-nids | append-param-nul | [57560](https://review.whamcloud.com/c/fs/lustre-release/+/57560/10) | append_param() moves the tail of the parameter buffer when replacing a parameter, but when there is no tail (the parameter is the last one) the move skips the NUL terminator, leaving the buffer unterminated. | fixed by 53ae11aa46 "LU-18896 utils: don't miss null-terminator in append_param" |
| fsinfo-long-nids | root-path-want | [59421](https://review.whamcloud.com/c/fs/lustre-release/+/59421/5) | get_root_path() calls the new get_root_path_fast() even when WANT_INDEX or WANT_NID is requested; the fast path cannot supply those, so the condition choosing it is wrong. | fixed by 50a2634695 "LU-19050 utils: fix get_root_path WANT condition" |
| lnetctl-import | import-fgets-overflow | [65055](https://review.whamcloud.com/c/fs/lustre-release/+/65055/8) | In lnetctl import --old-api, the fgets() loop reading the YAML into a heap buffer advances buf before recomputing strlen(), so 'len -= strlen(buf)' subtracts zero; len stays at the allocation size while buf walks forward, and fgets() is told it has more room than remains: a heap buffer overflow. | fixed by 20c2a77d69 "LU-20245 lnetctl: Fix buffer overflow in import --old-api" |
| lu-env-add-errors | osp-thread-args-leak | [57272](https://review.whamcloud.com/c/fs/lustre-release/+/57272/12) | After the change the caller frees the thread arguments only when the thread cannot be created, but the osp sync thread does not free them on all of its own exit paths, so they leak. | fixed by e3533028de "LU-18506 osp: ensure args are freed" |
| fallocate-no-restart | restart-offset-units | [59158](https://review.whamcloud.com/c/fs/lustre-release/+/59158/4) | osd_fallocate_preallocate() advances *start, the byte offset where the fallocate resumes after an -EAGAIN restart, by the block count ldiskfs_map_blocks() returned, mixing blocks and bytes, so the restarted pass re-maps a range already mapped (and can pass a start beyond end). | fixed by b0ab8999c6 "LU-20629 osd: fix fallocate restart offset units" |
| dne-migrate-layout | fld-range-uninit | [58585](https://review.whamcloud.com/c/fs/lustre-release/+/58585/8) | lod_dir_layout_check() calls lod_fld_lookup() with an fld range whose type was never initialised (it should be LU_SEQ_RANGE_MDT), passing stack garbage into fld_range_set_type(). | fixed by 5ad307db23 "LU-20428 lod: init fld range type before lod_fld_lookup" |
| nodemap-innermost-range | orphaned-subranges | [59890](https://review.whamcloud.com/c/fs/lustre-release/+/59890/10) | Sub-nodemap NID ranges are stored in the parent range's embedded interval tree (rn_subtree) and point back to it through rn_tree; range_delete_generic() frees a range without handling its subtree, so deleting a parent range leaves sub-ranges linked into freed memory with rn_tree dangling. | fixed by 7fae11ad4c "LU-20524 nodemap: do not orphan sub-ranges when deleting a range" |
| fileset-llog-compat | nodemap-del-null | [60006](https://review.whamcloud.com/c/fs/lustre-release/+/60006/10) | nodemap_del() can be called with out_clean_llog_fileset == NULL but dereferences it without a NULL check. | fixed by 0cf0f8d9e9 "LU-20272 nodemap: fix NULL deref in nodemap_del()" |
| resource-id-check | getxattr-check-order | [59208](https://review.whamcloud.com/c/fs/lustre-release/+/59208/6) | mdt_getxattr() calls mdt_check_resource_ids() after mdt_init_ucred(), so requests that will be rejected still acquire identity cache entries, which are then not cleaned up correctly; the check must come first. | fixed by 21bd3cd847 "LU-18756 mdt: check resource IDs before init_ucred in getxattr" |
| osc-lu-device | device-free-null-obd | [58818](https://review.whamcloud.com/c/fs/lustre-release/+/58818/8) | osc_device_free() dereferences the obd pointer without a NULL check; when device registration fails it is NULL and the cleanup path crashes. | fixed by 5537bbc0b6 "LU-20160 mdc,osc: Do not crash on device registration failure" |
| llog-subcommands | remove-runs-check | [56040](https://review.whamcloud.com/c/fs/lustre-release/+/56040/8) | The new llog subcommand table assigns jt_llog_check to the "remove" entry instead of jt_llog_remove, so 'lctl llog remove' silently runs llog_check. | fixed by 6681a33af5 "LU-20274 utils: fix wrong func ptr for "lctl llog remove"" |
| nodemap-fileset-inherit | fileset-parent-check | [55614](https://review.whamcloud.com/c/fs/lustre-release/+/55614/24) | When a parent nodemap has only alternate filesets, a child dynamic nodemap can set any primary fileset with 'lctl nodemap_set_fileset', even outside the parent's namespace: check_fileset_add_vs_parent() is not applied on that path. | fixed by b32f37a0f6 "LU-19426 nodemap: check parent when setting fileset" |
| changelog-enc-names | ns-store-null-target | [55916](https://review.whamcloud.com/c/fs/lustre-release/+/55916/8) | mdd_changelog_ns_store() uses the target object's FID unconditionally, but for lfs rm_entry the target is not defined, so mdd_object_fid() dereferences an invalid object and the MDS crashes. | fixed by c4cbeeca8c "LU-19501 mdd: check target in mdd_changelog_ns_store" |
| mgs-buffer-checks | skip-ipv6-uninit | [64953](https://review.whamcloud.com/c/fs/lustre-release/+/64953/4) | tgt_import_update() leaves nfd_skip_ipv6 (and nfd_has_ipv6) uninitialised on the stack; server_nid2radix() reads nfd_skip_ipv6 and may drop every large NID, so the target reports it cannot get its local NIDs. | fixed by 2d8050bbd4 "LU-20584 target: init nfd_skip_ipv6 in tgt_import_update()" |
| pm-qos-latency | delayed-work-not-stopped | [57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12) | The per-CPU cpu_latency_work() delayed work is not cancelled and waited for when the connection is finalised, so the timer or work can run after the cpu_latency_qos array and its PM-QoS request are freed and the module unloaded (later fixes added cancel_delayed_work(), then the _sync wait). | fixed by 80708743dc "LU-18788 ptlrpc: cancel PM-QoS delayed work" |
| pm-qos-latency | svc-stats-unprotected | [57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12) | kick_cpu_latency() reads obd->obd_svc_stats to choose the low-latency period without protection against concurrent teardown of the stats. | fixed by fb8423bfd9 "LU-19091 ptlrpc: protect internal access to obd->obd_svc_stats" |
| pm-qos-latency | cpu-index-math | [57039](https://review.whamcloud.com/c/fs/lustre-release/+/57039/12) | cpu_latency_work() computes the CPU number as the pointer difference divided by sizeof(struct cpu_latency_qos), but subtracting two typed pointers already gives the element index, so the CPU number is wrong. | fixed by 7cfd807096 "LU-6142 ptlrpc: fix cpu index computation in cpu_latency_work()" |
| lnd-module-loading | err-ptr-positive | [57946](https://review.whamcloud.com/c/fs/lustre-release/+/57946/3) | lnet_load_lnd() passes request_module()'s return value to ERR_PTR(), but request_module() can return a positive modprobe status, which ERR_PTR() turns into a bogus non-error pointer. | fixed by 969a680544 "LU-18893 lnet: Use negative errno for ERR_PTR" |
| grant-rpc-failure | o-grant-not-reset | [56547](https://review.whamcloud.com/c/fs/lustre-release/+/56547/5) | tgt_grant_dealloc() leaves oa->o_grant set when the server decides not to return grants, so a stale grant value goes back to the client. | fixed by a91c46a7da "LU-19016 target: reset o_grant if no grant is returned" |
| move-include-to-top-level | fix-shrinker-debug-init-fini-i | [60989](https://review.whamcloud.com/c/fs/lustre-release/+/60989/9) | In include/lustre_compat/linux/shrinker.h, the ORIGINAL commit moved the shrinker_debugfs_init/fini declarations from linux-misc.h (guarded by #ifndef CONFIG_SHRINKER_DEBUG) into the branch guarded by `!defined(CONFIG_SHRINKER_DEBUG) \|\| defined(HAVE_REGISTER_SHRINKER_FORMAT_NAMED)`. The inline ... | fixed by 4dae04cfc5 "LU-18687 compat: fix shrinker_debug_init/fini #ifdef" |
| refresh-context-vs-lbug-upon | refresh-context-vs-lbug-upon-g | [62467](https://review.whamcloud.com/c/fs/lustre-release/+/62467/5) | In the ORIGINAL change to lustre/ptlrpc/sec.c:sptlrpc_req_refresh_ctx(), the new refresh branch also fires when `rc2 == -ENODATA`, the return value of sptlrpc_req_replace_dead_ctx(), and not only when gc_gss_err is GSS_S_NO_CONTEXT. When the ctx is DEAD and cannot be replaced, the code clears ... | fixed by d22e461c41 "LU-19580 gss: refresh context vs LBUG upon gss error for LDLM" |
| relate-qmt-lvbo-free-wq-and | fix-the-order-of-freeing-qmt-l | [56661](https://review.whamcloud.com/c/fs/lustre-release/+/56661/3) | In qmt_dev.c:qmt_device_fini the ORIGINAL change moves the workqueue into struct qmt_device but keeps the destroy_workqueue(qmt->qmt_lvbo_free_wq) and NULL assignment at the very start of shutdown, before qmt_pool_fini() and the namespace/lock cleanup. Those later steps release ldlm resources ... | fixed by a0e766235d "LU-18024 quota: fix the order of freeing qmt_lvbo_free_wq" |
| lfs-find-ost-supports-index | fix-lfs-find-m-mdtidx-negation | [55902](https://review.whamcloud.com/c/fs/lustre-release/+/55902/5) | In lustre/utils/lfs.c:lfs_find, the ORIGINAL commit merged the 'm', 'i' and 'O' option handling into one case block and dropped the 'param.fp_exclude_mdt = !!neg_opt;' assignment that previously lived in the 'if (c == "m")' branch; only fp_exclude_obd is now set. check_mdt_match() still reads ... | fixed by e87c54a7e7 "LU-20433 utils: fix 'lfs find ! -m MDTIDX' negation" |
| support-pci-p2p-dma-mapping | fix-efa-p2p-configure-guard | [66985](https://review.whamcloud.com/c/fs/lustre-release/+/66985/26) | In lnet/klnds/efalnd/efalnd.c:kefalnd_startup, the ORIGINAL commit guards the ni->ni_p2pdma initialisation with IN_KERNEL_HAVE_OFED_IB_DMA_PCI_P2P_DMA_SUPPORTED, a macro that is only emitted by the o2ib configure check (LN_CONFIG_O2IB). In a --disable-o2ib --enable-efa build the macro is never ... | fixed by eabffde030 "LU-20438 lnd: fix EFA P2P configure guard" |
| use-nidmasks-for-ipv6-nids | fix-nidlist-leak-in-nodemap-pa | [56184](https://review.whamcloud.com/c/fs/lustre-release/+/56184/10) | In nodemap_parse_range() in lustre/ptlrpc/nodemap_handler.c, the ORIGINAL change parses range_str with cfs_parse_nidlist(), which allocates the nidlist on success. If cfs_nidmask_get_length() returns 0 (e.g. multiple nidmasks or zero prefix length), the code does GOTO(out, rc = -EINVAL) without ... | fixed by 1dad0bda39 "LU-20431 nodemap: fix nidlist leak in nodemap_parse_range()" |
| return-error-for-verify-when | fix-resync-with-nosync-stale-c | [61751](https://review.whamcloud.com/c/fs/lustre-release/+/61751/8) | The ORIGINAL change makes `lfs mirror verify` count every LCME_FL_STALE component as an error (print_stale_mirrors() in lustre/utils/lfs.c returns stale_count, and lfs_mirror_verify_file() sets rc2 = -ESTALE), without excluding components that are both stale and LCME_FL_NOSYNC. Nosync+stale ... | fixed by 34a17ba8ef "LU-19527 lfs: fix resync with nosync+stale components" |
| enforce-file-quota-for-root | fix-silent-data-loss-for-root | [60213](https://review.whamcloud.com/c/fs/lustre-release/+/60213/9) | In osd_declare_inode_qid (osd-ldiskfs/osd_quota.c) and osd_declare_quota (osd-zfs/osd_quota.c), the ORIGINAL change makes root-project-quota (QUOTA_FL_ROOT_PRJQUOTA) override the force/ignore-quota flag for block quota, so force = th->th_ignore_quota applies and OSD_QID_FORCE is effectively ... | fixed by e1af85a420 "LU-19791 quota: fix silent data loss for root project" |
| gds-support-for-kfilnd | fix-sg-table-free-after-orig-n | [61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2) | In lnet/klnds/kfilnd/kfilnd_tn.c:kfilnd_tn_set_sgl_buf(), the ORIGINAL change allocates the sg_table with sg_alloc_table(max_nkiov) and then overwrites tn->tn_sgt.orig_nents with sg_count (the number of populated segments) before DMA mapping. kfilnd_tn_free() later calls sg_free_table() on that ... | fixed by 2705d3bfef "LU-19796 kfilnd: fix sg_table free after orig_nents overwrite" |
| gds-support-for-kfilnd | fix-error-handling-in-dma-mapp | [61824](https://review.whamcloud.com/c/fs/lustre-release/+/61824/2) | In kfilnd_tn.c:kfilnd_tn_set_sgl_buf() (added by the ORIGINAL commit), the return value of lnet_rdma_map_sg_attrs()/dma_map_sgtable() is not checked before setting tn->tn_sgt_mapped = true and populating tn_num_iovec from tn_sgt.nents. If DMA mapping fails, the function proceeds as if mapped, so ... | fixed by 3d6df6b288 "LU-19496 kfilnd: Fix error handling in DMA mapping" |
| replace-client-lock-lru-with | fix-lfru-priv-ratio-limit-over | [60162](https://review.whamcloud.com/c/fs/lustre-release/+/60162/28) | In the ORIGINAL change, `ns_lfru_priv_ratio_limit_256` is declared `__s8` in struct ldlm_namespace (lustre_dlm.h) but is assigned values up to 255 (default 30*256/100=76, but sysfs `lru_priv_ratio_limit_store` in ldlm_resource.c accepts any int and stores `tmp * 256 / 100` unchecked). Values ... | fixed by f1e4a9fcfe "LU-11509 ldlm: fix lfru priv_ratio_limit overflow issue" |
| specify-total-count-for-mirr | fix-issues-with-llapi-get-lmm | [55867](https://review.whamcloud.com/c/fs/lustre-release/+/55867/11) | The ORIGINAL commit adds llapi_get_lmm_from_path() in liblustreapi.c and its use in lfs.c:lfs_setstripe_internal (-N=TOTAL mode). In llapi_get_lmm_from_path, lmmlen is declared size_t so the `lmmlen < 0` check from get_mds_md_size() is never true (negative error wraps to a huge value), leading ... | fixed by bed10bb933 "LU-16446 utils: fix issues with llapi_get_lmm_from_path()" |
| lmv-disconnect-uaf | upcall-clear-after-disconnect | [64317](https://review.whamcloud.com/c/fs/lustre-release/+/64317/23) | lmv_disconnect_mdc() and the lmv_check_connect() error path clear tgt->ltd_exp->exp_obd->obd_upcall (onu_owner/onu_upcall) after obd_disconnect(tgt->ltd_exp). obd_disconnect() drops the export reference, so the export can already be freed: use-after-free (sanity-sec test_27e crash). The upcall must ... | fixed by deacfcca45 "LU-20700 lmv: fix use-after-free in lmv_disconnect_mdc()" |
| ec-parity-km-zero | ec-k-zero-divide | [62906](https://review.whamcloud.com/c/fs/lustre-release/+/62906/42) | lod_qos_prep_create() sizes a parity component with ec_split_stripes(data stripe count, llc_dstripe_count). llc_dstripe_count (k) comes unchecked from the client layout; lod_verify_striping() bounds only k+m. k == 0 divides by zero in ec_split_stripes() ('total % suggested') on the MDS. | fixed by fed89412bf "LU-19548 lfs: mirror extend support for EC" |
| ec-parity-km-zero | ec-m-zero-lassert | [62906](https://review.whamcloud.com/c/fs/lustre-release/+/62906/42) | llc_cstripe_count (m) is also copied unchecked from the client layout. m == 0 makes the parity component's llc_stripe_count = (n0 + n1) * 0 = 0, which trips LASSERT(lod_comp->llc_stripe_count) on the MDS. lod_verify_striping() must reject k == 0 or m == 0. | fixed by fed89412bf "LU-19548 lfs: mirror extend support for EC" |
| gssiam-modprobe-loop | wireflavor2policy-loop | [65062](https://review.whamcloud.com/c/fs/lustre-release/+/65062/78) | sptlrpc_wireflavor2policy() now retries request_module("ptlrpc_gss") in while(1) until policies[SPTLRPC_POLICY_GSSIAM] is set, but nothing registers a GSSIAM policy. Any RPC with a GSSIAM wire flavor pins the calling thread (a ptlrpc service thread for an incoming request) in a loop that runs ... | fixed by 201f039a1b "LU-20984 ptlrpc: bound ptlrpc_gss load in policy lookup" |
| p2pdma-per-stripe | p2p-decided-per-chunk | [66987](https://review.whamcloud.com/c/fs/lustre-release/+/66987/30) | P2P DMA capability is evaluated in osc_io_init()/mdc_io_init(), but lov_io_rw_iter_init() limits each cl_io_loop() pass to one stripe chunk and creates the sub-IO only in that pass. A direct I/O whose first stripe supports P2P pins and sends P2P pages before a later non-capable stripe sets ... | fixed by 3ace9a1d38 "LU-20438 clio: eagerly evaluate P2P DMA capability via layout" |
| osc-encrypt-unused-index | fallback-macro-drops-index | [69505](https://review.whamcloud.com/c/fs/lustre-release/+/69505/9) | Without embedded llcrypt (CONFIG_LL_ENCRYPTION unset) the fallback osc_encrypt_pagecache_blocks() macro drops its inode, index and directio arguments, so the new 'index' variable in osc_brw_prep_request() is set but never used and a clang -Werror build fails (-Wunused-but-set-variable). | fixed by 6f724612d9 "LU-20896 osc: fix unused variable in non-llcrypt build" |
| debug-vlen-always-inline | va-list-in-static-inline | [69231](https://review.whamcloud.com/c/fs/lustre-release/+/69231/10) | lustre_debug_vlen() is a static inline in include/trace/events/lustre_debug.h that uses va_copy()/va_list. On older kernels built without CONFIG_OPTIMIZE_INLINING (el8 4.18, every aarch64 build) 'inline' means always_inline, and gcc refuses: 'function can never be inlined because it uses variable ... | fixed by 657e3f656c "LU-20837 libcfs: move lustre_debug_vlen() out of line" |
| ofd-failure-domain-race | failure-domain-unlocked-read | [65184](https://review.whamcloud.com/c/fs/lustre-release/+/65184/18) | ofd_statfs() copies ofd->ofd_failure_domain into os_failure_domain without ofd_flags_lock, while failure_domain_store() writes it under that lock (Coverity CID 506107 data race). | fixed by 76b710dde6 "LU-17000 ofd: Fix data race when accessing ofd_failure_domain" |
| o2iblnd-fastreg-gaps | mid-page-split-counted-as-gap | [66689](https://review.whamcloud.com/c/fs/lustre-release/+/66689/3) | kiblnd_setup_rd_kiov() flags tx_gaps when a non-first kiov fragment starts mid-page or a non-last one ends mid-page. ib_sg_to_pages() coalesces fragments that meet inside a page (end of one == start of the next), so a contiguous payload split mid-page is not a gap; the check needlessly requires ... | fixed by 073c739a0d "LU-20886 o2iblnd: use the FastReg key for immediate payload" |
| llog-skip-plain-client | skip-plain-server-only | [60240](https://review.whamcloud.com/c/fs/lustre-release/+/60240/27) | The patch moves the LLOG_SKIP_PLAIN -> 0 conversion in llog_cat_process_cb() inside #ifdef CONFIG_LUSTRE_FS_SERVER. On a client-only build a plain-llog skip (chlg_read_cat_process_cb() via llog_is_plain_skipable()) stops the whole catalog walk, so a client changelog reader (lustre_rsync) silently ... | fixed by af770d08d0 "LU-16489 obdclass: handle LLOG_SKIP_PLAIN on client builds" |
