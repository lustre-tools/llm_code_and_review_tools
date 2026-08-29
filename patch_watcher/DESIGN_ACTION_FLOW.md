# Patch Watcher Action Flow

This document describes the first step toward Patch Shepherd-style patch
handling. It is a design plan only: the controls do not execute automated
actions yet.

## Per-patch controls

Each watched Gerrit patch gets its own action settings beside its status:

- **Action:** a dropdown whose initial option is **Do nothing**.
- **Handle build errors:** a checkbox.
- **Handle test errors:** a checkbox.

The settings belong to the individual patch, not to the page globally. They
must remain visible while the patch is refreshed so an operator can see both
the current Gerrit state and the selected handling policy.

## Defaults

Newly added patches use safe defaults:

- Action: **Do nothing**
- Handle build errors: unchecked
- Handle test errors: unchecked

Defaults should be configurable later, but changing them must never silently
enable an automated action for existing patches.

## Planned flow

1. Refresh the patch and record its Gerrit, review, and CI state.
2. Present the state and the patch's selected handling settings.
3. If a checked error category matches, record a pending recommendation.
4. In a later phase, add an explicit operator-approved action for that
   recommendation (for example, investigate or request a retest).

Until that later phase is designed and approved, Patch Watcher remains
read-only: checking a box records intent but does not post comments, request
retests, alter Gerrit state, or send other external actions.
