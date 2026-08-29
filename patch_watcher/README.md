# Patch Watcher

Patch Watcher is a tool for watching patches over time and handling their
state and changes. It is intended to provide a shared foundation for patch
tracking, review-state transitions, CI and test updates, and an operator-
friendly website.

The project will keep a durable history of patch events and make the current
state easy to inspect, while allowing automated watchers and human operators
to act on changes.

## Run it

The current skeleton is dependency-free and stores watched patches in memory.
Start it with:

```bash
python3 app.py
```

Then open <http://127.0.0.1:8080>. Add and remove buttons are provided on the
page. URLs must be HTTPS changes hosted at `review.whamcloud.com/c/`; titles
are optional and default to the Gerrit change number. Review status and last
updated are displayed as placeholders for the future Gerrit watcher.

Run the tests with `python3 -m unittest -v`.
