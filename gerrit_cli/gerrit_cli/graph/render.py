"""HTML template loading and file output.

Loads the three template files (`graph.html` shell, `graph.css`, and
`graph.js`) from the package `templates/` directory and inlines them
into a single self-contained document. `__GRAPH_DATA__` is the only
remaining placeholder, substituted by `generate_html()` with the
JSON graph payload."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_template() -> str:
    """Load graph.html and inline graph.css + graph.js into a single
    self-contained HTML document. The returned string still contains
    the __GRAPH_DATA__ marker that generate_html() fills in."""
    html = (_TEMPLATE_DIR / "graph.html").read_text()
    css = (_TEMPLATE_DIR / "graph.css").read_text()
    js = (_TEMPLATE_DIR / "graph.js").read_text()
    return html.replace("/*__CSS__*/", css).replace("/*__JS__*/", js)


def generate_html(graph_data: dict[str, Any]) -> str:
    """Generate a self-contained interactive HTML visualization."""
    data_json = json.dumps(graph_data)
    return _load_template().replace("__GRAPH_DATA__", data_json)


def save_and_open(html_content: str, output_path: str | None = None) -> str:
    """Save HTML to a file. Returns the path."""
    if output_path:
        path = output_path
    else:
        fd, path = tempfile.mkstemp(suffix=".html", prefix="gerrit-graph-")
        os.close(fd)
    with open(path, "w") as f:
        f.write(html_content)
    return path
