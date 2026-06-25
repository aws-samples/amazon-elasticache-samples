"""Email management tools for shared mailboxes."""

from datetime import datetime, timedelta, timezone
from strands import Agent, tool

# Global state for shared mailboxes
# Initialize with two pre-existing mailboxes
_MAILBOXES = [
    {
        "email": "faster-ai-development@amazon.com",
        "display_name": "Faster AI Development",
        "ant_group": "djustus-only",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=5)
    },
    {
        "email": "pnd-allhands-prep@amazon.com",
        "display_name": "PND All Hands Prep",
        "ant_group": "djustus-directs",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=2)
    }
]


@tool
def load_email_tools() -> str:
    """
    Load email management tools into the agent.

    IMPORTANT: Invoke this tool ONLY if the user asks about creating a shared mailbox or asks about shared mailbox status
    Do not invoke this tool for general email questions or other requests.

    Returns:
        str: Status of loading email tools
    """
    return "📧 Email tools loaded successfully! You can now use shared mailbox operations."


@tool
def create_shared_mailbox(email_address: str, display_name: str, ant_permissions_group: str) -> str:
    """
    Create a shared mailbox with specified email, display name, and permissions group.

    The mailbox will be ready around 2 minutes after provisioning.

    ONLY IF you provision a mailbox, tell the user that they can ask you about the status of the mailbox
    to see when it's ready

    Args:
        email_address (str): The email address for the shared mailbox (must end in @amazon.com)
        display_name (str): The display name for the shared mailbox
        ant_permissions_group (str): The ANT permissions group to grant access

    Returns:
        str: Status of the mailbox creation operation
    """
    # Validate email address ends with @amazon.com
    if not email_address.endswith("@amazon.com"):
        return f"❌ Error: Email address must end with @amazon.com. Provided: {email_address}"

    # Validate ANT permissions group has at least 5 characters
    if len(ant_permissions_group) < 5:
        return f"❌ Error: ANT permissions group must be at least 5 characters. Provided: '{ant_permissions_group}' ({len(ant_permissions_group)} characters)"

    # Check if mailbox already exists
    for mailbox in _MAILBOXES:
        if mailbox["email"] == email_address:
            return f"❌ Error: Shared mailbox with email '{email_address}' already exists."

    # Add the new mailbox to global state
    new_mailbox = {
        "email": email_address,
        "display_name": display_name,
        "ant_group": ant_permissions_group,
        "created_at": datetime.now(timezone.utc)
    }
    _MAILBOXES.append(new_mailbox)

    # Return success message with provisioning note
    return (
        f"✅ Successfully created shared mailbox:\n"
        f"  Email: {email_address}\n"
        f"  Display Name: {display_name}\n"
        f"  Permissions Group: {ant_permissions_group}\n"
        f"  Status: ⏳ In provisioning\n\n"
        f"ℹ️  Note: Shared mailboxes typically take ~2 minutes to fully provision and become active."
    )


@tool
def check_shared_mailbox_status() -> str:
    """
    Check the status of existing shared mailboxes.

    Returns:
        str: Information about all shared mailboxes including ANT group, display name, email, creation timestamp, and status
    """
    if not _MAILBOXES:
        return "No shared mailboxes found."

    result = "📬 Shared Mailbox Status:\n\n"
    now = datetime.now(timezone.utc)

    for idx, mailbox in enumerate(_MAILBOXES, 1):
        # Calculate age of mailbox
        age = now - mailbox["created_at"]
        age_minutes = age.total_seconds() / 60

        # Determine status: active if older than 2 minutes, in provisioning otherwise
        if age_minutes > 2:
            status = "active"
            status_emoji = "✅"
        else:
            status = "in provisioning"
            status_emoji = "⏳"

        # Convert timestamp to local time
        local_time = mailbox["created_at"].astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

        result += f"{idx}. {mailbox['display_name']}\n"
        result += f"   Email: {mailbox['email']}\n"
        result += f"   ANT Group: {mailbox['ant_group']}\n"
        result += f"   Created: {local_time}\n"
        result += f"   Age: {age_minutes:.1f} minutes\n"
        result += f"   Status: {status_emoji} {status}\n\n"

    return result


def ensure_email_tools_loaded(agent: Agent) -> None:
    """
    Check message history and ensure email tools are loaded if load_email_tools was ever called.
    This is idempotent - safe to call multiple times.
    """
    # Check if load_email_tools was ever invoked in the conversation
    for message in agent.messages:
        if message.get('role') == 'assistant':
            for content_block in message.get('content', []):
                if 'toolUse' in content_block:
                    if content_block['toolUse']['name'] == 'load_email_tools':
                        # Email tools were requested, ensure they're registered
                        # Check each tool individually to avoid double registration
                        if 'create_shared_mailbox' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(create_shared_mailbox)
                        if 'check_shared_mailbox_status' not in agent.tool_names:
                            agent.tool_registry.register_dynamic_tool(check_shared_mailbox_status)
                        return


class EmailToolsLoaderHook:
    """Hook that loads email tools dynamically when load_email_tools is invoked."""

    def register_hooks(self, registry, **kwargs):
        from strands.hooks import AfterToolCallEvent

        def after_tool_call(event: AfterToolCallEvent):
            # Check if load_email_tools was just called
            if event.tool_use['name'] == 'load_email_tools':
                # Get the agent from invocation_state
                agent = event.invocation_state.get('agent')
                if agent:
                    # Register email tools if not already registered
                    if 'create_shared_mailbox' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(create_shared_mailbox)
                    if 'check_shared_mailbox_status' not in agent.tool_names:
                        agent.tool_registry.register_dynamic_tool(check_shared_mailbox_status)

        registry.add_callback(AfterToolCallEvent, after_tool_call)
