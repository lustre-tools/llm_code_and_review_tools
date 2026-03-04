"""Link commands: link, unlink, links, link-types."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError
from ._helpers import (
    extract_issue_key,
    get_client,
    handle_error,
    output_result,
)


def register(main):
    """Register link commands on *main*."""

    @main.command("link")
    @click.argument("key")
    @click.argument("target_key")
    @click.option("--type", "link_type", default="Related",
                  help="Link type name (e.g., Related, Blocks, Duplicate). Default: Related")
    @click.pass_context
    def issue_link_create(ctx: click.Context, key: str, target_key: str, link_type: str) -> None:
        """
        Create a link between two issues.

        KEY is the source issue key (e.g., PROJ-123) or a JIRA URL.
        TARGET_KEY is the destination issue key.
        """
        command = "link"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)
            target_key = extract_issue_key(target_key)

            client.create_link(key, target_key, link_type)

            link_data = {
                "source_key": key,
                "target_key": target_key,
                "link_type": link_type,
            }

            envelope = success_response(link_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("unlink")
    @click.argument("link_id")
    @click.pass_context
    def issue_unlink(ctx: click.Context, link_id: str) -> None:
        """
        Remove a link between two issues.

        LINK_ID is the numeric link ID (from 'jira links' output).
        """
        command = "unlink"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            client.delete_link(link_id)

            data = {
                "link_id": link_id,
                "deleted": True,
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
            client = get_client(ctx)
            key = extract_issue_key(key)

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
