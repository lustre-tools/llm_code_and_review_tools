"""Attachment commands: attachment group (get/content/delete/upload), attach, attachments."""

import sys

import click

from ..envelope import success_response
from ..errors import ConfigError, ExitCode, JiraToolError
from ._helpers import (
    _normalize_attachment,
    extract_issue_key,
    get_client,
    handle_error,
    output_result,
)


def register(main):
    """Register attachment commands on *main*."""

    @main.group()
    def attachment() -> None:
        """Attachment operations."""
        pass

    @attachment.command("get")
    @click.argument("attachment_id")
    @click.pass_context
    def attachment_get(ctx: click.Context, attachment_id: str) -> None:
        """
        Get attachment metadata.

        ATTACHMENT_ID is the numeric attachment ID.
        """
        command = "attachment.get"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            raw_attachment = client.get_attachment(attachment_id)
            attachment_data = _normalize_attachment(raw_attachment)

            envelope = success_response(attachment_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @attachment.command("content")
    @click.argument("attachment_id")
    @click.option("--max-size", default=102400, help="Maximum size in bytes (default: 100KB, 0 for no limit)")
    @click.option("--encoding", default="utf-8", help="Text encoding (default: utf-8)")
    @click.option("--raw", is_flag=True, help="Output raw content to stdout (no JSON envelope)")
    @click.pass_context
    def attachment_content(ctx: click.Context, attachment_id: str, max_size: int, encoding: str, raw: bool) -> None:
        """
        Get attachment content.

        ATTACHMENT_ID is the numeric attachment ID.

        By default, limits to 100KB and decodes as UTF-8 text.
        Use --raw to output content directly (useful for piping).

        Note: Binary files may not display correctly without --raw.
        """
        command = "attachment.content"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            content_bytes, metadata = client.get_attachment_content(attachment_id, max_size=max_size)

            if raw:
                # Output raw content directly
                sys.stdout.buffer.write(content_bytes)
                sys.exit(ExitCode.SUCCESS)

            # Try to decode as text
            try:
                content_text = content_bytes.decode(encoding)
            except UnicodeDecodeError:
                # For binary files, indicate it's binary
                content_text = None

            content_data = {
                "attachment": _normalize_attachment(metadata),
                "size_bytes": len(content_bytes),
                "encoding": encoding if content_text else None,
                "is_text": content_text is not None,
                "content": content_text,
                "content_truncated": False,
            }

            if content_text is None:
                content_data["note"] = "Binary content - use --raw flag to download"

            envelope = success_response(content_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @attachment.command("delete")
    @click.argument("attachment_id")
    @click.pass_context
    def attachment_delete(ctx: click.Context, attachment_id: str) -> None:
        """
        Delete an attachment.

        ATTACHMENT_ID is the numeric attachment ID.
        """
        command = "attachment.delete"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)

            client.delete_attachment(attachment_id)

            data = {
                "attachment_id": attachment_id,
                "deleted": True,
            }

            envelope = success_response(data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @attachment.command("upload")
    @click.argument("key")
    @click.argument("file_path", type=click.Path(exists=True))
    @click.option("--filename", help="Override filename (default: use file's basename)")
    @click.pass_context
    def attachment_upload(ctx: click.Context, key: str, file_path: str, filename: str | None) -> None:
        """
        Upload an attachment to an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.
        FILE_PATH is the path to the file to upload.
        """
        command = "attachment.upload"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            result = client.upload_attachment(key, file_path, filename=filename)

            # JIRA returns a list of attachments (usually just one)
            attachments = [_normalize_attachment(a) for a in result] if result else []

            upload_data = {
                "issue_key": key,
                "uploaded": len(attachments),
                "attachments": attachments,
            }

            envelope = success_response(upload_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))

    @main.command("attach")
    @click.argument("key")
    @click.argument("file_path", type=click.Path(exists=True))
    @click.option("--filename", help="Override filename (default: use file's basename)")
    @click.pass_context
    def attach(ctx: click.Context, key: str, file_path: str, filename: str | None) -> None:
        """Alias for 'attachment upload' - upload an attachment to an issue."""
        ctx.invoke(attachment_upload, key=key, file_path=file_path, filename=filename)

    @main.command("attachments")
    @click.argument("key")
    @click.pass_context
    def issue_attachments(ctx: click.Context, key: str) -> None:
        """
        List attachments for an issue.

        KEY is the issue key (e.g., PROJ-123) or a JIRA URL.

        Returns attachment metadata including filename, size, and content URL.
        """
        command = "attachments"
        pretty = ctx.obj.get("pretty", False)

        try:
            client = get_client(ctx)
            key = extract_issue_key(key)

            # Get issue with attachment field
            raw_issue = client.get_issue(key, fields=["attachment"])
            attachments = raw_issue.get("fields", {}).get("attachment", [])

            # Normalize attachments
            attachments_data = {
                "issue_key": key,
                "total": len(attachments),
                "attachments": [_normalize_attachment(a) for a in attachments],
            }

            envelope = success_response(attachments_data, command)
            output_result(envelope, pretty)
            sys.exit(ExitCode.SUCCESS)

        except JiraToolError as e:
            sys.exit(handle_error(e, command, pretty))
        except ConfigError as e:
            sys.exit(handle_error(e, command, pretty))
