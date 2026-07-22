"""Machine-readable API description for the Confluence tool."""

from llm_tool_common import Argument, Command, ToolDescription

from . import __version__


def get_tool_description() -> ToolDescription:
    """Return the complete Confluence tool API description."""
    return ToolDescription(
        name="confluence",
        version=__version__,
        description=(
            "Read-only Confluence CLI for LLM agents. Search and read wiki pages "
            "with structured JSON output. Multi-instance: 'cloud' (authenticated) "
            "and 'whamcloud' (public Lustre community wiki, anonymous read-only)."
        ),
        env_vars=[
            {"name": "CONFLUENCE_SERVER", "description": "Confluence base URL", "required": "false"},
            {"name": "CONFLUENCE_TOKEN", "description": "API token (bearer/basic)", "required": "false"},
        ],
        commands=[
            Command(
                name="search",
                description="Search pages by text (or raw CQL). Returns matching pages.",
                usage="confluence search <text> [--space KEY] [--cql] [--limit N]",
                arguments=[
                    Argument(name="query", description="Search text, or a raw CQL query with --cql", required=True),
                    Argument(name="--space", description="Restrict to a space key"),
                    Argument(name="--cql", description="Treat the query as a raw CQL expression", type="boolean", default=False),
                    Argument(name="--limit", description="Maximum results (default: 25)", type="integer", default=25),
                    Argument(name="--output", description="Output only this field from each result (one per line)"),
                ],
                examples=[
                    "confluence search 'lustre nodemap'",
                    "confluence -I whamcloud search 'lnet routing' --space LNet",
                    "confluence search 'type = page and title ~ \"Maintainers\"' --cql",
                ],
                output_fields=["id", "type", "title", "space", "url"],
                next_actions=["get"],
            ),
            Command(
                name="get",
                description="Get a page's content by id or URL (body rendered to text).",
                usage="confluence get <id-or-url>",
                arguments=[
                    Argument(name="id", description="Page id or Confluence page URL", required=True),
                    Argument(name="--output", description="Output only this field as plain text"),
                ],
                examples=[
                    "confluence get 3727963991",
                    "confluence get https://your-org.atlassian.net/wiki/spaces/EXA/pages/3727963991/Lustre+Maintainers",
                    "confluence get 3727963991 --output text",
                ],
                output_fields=["id", "type", "title", "space", "version", "url", "text"],
                next_actions=["search"],
            ),
            Command(
                name="spaces",
                description="List spaces on the instance (discovery / connectivity check).",
                usage="confluence spaces [--limit N]",
                arguments=[
                    Argument(name="--limit", description="Maximum spaces (default: 100)", type="integer", default=100),
                ],
                examples=["confluence spaces", "confluence -I whamcloud spaces"],
                output_fields=["key", "name", "type", "url"],
                next_actions=["search"],
            ),
            Command(
                name="config-sample",
                description="Print a sample ~/.confluence-tool.json.",
                usage="confluence config-sample",
                arguments=[],
                examples=["confluence config-sample > ~/.confluence-tool.json"],
                output_fields=[],
                next_actions=[],
            ),
            Command(
                name="describe",
                description="Show this machine-readable API description.",
                usage="confluence describe [--command NAME]",
                arguments=[Argument(name="--command", description="Show one command only")],
                examples=["confluence describe", "confluence describe --command search"],
                output_fields=[],
                next_actions=[],
            ),
        ],
    )
