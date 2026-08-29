# Patch Watcher

Patch Watcher is a tool for watching patches over time and handling their
state and changes. It is intended to provide a shared foundation for patch
tracking, review-state transitions, CI and test updates, and an operator-
friendly website.

The project will keep a durable history of patch events and make the current
state easy to inspect, while allowing automated watchers and human operators
to act on changes.

This directory is intentionally a starting point. The command-line interface,
storage model, watchers, and web application will be designed as the project
takes shape.
