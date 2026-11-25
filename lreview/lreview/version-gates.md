# Version gates in tests

The facts above are computed; do not redo them.  These rules override any
other rule on version gates.

- A gate ABOVE the tree's version is a `(defect)`: the patch skips its own
  test.  One at or below it is right.  Never say that it lets in builds
  without the fix, and never ask for the next tag or for a confirmation.
- A patch MISSING on another branch is a `(defect)` if the test would fail
  there.
- Report a NOTE about code this commit changes as `(minor)` with
  `unresolved: false`, once for each version.  It needs no refresh.
- A missing server gate, one on the wrong side, and a whole release without
  the fix that passes the gate are still defects.
