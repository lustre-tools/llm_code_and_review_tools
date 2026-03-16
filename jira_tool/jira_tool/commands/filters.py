"""Filter commands: filter-list, filter-get, filter-export, filter-import, filter-scrape."""

import html as htmlmod
import json
import re
import sys
from typing import Any

import click
import requests

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError
from ._helpers import (
    get_client,
    handle_error,
    output_result,
)


def _normalize_filter(raw_filter: dict[str, Any]) -> dict[str, Any]:
    """Normalize a JIRA filter to agent-friendly format."""
    owner = raw_filter.get("owner", {})

    # Server uses "name", Cloud uses "accountId"
    owner_username = owner.get("name") or owner.get("accountId") if owner else None

    result: dict[str, Any] = {
        "id": raw_filter.get("id"),
        "name": raw_filter.get("name"),
        "jql": raw_filter.get("jql"),
        "owner": owner.get("displayName") if owner else None,
        "owner_name": owner_username,
        "favourite": raw_filter.get("favourite", False),
    }

    description = raw_filter.get("description")
    if description:
        result["description"] = description

    share_perms = raw_filter.get("sharePermissions", [])
    if share_perms:
        result["shared"] = True
        result["share_permissions"] = [
            {
                "type": p.get("type"),
                "group": p.get("group", {}).get("name") if p.get("group") else None,
                "project": p.get("project", {}).get("key") if p.get("project") else None,
                "role": p.get("role", {}).get("name") if p.get("role") else None,
            }
            for p in share_perms
        ]
    else:
        result["shared"] = False

    return result


def _export_format(raw_filter: dict[str, Any]) -> dict[str, Any]:
    """Create a portable export record for a filter.

    Includes everything needed to recreate the filter on another instance.
    """
    owner = raw_filter.get("owner", {})
    owner_username = owner.get("name") or owner.get("accountId") if owner else None
    result: dict[str, Any] = {
        "name": raw_filter.get("name"),
        "jql": raw_filter.get("jql"),
        "description": raw_filter.get("description", ""),
        "favourite": raw_filter.get("favourite", False),
        "source_id": raw_filter.get("id"),
        "owner_display_name": owner.get("displayName") if owner else None,
        "owner_username": owner_username,
    }

    share_perms = raw_filter.get("sharePermissions", [])
    if share_perms:
        result["share_permissions"] = share_perms

    return result


def register(main):
    """Register filter commands on *main*."""

    @main.group("filter")
    def filter_group() -> None:
        """Saved filter operations (list, get, export, import)."""
        pass

    @filter_group.command("list")
    @click.option("--owner", help="Filter by owner username")
    @click.option("--name", "filter_name", help="Filter by name (substring match)")
    @click.option("--limit", default=100, help="Maximum results (default: 100)")
    @click.pass_context
    def filter_list(ctx: click.Context, owner: str | None, filter_name: str | None, limit: int) -> None:
        """List saved filters.

        Without options, lists filters visible to the current user.
        Use --owner to filter by owner username.

        Uses filter/search API (JIRA Cloud and Server 8.x+). Falls
        back to filter/favourite on servers where search is unavailable.
        """
        command = "filter.list"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            try:
                result = client.search_filters(
                    filter_name=filter_name,
                    owner=owner,
                    max_results=limit,
                )
                raw_filters = result.get("values", [])
                source = "search"
            except (JiraToolError, Exception) as search_err:
                # filter/search not available — fall back to favourites
                if owner or filter_name:
                    # Can't filter by owner/name without search endpoint
                    raise JiraToolError(
                        message=(
                            f"filter/search endpoint not available on this server "
                            f"(got: {search_err}). "
                            f"Use 'jira filter favourites' or try from JIRA Cloud."
                        ),
                    ) from search_err
                raw_filters = client.get_favourite_filters()
                source = "favourites"

            filters = [_normalize_filter(f) for f in raw_filters]

            data: dict[str, Any] = {
                "total": len(filters),
                "returned": len(filters),
                "source": source,
                "filters": filters,
            }
            if source == "favourites":
                data["note"] = (
                    "filter/search unavailable — showing starred filters only. "
                    "Ask users to star filters before export, or use JIRA Cloud instance."
                )

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @filter_group.command("get")
    @click.argument("filter_id")
    @click.pass_context
    def filter_get(ctx: click.Context, filter_id: str) -> None:
        """Get details of a saved filter by ID.

        FILTER_ID is the numeric filter ID.
        """
        command = "filter.get"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            raw_filter = client.get_filter(filter_id)
            data = _normalize_filter(raw_filter)

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @filter_group.command("favourites")
    @click.pass_context
    def filter_favourites(ctx: click.Context) -> None:
        """List the current user's favourite (starred) filters."""
        command = "filter.favourites"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            raw_filters = client.get_favourite_filters()
            filters = [_normalize_filter(f) for f in raw_filters]

            data = {
                "total": len(filters),
                "filters": filters,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @filter_group.command("export")
    @click.option("--owner", help="Export filters owned by this username")
    @click.option("--name", "filter_name", help="Export filters matching this name")
    @click.option("--all", "export_all", is_flag=True, help="Export all visible filters (paginated)")
    @click.option("--output", "output_file", type=click.Path(), help="Write to file instead of stdout")
    @click.pass_context
    def filter_export(ctx: click.Context, owner: str | None, filter_name: str | None,
                      export_all: bool, output_file: str | None) -> None:
        """Export saved filters as portable JSON.

        Exports filter definitions (name, JQL, description, owner,
        share permissions) in a format suitable for importing into
        another JIRA instance.

        Without --all, exports filters visible to the current user.
        Use --owner to export a specific user's filters.

        Example: jira filter export --owner heqing > heqing_filters.json
        Example: jira filter export --all --output all_filters.json
        """
        command = "filter.export"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            try:
                if export_all:
                    # Paginate through all filters
                    all_filters: list[dict[str, Any]] = []
                    start_at = 0
                    page_size = 100
                    while True:
                        result = client.search_filters(
                            filter_name=filter_name,
                            owner=owner,
                            max_results=page_size,
                            start_at=start_at,
                        )
                        values = result.get("values", [])
                        all_filters.extend(values)
                        total = result.get("total", len(all_filters))
                        if len(all_filters) >= total or not values:
                            break
                        start_at += len(values)
                    raw_filters = all_filters
                    source = "search"
                else:
                    result = client.search_filters(
                        filter_name=filter_name,
                        owner=owner,
                        max_results=100,
                    )
                    raw_filters = result.get("values", [])
                    source = "search"
            except (JiraToolError, Exception):
                # filter/search not available — fall back to favourites
                if owner or filter_name:
                    raise
                raw_filters = client.get_favourite_filters()
                source = "favourites"

            exported = [_export_format(f) for f in raw_filters]

            # Group by owner for readability
            by_owner: dict[str, list[dict[str, Any]]] = {}
            for f in exported:
                owner_key = f.get("owner_display_name") or f.get("owner_username") or "unknown"
                by_owner.setdefault(owner_key, []).append(f)

            export_data: dict[str, Any] = {
                "source_server": client.config.server,
                "source_method": source,
                "total_filters": len(exported),
                "owners": len(by_owner),
                "filters_by_owner": by_owner,
                "filters": exported,
            }
            if source == "favourites":
                export_data["note"] = (
                    "Exported from favourites only (filter/search unavailable). "
                    "Only starred filters are included."
                )

            if output_file:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(export_data, f, indent=2)
                data = {
                    "exported": len(exported),
                    "owners": len(by_owner),
                    "output_file": output_file,
                }
            else:
                data = export_data

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @filter_group.command("import")
    @click.argument("input_file", type=click.Path(exists=True))
    @click.option("--dry-run", is_flag=True, help="Show what would be created without creating")
    @click.option("--favourite/--no-favourite", default=False, help="Mark imported filters as favourites")
    @click.pass_context
    def filter_import(ctx: click.Context, input_file: str, dry_run: bool, favourite: bool) -> None:
        """Import filters from an export file into the current instance.

        INPUT_FILE is a JSON file produced by 'jira filter export'.

        Use --dry-run to preview what would be created.
        Typically used with -I to target a specific instance:
          jira -I cloud filter import exported_filters.json --dry-run

        Note: JQL referencing custom field IDs may need manual adjustment
        if field IDs differ between instances.
        """
        command = "filter.import"
        pretty = ctx.obj.get("pretty", False)

        try:
            with open(input_file, encoding="utf-8") as f:
                export_data = json.load(f)

            filters = export_data.get("filters", [])

            if dry_run:
                preview = []
                for filt in filters:
                    preview.append({
                        "name": filt["name"],
                        "jql": filt["jql"],
                        "original_owner": filt.get("owner_display_name") or filt.get("owner_username"),
                        "source_id": filt.get("source_id"),
                    })
                data: dict[str, Any] = {
                    "dry_run": True,
                    "would_create": len(preview),
                    "source_server": export_data.get("source_server"),
                    "filters": preview,
                }
                envelope = success_response(data, command)
                output_result(envelope, pretty)
                sys.exit(ExitCode.SUCCESS)

            client = get_client(ctx)

            created = []
            errors = []
            for filt in filters:
                try:
                    result = client.create_filter(
                        name=filt["name"],
                        jql=filt["jql"],
                        description=filt.get("description", ""),
                        favourite=favourite,
                    )
                    created.append({
                        "name": filt["name"],
                        "new_id": result.get("id"),
                        "source_id": filt.get("source_id"),
                    })
                except JiraToolError as e:
                    errors.append({
                        "name": filt["name"],
                        "jql": filt["jql"],
                        "error": str(e),
                    })

            data = {
                "created": len(created),
                "errors": len(errors),
                "target_server": client.config.server,
                "results": created,
            }
            if errors:
                data["failed"] = errors

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except json.JSONDecodeError as e:
            from ..errors import InvalidInputError, ErrorCode
            sys.exit(handle_error(
                InvalidInputError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Invalid JSON in {input_file}: {e}",
                ),
                command, pretty,
            ))
        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @filter_group.command("scrape")
    @click.option("--output", "output_file", type=click.Path(), help="Write to file instead of stdout")
    @click.option("--limit", default=0, help="Max filters to scrape (0 = all)")
    @click.option("--skip-verify", is_flag=True, help="Skip RSS verification (much faster)")
    @click.pass_context
    def filter_scrape(ctx: click.Context, output_file: str | None,
                      limit: int, skip_verify: bool) -> None:
        """Scrape filters from JIRA Server web UI (HTML fallback).

        For servers where the filter REST API is blocked by a reverse
        proxy, this command scrapes the ManageFilters JSP pages and
        optionally the XML RSS endpoint to verify each filter.

        Extracts: filter ID, name, share permissions, favourite count.
        With RSS verification (default): also confirms accessibility
        and gets issue count per filter.

        JQL is NOT available via scraping — the web UI loads it via
        JavaScript. Users can look up their JQL at:
          https://SERVER/issues/?filter=FILTER_ID

        Examples:
          jira filter scrape --limit 100 --output sample.json
          jira filter scrape --skip-verify --output all_filters.json
        """
        command = "filter.scrape"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            session = client._session
            server = client.config.server

            # Step 1: Scrape ManageFilters.jspa search view (paginated)
            all_scraped: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            paging_offset = 0
            total_reported = None
            done = False

            click.echo("Scraping ManageFilters.jspa...", err=True)

            while not done:
                url = (
                    f"{server}/secure/ManageFilters.jspa"
                    f"?filterView=search&Search=Search"
                    f"&searchShareType=any"
                    f"&sortColumn=name&sortAscending=true"
                    f"&pagingOffset={paging_offset}"
                )
                resp = session.get(url, timeout=client.timeout)
                if resp.status_code != 200:
                    raise JiraToolError(
                        message=f"ManageFilters.jspa returned HTTP {resp.status_code}",
                    )

                page_text = resp.text

                # Parse total on first page
                if total_reported is None:
                    total_m = re.search(r'\d+\s*-\s*\d+\s*of\s*(\d+)', page_text)
                    if total_m:
                        total_reported = int(total_m.group(1))
                        click.echo(f"  Server reports {total_reported} shared filters", err=True)
                        if limit:
                            click.echo(f"  Limiting to {limit}", err=True)

                # Extract filter rows
                filter_ids = re.findall(r'data-filter-id="(\d+)"', page_text)
                if not filter_ids:
                    break

                new_on_page = 0
                for fid in filter_ids:
                    if fid in seen_ids:
                        continue
                    seen_ids.add(fid)

                    row_m = re.search(
                        rf'data-filter-id="{fid}"(.*?)</tr>',
                        page_text, re.DOTALL,
                    )
                    if not row_m:
                        continue
                    row = row_m.group(0)

                    # Filter name
                    name_m = re.search(rf'filterlink_{fid}[^>]*>([^<]+)', row)
                    name = htmlmod.unescape(name_m.group(1).strip()) if name_m else f"filter-{fid}"

                    # Share permissions
                    shares = re.findall(
                        r'<li class="([^"]+)"[^>]*title="([^"]+)"',
                        row,
                    )
                    share_list = [
                        {"type": s[0], "description": htmlmod.unescape(s[1])}
                        for s in shares
                    ]

                    # Favourite count
                    fav_m = re.search(
                        rf'fav_count_disabled_mf_\w+_SearchRequest_{fid}">\s*(\d+)',
                        row,
                    )
                    fav_count = int(fav_m.group(1)) if fav_m else 0

                    all_scraped.append({
                        "id": fid,
                        "name": name,
                        "shares": share_list,
                        "favourite_count": fav_count,
                        "url": f"{server}/issues/?filter={fid}",
                    })
                    new_on_page += 1

                    if limit and len(all_scraped) >= limit:
                        done = True
                        break

                # If no new filters on this page, pagination is stuck
                if new_on_page == 0:
                    break

                page_num = paging_offset + 1
                if page_num % 5 == 0 or done or not re.search(r'class="icon icon-next"', page_text):
                    click.echo(
                        f"  Page {page_num}: {len(all_scraped)} filters scraped",
                        err=True,
                    )

                # Check for next page
                if not re.search(r'class="icon icon-next"', page_text):
                    break
                paging_offset += 1

            # Step 2: Optionally verify via RSS/XML endpoint
            verified = 0
            if not skip_verify:
                click.echo(
                    f"Verifying {len(all_scraped)} filters via RSS...",
                    err=True,
                )
                for i, filt in enumerate(all_scraped):
                    fid = filt["id"]
                    xml_url = (
                        f"{server}/sr/jira.issueviews:searchrequest-xml"
                        f"/{fid}/SearchRequest-{fid}.xml?tempMax=0"
                    )
                    try:
                        xml_resp = session.get(xml_url, timeout=client.timeout)
                        if xml_resp.status_code == 200:
                            title_m = re.search(r'<title>([^<]+)', xml_resp.text)
                            if title_m:
                                rss_title = title_m.group(1).strip()
                                rss_title = re.sub(r'\s*\([^)]+\)\s*$', '', rss_title)
                                filt["rss_title"] = rss_title
                            count_m = re.search(r'total="(\d+)"', xml_resp.text)
                            if count_m:
                                filt["issue_count"] = int(count_m.group(1))
                            filt["accessible"] = True
                            verified += 1
                        else:
                            filt["accessible"] = False
                    except requests.RequestException:
                        filt["accessible"] = False

                    if (i + 1) % 50 == 0:
                        click.echo(f"  Verified {i + 1}/{len(all_scraped)}...", err=True)

                click.echo(
                    f"  {verified}/{len(all_scraped)} filters accessible",
                    err=True,
                )

            # Build output
            scrape_data: dict[str, Any] = {
                "source_server": server,
                "source_method": "html_scrape",
                "total_on_server": total_reported,
                "total_scraped": len(all_scraped),
                "note": (
                    "JQL not available via scraping. Users can view their "
                    "filter JQL at the 'url' field for each filter."
                ),
                "filters": all_scraped,
            }
            if not skip_verify:
                scrape_data["verified_accessible"] = verified

            if output_file:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(scrape_data, f, indent=2)
                data: dict[str, Any] = {
                    "scraped": len(all_scraped),
                    "total_on_server": total_reported,
                    "output_file": output_file,
                }
                if not skip_verify:
                    data["verified"] = verified
            else:
                data = scrape_data

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
