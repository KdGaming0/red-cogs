import discord
from discord.ext import commands
from typing import Dict, List, Optional, Any, Callable
from .utils import is_snapshot, filter_minecraft_versions, format_version_list
import logging

log = logging.getLogger("red.modrinth_checker")


class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.value = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.edit_message(content="✅ Confirmed!", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.edit_message(content="❌ Cancelled!", view=None)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class MinecraftVersionView(discord.ui.View):
    def __init__(self, available_versions: List[str], has_snapshots: bool = False):
        super().__init__(timeout=300)
        self.available_versions = available_versions
        self.release_versions = filter_minecraft_versions(available_versions, include_snapshots=False)
        self.has_snapshots = has_snapshots
        self.result = None
        self.selected_versions = []
        self.specific_mode = False
        self.showing_snapshots = False  # Start with snapshots hidden

        # Start with release versions only
        self.current_versions = self.release_versions.copy()

        # Build initial view
        self._build_main_view()

    def _build_main_view(self):
        """Build the main selection view."""
        self.clear_items()

        # Main buttons with descriptions
        all_btn = discord.ui.Button(
            label="All Versions",
            style=discord.ButtonStyle.primary,
            row=0
        )
        all_btn.callback = self.all_versions_btn
        self.add_item(all_btn)

        specific_btn = discord.ui.Button(
            label="Specific Versions",
            style=discord.ButtonStyle.secondary,
            row=0
        )
        specific_btn.callback = self.specific_versions
        self.add_item(specific_btn)

        # Latest buttons
        latest_current_btn = discord.ui.Button(
            label="Latest (Current MC)",
            style=discord.ButtonStyle.success,
            row=1
        )
        latest_current_btn.callback = self.latest_current
        self.add_item(latest_current_btn)

        latest_always_btn = discord.ui.Button(
            label="Latest (Always)",
            style=discord.ButtonStyle.success,
            row=1
        )
        latest_always_btn.callback = self.latest_always
        self.add_item(latest_always_btn)

        # Add snapshot toggle if available
        if self.has_snapshots:
            snapshot_btn = discord.ui.Button(
                label="Show Snapshots" if not self.showing_snapshots else "Hide Snapshots",
                style=discord.ButtonStyle.secondary,
                row=2
            )
            snapshot_btn.callback = self._toggle_snapshots_main
            self.add_item(snapshot_btn)

    def _build_specific_view(self):
        """Build the specific version selection view."""
        self.clear_items()

        # Add version selector with current versions (respecting snapshot setting)
        version_select = VersionSelect(self.current_versions, self)
        self.add_item(version_select)

        # Add snapshot toggle if available
        if self.has_snapshots:
            snapshot_btn = discord.ui.Button(
                label="Show Snapshots" if not self.showing_snapshots else "Hide Snapshots",
                style=discord.ButtonStyle.secondary,
                row=1
            )
            snapshot_btn.callback = self._toggle_snapshots_specific
            self.add_item(snapshot_btn)

        # Add continue button
        continue_btn = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.green,
            row=2,
            disabled=len(self.selected_versions) == 0
        )
        continue_btn.callback = self._continue_specific
        self.add_item(continue_btn)

        # Add back button
        back_btn = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=2
        )
        back_btn.callback = self._back_to_main
        self.add_item(back_btn)

    def _create_main_embed(self):
        """Create the main selection embed."""
        embed = discord.Embed(
            title="Step 1: Minecraft Version Configuration",
            description="Which Minecraft versions should be monitored?",
            color=discord.Color.blue()
        )

        # Show current version list
        version_display = format_version_list(self.current_versions, max_display=15)
        embed.add_field(
            name=f"Available Versions {'(Including Snapshots)' if self.showing_snapshots else '(Release Only)'}",
            value=version_display,
            inline=False
        )

        # Add button descriptions
        embed.add_field(
            name="📋 Options:",
            value=(
                "**All Versions** - Monitor all available versions\n"
                "**Specific Versions** - Choose which versions to monitor\n"
                "**Latest (Current MC)** - Monitor only the latest version for current Minecraft\n"
                "**Latest (Always)** - Always monitor the newest version automatically\n"
                f"**{'Show' if not self.showing_snapshots else 'Hide'} Snapshots** - Toggle snapshot visibility"
            ),
            inline=False
        )

        return embed

    def _create_specific_embed(self):
        """Create the specific version selection embed."""
        embed = discord.Embed(
            title="Select Specific Versions",
            description="Choose which versions to monitor from the dropdown below:",
            color=discord.Color.blue()
        )

        embed.add_field(
            name=f"Available Versions {'(Including Snapshots)' if self.showing_snapshots else '(Release Only)'}",
            value=format_version_list(self.current_versions, max_display=15),
            inline=False
        )

        if self.selected_versions:
            embed.add_field(
                name="✅ Selected Versions",
                value=format_version_list(self.selected_versions),
                inline=False
            )
        else:
            embed.add_field(
                name="⚠️ Selected Versions",
                value="None selected yet",
                inline=False
            )

        return embed

    @discord.ui.button(label="All Versions", style=discord.ButtonStyle.primary, row=0)
    async def all_versions_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Monitor all versions."""
        self.result = {
            "type": "all",
            "versions": self.current_versions
        }

        embed = discord.Embed(
            title="✅ Configuration Complete",
            description=f"Monitoring **all versions** ({len(self.current_versions)} versions)",
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="Specific Versions", style=discord.ButtonStyle.secondary, row=0)
    async def specific_versions(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Allow selection of specific versions."""
        self.specific_mode = True
        self.selected_versions = []

        # Build specific selection view
        self._build_specific_view()
        embed = self._create_specific_embed()

        await interaction.response.edit_message(embed=embed, view=self)

    async def _toggle_snapshots_main(self, interaction: discord.Interaction):
        """Toggle snapshots in main menu."""
        self.showing_snapshots = not self.showing_snapshots

        # Update current versions based on snapshot setting
        if self.showing_snapshots:
            self.current_versions = self.available_versions.copy()
        else:
            self.current_versions = self.release_versions.copy()

        # Rebuild main view with updated button
        self._build_main_view()
        embed = self._create_main_embed()

        await interaction.response.edit_message(embed=embed, view=self)

    async def _toggle_snapshots_specific(self, interaction: discord.Interaction):
        """Toggle snapshots in specific selection mode."""
        self.showing_snapshots = not self.showing_snapshots

        # Update current versions based on snapshot setting
        if self.showing_snapshots:
            self.current_versions = self.available_versions.copy()
        else:
            self.current_versions = self.release_versions.copy()

        # Clear selected versions that are no longer available
        if not self.showing_snapshots:
            # Remove any snapshots from selected versions
            self.selected_versions = [v for v in self.selected_versions if not is_snapshot(v)]

        # Rebuild specific view
        self._build_specific_view()
        embed = self._create_specific_embed()

        await interaction.response.edit_message(embed=embed, view=self)

    async def _continue_specific(self, interaction: discord.Interaction):
        """Continue with selected specific versions."""
        if not self.selected_versions:
            await interaction.response.send_message("❌ Please select at least one version.", ephemeral=True)
            return

        self.result = {
            "type": "specific",
            "versions": self.selected_versions
        }

        embed = discord.Embed(
            title="✅ Configuration Complete",
            description=f"Monitoring **{len(self.selected_versions)} selected versions**",
            color=discord.Color.green()
        )

        embed.add_field(
            name="Selected Versions",
            value=format_version_list(self.selected_versions),
            inline=False
        )

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    async def _back_to_main(self, interaction: discord.Interaction):
        """Go back to main menu."""
        self.specific_mode = False
        # Don't clear selected versions in case user wants to go back to specific selection

        # Rebuild main view
        self._build_main_view()
        embed = self._create_main_embed()

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Latest (Current MC)", style=discord.ButtonStyle.success, row=1)
    async def latest_current(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Monitor only the latest version for current MC."""
        # Get the latest version from current display
        latest_version = self.current_versions[0] if self.current_versions else None

        if not latest_version:
            await interaction.response.send_message("❌ No versions available.", ephemeral=True)
            return

        self.result = {
            "type": "latest_current",
            "versions": [latest_version]
        }

        embed = discord.Embed(
            title="✅ Configuration Complete",
            description=f"Monitoring **latest version for current MC**: {latest_version}",
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="Latest (Always)", style=discord.ButtonStyle.success, row=1)
    async def latest_always(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Always monitor the latest version."""
        self.result = {
            "type": "latest_always",
            "versions": []  # Empty list means always get latest
        }

        embed = discord.Embed(
            title="✅ Configuration Complete",
            description="Monitoring **latest version always** (automatically updates to newest version)",
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class VersionSelect(discord.ui.Select):
    def __init__(self, versions: List[str], parent_view: MinecraftVersionView):
        self.parent_view = parent_view

        # Create options from versions (Discord has a 25 option limit)
        options = []
        for version in versions[:25]:  # Limit to first 25 versions
            # Check if already selected
            is_selected = version in parent_view.selected_versions

            options.append(discord.SelectOption(
                label=version,
                value=version,
                description=f"{'Snapshot' if is_snapshot(version) else 'Release'} version",
                default=is_selected
            ))

        super().__init__(
            placeholder="Select versions to monitor...",
            min_values=0,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        # Update selected versions
        self.parent_view.selected_versions = self.values.copy()

        # Update embed
        embed = self.parent_view._create_specific_embed()

        # Update continue button state
        for item in self.parent_view.children:
            if isinstance(item, discord.ui.Button) and item.label == "Continue":
                item.disabled = len(self.parent_view.selected_versions) == 0

        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class LoaderView(discord.ui.View):
    def __init__(self, available_loaders: List[str]):
        super().__init__(timeout=300)
        self.available_loaders = available_loaders
        self.result = None
        self.selected_loaders = []

        # Create buttons for each loader
        for i, loader in enumerate(available_loaders):
            button = discord.ui.Button(
                label=loader.title(),
                style=discord.ButtonStyle.secondary,
                row=i // 5  # 5 buttons per row
            )
            button.callback = self._create_loader_callback(loader)
            self.add_item(button)

        # Add "All Loaders" button
        all_btn = discord.ui.Button(
            label="All Loaders",
            style=discord.ButtonStyle.primary,
            row=(len(available_loaders) // 5) + 1
        )
        all_btn.callback = self._all_loaders_callback
        self.add_item(all_btn)

        # Add custom selection button
        custom_btn = discord.ui.Button(
            label="Custom Selection",
            style=discord.ButtonStyle.secondary,
            row=(len(available_loaders) // 5) + 1
        )
        custom_btn.callback = self._custom_selection_callback
        self.add_item(custom_btn)

    async def _all_loaders_callback(self, interaction: discord.Interaction):
        """Select all available loaders."""
        self.result = {
            "type": "all",
            "loaders": self.available_loaders
        }

        embed = discord.Embed(
            title="✅ Configuration Complete",
            description="Monitoring **all loaders**",
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    def _create_loader_callback(self, loader: str):
        """Create a callback for a specific loader button."""

        async def callback(interaction: discord.Interaction):
            self.result = {
                "type": "specific",
                "loaders": [loader]
            }

            embed = discord.Embed(
                title="✅ Configuration Complete",
                description=f"Monitoring **{loader.title()}** loader only",
                color=discord.Color.green()
            )

            await interaction.response.edit_message(embed=embed, view=None)
            self.stop()

        return callback

    async def _custom_selection_callback(self, interaction: discord.Interaction):
        """Allow custom selection of loaders."""
        embed = discord.Embed(
            title="Select Loaders",
            description="Select which loaders to monitor:",
            color=discord.Color.blue()
        )

        view = discord.ui.View(timeout=300)

        # Add loader selector
        loader_select = LoaderSelect(self.available_loaders, self)
        view.add_item(loader_select)

        # Add continue button
        continue_btn = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.green,
            disabled=True
        )
        continue_btn.callback = self._continue_callback
        view.add_item(continue_btn)

        await interaction.response.edit_message(embed=embed, view=view)

    async def _continue_callback(self, interaction: discord.Interaction):
        """Continue with selected loaders."""
        if not self.selected_loaders:
            await interaction.response.send_message("❌ Please select at least one loader.", ephemeral=True)
            return

        self.result = {
            "type": "specific",
            "loaders": self.selected_loaders
        }

        embed = discord.Embed(
            title="✅ Configuration Complete",
            description=f"Monitoring **{len(self.selected_loaders)} selected loaders**",
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class LoaderSelect(discord.ui.Select):
    def __init__(self, loaders: List[str], parent_view: LoaderView):
        self.parent_view = parent_view

        options = []
        for loader in loaders:
            options.append(discord.SelectOption(
                label=loader.title(),
                value=loader,
                description=f"{loader.title()} mod loader"
            ))

        super().__init__(
            placeholder="Select loaders to monitor...",
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_loaders = self.values.copy()

        # Update continue button
        for item in self.parent_view.children:
            if isinstance(item, discord.ui.Button) and item.label == "Continue":
                item.disabled = len(self.parent_view.selected_loaders) == 0

        await interaction.response.edit_message(view=self.parent_view)


class ReleaseChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.result = None
        self.selected_channels = []

        # Available release channels
        channels = ["release", "beta", "alpha"]

        # Create buttons for each channel
        for channel in channels:
            button = discord.ui.Button(
                label=channel.title(),
                style=discord.ButtonStyle.secondary
            )
            button.callback = self._create_channel_callback(channel)
            self.add_item(button)

        # Add "All Channels" button
        all_btn = discord.ui.Button(
            label="All Channels",
            style=discord.ButtonStyle.primary
        )
        all_btn.callback = self._all_channels_callback
        self.add_item(all_btn)

        # Add custom selection button
        custom_btn = discord.ui.Button(
            label="Custom Selection",
            style=discord.ButtonStyle.secondary
        )
        custom_btn.callback = self._custom_selection_callback
        self.add_item(custom_btn)

    async def _all_channels_callback(self, interaction: discord.Interaction):
        """Select all release channels."""
        self.result = {
            "type": "all",
            "channels": ["release", "beta", "alpha"]
        }

        embed = discord.Embed(
            title="✅ Configuration Complete",
            description="Monitoring **all release channels**",
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    def _create_channel_callback(self, channel: str):
        """Create a callback for a specific channel button."""

        async def callback(interaction: discord.Interaction):
            self.result = {
                "type": "specific",
                "channels": [channel]
            }

            embed = discord.Embed(
                title="✅ Configuration Complete",
                description=f"Monitoring **{channel.title()}** releases only",
                color=discord.Color.green()
            )

            await interaction.response.edit_message(embed=embed, view=None)
            self.stop()

        return callback

    async def _custom_selection_callback(self, interaction: discord.Interaction):
        """Allow custom selection of release channels."""
        embed = discord.Embed(
            title="Select Release Channels",
            description="Select which release channels to monitor:",
            color=discord.Color.blue()
        )

        view = discord.ui.View(timeout=300)

        # Add channel selector
        channel_select = ReleaseChannelSelect(self)
        view.add_item(channel_select)

        # Add continue button
        continue_btn = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.green,
            disabled=True
        )
        continue_btn.callback = self._continue_callback
        view.add_item(continue_btn)

        await interaction.response.edit_message(embed=embed, view=view)

    async def _continue_callback(self, interaction: discord.Interaction):
        """Continue with selected channels."""
        if not self.selected_channels:
            await interaction.response.send_message("❌ Please select at least one channel.", ephemeral=True)
            return

        self.result = {
            "type": "specific",
            "channels": self.selected_channels
        }

        embed = discord.Embed(
            title="✅ Configuration Complete",
            description=f"Monitoring **{len(self.selected_channels)} selected channels**",
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class ReleaseChannelSelect(discord.ui.Select):
    def __init__(self, parent_view: ReleaseChannelView):
        self.parent_view = parent_view

        options = [
            discord.SelectOption(
                label="Release",
                value="release",
                description="Stable releases"
            ),
            discord.SelectOption(
                label="Beta",
                value="beta",
                description="Beta releases"
            ),
            discord.SelectOption(
                label="Alpha",
                value="alpha",
                description="Alpha releases"
            )
        ]

        super().__init__(
            placeholder="Select release channels to monitor...",
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_channels = self.values.copy()

        # Update continue button
        for item in self.parent_view.children:
            if isinstance(item, discord.ui.Button) and item.label == "Continue":
                item.disabled = len(self.parent_view.selected_channels) == 0

        await interaction.response.edit_message(view=self.parent_view)


class ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, callback_func: Callable):
        self.callback_func = callback_func
        super().__init__(
            placeholder="Select a channel...",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text]
        )

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await self.callback_func(interaction, channel)


class RoleSelect(discord.ui.RoleSelect):
    def __init__(self, callback_func: Callable):
        self.callback_func = callback_func
        super().__init__(
            placeholder="Select roles to ping (optional)...",
            min_values=0,
            max_values=10
        )

    async def callback(self, interaction: discord.Interaction):
        roles = self.values
        await self.callback_func(interaction, roles)