"""Link commands: link, unlink, links, link-types."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ErrorCode, ExitCode, JiraToolError, NotFoundError
from ._helpers import (
    extract_issue_key,
    get_client,
    handle_error,
    output_result,
)

_LINK_TYPE_ALIASES = {
    "related": ("relates",),
    "relates": ("related",),
    "blocker": ("blocks",),
    "blocks": ("blocker",),
    "duplicates": ("duplicate",),
}


def _resolve_link_type(client, user_type: str) -> str:
    """Fuzzy-match a user-provided link type against available JIRA link types.

    Matches against name, inward, and outward descriptions (case-insensitive).
    Returns the canonical JIRA link type name.
    """
    raw = client.get_link_types()
    link_types = raw.get("issueLinkTypes", [])
    user_lower = user_type.lower()

    # Exact name match first
    for lt in link_types:
        if lt["name"].lower() == user_lower:
            return lt["name"]

    # Match against inward/outward descriptions and common aliases
    for lt in link_types:
        candidates = [
            lt.get("inward", "").lower(),
            lt.get("outward", "").lower(),
        ]
        if user_lower in candidates:
            return lt["name"]
        # Substring match (e.g., "blocks" matches "is blocking")
        for c in candidates:
            if user_lower in c or c in user_lower:
                return lt["name"]

    # Server and Cloud name the same types differently ("Related" on
    # jira.whamcloud.com, "Relates" on Cloud); try the other spelling.
    for alias in _LINK_TYPE_ALIASES.get(user_lower, ()):
        for lt in link_types:
            if lt["name"].lower() == alias:
                return lt["name"]

    # No match — return as-is and let JIRA reject it with a clear error
    return user_type


def register(main):
    """Register link commands on *main*."""

    @main.command("link")
    @click.argument("key")
    @click.argument("target_key")
    @click.option("--type", "link_type", default="Related",
                  help="Link type name (e.g., Related, Blocker, Duplicate). "
                  "Also accepts common aliases: Blocks -> Blocker. Default: Related")
    @click.pass_context
    def issue_link_create(ctx: click.Context, key: str, target_key: str, link_type: str) -> None:
        """
        Create a link between two issues.

        KEY is the source issue key (e.g., PROJ-123) or a JIRA URL.
        TARGET_KEY is the destination issue key.

        The link type is fuzzy-matched against available types by name,
        inward, or outward description (e.g., "Blocks" matches "Blocker").
        """
        command = "link"
        pretty = ctx.obj.get("pretty", False)

        try:
            key = extract_issue_key(key)
            target_key = extract_issue_key(target_key)
            client = get_client(ctx, issue_key=key)

            # Fuzzy-match link type against available types
            resolved_type = _resolve_link_type(client, link_type)

            client.create_link(key, target_key, resolved_type)

            link_data = {
                "source_key": key,
                "target_key": target_key,
                "link_type": resolved_type,
            }

            envelope = success_response(link_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("unlink")
    @click.argument("key")
    @click.argument("target_key", required=False, default=None)
    @click.option("--type", "link_type", default=None,
                  help="Only remove links of this type (fuzzy-matched)")
    @click.pass_context
    def issue_unlink(ctx: click.Context, key: str, target_key: str | None, link_type: str | None) -> None:
        """
        Remove a link between two issues.

        Usage:
          jira unlink LINK_ID              Remove link by numeric ID
          jira unlink KEY TARGET_KEY       Remove link between two issues
          jira unlink KEY TARGET_KEY --type Blocker   Remove specific link type
        """
        command = "unlink"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx, issue_key=key)

            if target_key is None:
                # Single argument: treat as link ID
                client.delete_link(key)
                data = {
                    "link_id": key,
                    "deleted": True,
                }
            else:
                # Two arguments: find and delete link between issues
                source = extract_issue_key(key)
                target = extract_issue_key(target_key)

                raw_issue = client.get_issue(source, fields=["issuelinks"])
                raw_links = raw_issue.get("fields", {}).get("issuelinks", [])

                deleted = []
                for link in raw_links:
                    linked_key = None
                    if "inwardIssue" in link:
                        linked_key = link["inwardIssue"].get("key")
                    elif "outwardIssue" in link:
                        linked_key = link["outwardIssue"].get("key")

                    if linked_key != target:
                        continue

                    if link_type:
                        resolved = _resolve_link_type(client, link_type)
                        if link["type"]["name"] != resolved:
                            continue

                    client.delete_link(str(link["id"]))
                    deleted.append({
                        "link_id": str(link["id"]),
                        "link_type": link["type"]["name"],
                    })

                if not deleted:
                    raise NotFoundError(
                        code=ErrorCode.NOT_FOUND,
                        message=f"No link found between {source} and {target}"
                        + (f" of type '{link_type}'" if link_type else ""),
                    )

                data = {
                    "source_key": source,
                    "target_key": target,
                    "deleted": deleted,
                }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("links")
    @click.argument("key")
    @click.pass_context
    def issue_links(ctx: click.Context, key: str) -> None:
        """
        List issue links (relationships to other issues).

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

        Shows relationships like: blocks, is blocked by, relates to, duplicates, etc.
        """
        command = "links"
        pretty = ctx.obj.get("pretty", False)

        try:
            key = extract_issue_key(key)
            client = get_client(ctx, issue_key=key)

            # Get issue with issuelinks field
            raw_issue = client.get_issue(key, fields=["issuelinks"])
            raw_links = raw_issue.get("fields", {}).get("issuelinks", [])

            # Normalize links
            links = []
            for link in raw_links:
                link_type = link.get("type", {})
                # Each link has either inwardIssue or outwardIssue
                if "inwardIssue" in link:
                    linked_issue = link["inwardIssue"]
                    direction = "inward"
                    relationship = link_type.get("inward", "related to")
                elif "outwardIssue" in link:
                    linked_issue = link["outwardIssue"]
                    direction = "outward"
                    relationship = link_type.get("outward", "relates to")
                else:
                    continue

                links.append({
                    "id": link.get("id"),
                    "direction": direction,
                    "relationship": relationship,
                    "link_type": link_type.get("name"),
                    "issue_key": linked_issue.get("key"),
                    "issue_summary": linked_issue.get("fields", {}).get("summary"),
                    "issue_status": linked_issue.get("fields", {}).get("status", {}).get("name"),
                })

            links_data = {
                "issue_key": key,
                "total": len(links),
                "links": links,
            }

            envelope = success_response(links_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("link-types")
    @click.pass_context
    def issue_link_types(ctx: click.Context) -> None:
        """
        List available issue link types.

        Shows the valid type names for use with 'jira link --type'.
        """
        command = "link-types"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            raw = client.get_link_types()
            link_types = []
            for lt in raw.get("issueLinkTypes", []):
                link_types.append({
                    "id": lt.get("id"),
                    "name": lt.get("name"),
                    "inward": lt.get("inward"),
                    "outward": lt.get("outward"),
                })

            data = {
                "total": len(link_types),
                "link_types": link_types,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
