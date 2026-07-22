"""Shared helpers used by Confluence command modules."""

import re
from typing import Any
from urllib.parse import urlparse

import click

from ..client import ConfluenceClient, html_to_text
from ..config import ConfluenceConfig, load_config
from ..envelope import error_response, format_json
from ..errors import ConfluenceToolError

# A Confluence page URL carries the numeric id as /pages/<id>/... (Cloud & Server)
_PAGE_ID_IN_URL = re.compile(r"/pages/(\d+)")


def output_result(envelope: dict[str, Any], pretty: bool) -> None:
    """Output result to stdout, honoring the --envelope flag from context."""
    ctx = click.get_current_context(silent=True)
    full_envelope = ctx.obj.get("envelope", False) if ctx and ctx.obj else False
    click.echo(format_json(envelope, pretty=pretty, full_envelope=full_envelope))


def handle_error(error: ConfluenceToolError, command: str, pretty: bool) -> int:
    """Output an error envelope and return the process exit code."""
    envelope = error_response(error, command)
    output_result(envelope, pretty)
    return error.exit_code


def extract_page_id(id_or_url: str) -> str:
    """Return a bare page id from a page id or a Confluence page URL."""
    id_or_url = id_or_url.strip()
    if id_or_url.isdigit():
        return id_or_url
    parsed = urlparse(id_or_url)
    if parsed.scheme:
        m = _PAGE_ID_IN_URL.search(parsed.path)
        if m:
            return m.group(1)
    return id_or_url  # let the API report the error if it's not resolvable


def get_client(ctx: click.Context) -> ConfluenceClient:
    """Build a ConfluenceClient for the selected instance."""
    config = load_config(
        config_path=ctx.obj.get("config_path"),
        server_override=ctx.obj.get("server_override"),
        instance=ctx.obj.get("instance"),
    )
    ctx.obj["config"] = config
    return ConfluenceClient(config, debug=ctx.obj.get("debug", False))


def normalize_result(item: dict[str, Any], server: str) -> dict[str, Any]:
    """Normalize a search-result / content item to a compact shape."""
    links = item.get("_links", {}) or {}
    space = item.get("space", {}) or {}
    webui = links.get("webui", "")
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "title": item.get("title"),
        "space": space.get("key") or space.get("name"),
        "url": f"{server}{webui}" if webui else None,
    }


def normalize_page(item: dict[str, Any], server: str) -> dict[str, Any]:
    """Normalize a full page, converting the HTML body to plain text."""
    data = normalize_result(item, server)
    version = item.get("version", {}) or {}
    body = item.get("body", {}) or {}
    view = (body.get("view") or body.get("storage") or {}).get("value")
    data["version"] = version.get("number")
    data["text"] = html_to_text(view)
    return data
