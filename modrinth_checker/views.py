import discord
from discord.ext import commands
from typing import List, Optional, Dict, Any, Callable
import asyncio


class ConfirmView(discord.ui.View):
    """Simple confirmation view with Yes/No buttons."""

    def __init__(self, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.value: Optional[bool] = None

    @discord.ui.button(label='✅ Confirm', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.edit_message(content="✅ Confirmed!", view=None)
        self.stop()

    @discord.ui.button(label='❌ Cancel', style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.edit_message(content="❌ Cancelled!", view=None)
        self.stop()

    async def on_timeout(self):
        self.value = False
        self.stop()


class MinecraftVersionView(discord.ui.View):
    """View for selecting Minecraft version monitoring options."""

    def __init__(self, available_versions: List[str], has_snapshots: bool = False, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.available_versions = available_versions
        self.release_versions = [v for v in available_versions if not self._is_snapshot(v)]
        self.has_snapshots = has_snapshots
        self.result: Optional[Dict[str, Any]] = None
        self.selected_versions: List[str] = []
        self.specific_mode = False
        self.showing_snapshots = False

    def _is_snapshot(self, version: str) -> bool:
        """Check if version is a snapshot."""
        snapshot_indicators = ['snapshot', 'snap', 'pre', 'rc', 'alpha', 'beta', 'dev', 'experimental', 'test']
        return any(indicator in version.lower() for indicator in snapshot_indicators)

    @discord.ui.button(label='1️⃣ All supported versions', style=discord.ButtonStyle.primary, row=0)
    async def all_versions_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = {
            "type": "all",
            "versions": self.available_versions
        }
        await interaction.response.edit_message(
            content="✅ Selected: Monitor all supported versions",
            view=None
        )
        self.stop()

    @discord.ui.button(label='2️⃣ Specific versions', style=discord.ButtonStyle.secondary, row=0)
    async def specific_versions(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.specific_mode = True
        versions_to_show = self.available_versions if self.showing_snapshots else self.release_versions

        if not versions_to_show:
            await interaction.response.send_message("No versions available to select.", ephemeral=True)
            return

        # Create dropdown for version selection
        select = VersionSelect(versions_to_show[:25], self)  # Discord limit of 25 options
        view = discord.ui.View()
        view.add_item(select)

        # Add snapshot toggle if available
        if self.has_snapshots:
            toggle_btn = discord.ui.Button(
                label=f"{'Hide' if self.showing_snapshots else 'Show'} Snapshots",
                style=discord.ButtonStyle.secondary
            )
            toggle_btn.callback = self._toggle_snapshots
            view.add_item(toggle_btn)

        # Add continue button
        continue_btn = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.green,
            disabled=len(self.selected_versions) == 0
        )
        continue_btn.callback = self._continue_specific
        view.add_item(continue_btn)

        await interaction.response.edit_message(
            content=f"Select specific versions to monitor:\n**Selected:** {', '.join(self.selected_versions) if self.selected_versions else 'None'}",
            view=view
        )

    async def _toggle_snapshots(self, interaction: discord.Interaction):
        """Toggle snapshot visibility."""
        self.showing_snapshots = not self.showing_snapshots
        await self.specific_versions(interaction, None)

    async def _continue_specific(self, interaction: discord.Interaction):
        """Continue with selected specific versions."""
        if not self.selected_versions:
            await interaction.response.send_message("Please select at least one version.", ephemeral=True)
            return

        self.result = {
            "type": "specific",
            "versions": self.selected_versions
        }
        await interaction.response.edit_message(
            content=f"✅ Selected specific versions: {', '.join(self.selected_versions)}",
            view=None
        )
        self.stop()

    @discord.ui.button(label='3️⃣ Latest current version', style=discord.ButtonStyle.secondary, row=1)
    async def latest_current(self, interaction: discord.Interaction, button: discord.ui.Button):
        latest_version = self.release_versions[0] if self.release_versions else self.available_versions[0]
        self.result = {
            "type": "latest_current",
            "versions": [latest_version]
        }
        await interaction.response.edit_message(
            content=f"✅ Selected: Monitor latest current version ({latest_version})",
            view=None
        )
        self.stop()

    @discord.ui.button(label='4️⃣ Latest version always', style=discord.ButtonStyle.secondary, row=1)
    async def latest_always(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = {
            "type": "latest_always",
            "versions": []  # Will be determined dynamically
        }
        await interaction.response.edit_message(
            content="✅ Selected: Monitor latest version always (dynamic)",
            view=None
        )
        self.stop()

    async def on_timeout(self):
        self.result = None
        self.stop()


class VersionSelect(discord.ui.Select):
    """Select menu for choosing specific versions."""

    def __init__(self, versions: List[str], parent_view: MinecraftVersionView):
        self.parent_view = parent_view

        options = []
        for version in versions:
            options.append(discord.SelectOption(
                label=version,
                value=version,
                default=version in parent_view.selected_versions
            ))

        super().__init__(
            placeholder="Choose versions to monitor...",
            options=options,
            max_values=len(options),
            min_values=0
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_versions = self.values

        # Update the message
        await interaction.response.edit_message(
            content=f"Select specific versions to monitor:\n**Selected:** {', '.join(self.values) if self.values else 'None'}",
            view=self.view
        )

        # Enable/disable continue button
        for item in self.view.children:
            if isinstance(item, discord.ui.Button) and item.label == "Continue":
                item.disabled = len(self.values) == 0
                break


class LoaderView(discord.ui.View):
    """View for selecting loader monitoring options."""

    def __init__(self, available_loaders: List[str], timeout: int = 120):
        super().__init__(timeout=timeout)
        self.available_loaders = available_loaders
        self.result: Optional[Dict[str, Any]] = None
        self.selected_loaders: List[str] = []

        # Add all loaders button
        all_btn = discord.ui.Button(
            label="1️⃣ All supported loaders",
            style=discord.ButtonStyle.primary,
            row=0
        )
        all_btn.callback = self._all_loaders_callback
        self.add_item(all_btn)

        # Add individual loader buttons
        for i, loader in enumerate(available_loaders):
            if i < 4:  # Max 4 individual loader buttons
                btn = discord.ui.Button(
                    label=f"{i + 2}️⃣ {loader.title()} only",
                    style=discord.ButtonStyle.secondary,
                    row=i // 2 + 1
                )
                btn.callback = self._create_loader_callback(loader)
                self.add_item(btn)

        # Add custom selection button if more than 4 loaders
        if len(available_loaders) > 4 or len(available_loaders) > 1:
            custom_btn = discord.ui.Button(
                label="🔧 Custom selection",
                style=discord.ButtonStyle.secondary,
                row=3
            )
            custom_btn.callback = self._custom_selection_callback
            self.add_item(custom_btn)

    async def _all_loaders_callback(self, interaction: discord.Interaction):
        self.result = {
            "type": "all",
            "loaders": self.available_loaders
        }
        await interaction.response.edit_message(
            content="✅ Selected: Monitor all supported loaders",
            view=None
        )
        self.stop()

    def _create_loader_callback(self, loader: str) -> Callable:
        async def callback(interaction: discord.Interaction):
            self.result = {
                "type": "specific",
                "loaders": [loader]
            }
            await interaction.response.edit_message(
                content=f"✅ Selected: Monitor {loader.title()} only",
                view=None
            )
            self.stop()

        return callback

    async def _custom_selection_callback(self, interaction: discord.Interaction):
        # Create multi-select dropdown
        select = LoaderSelect(self.available_loaders, self)
        view = discord.ui.View()
        view.add_item(select)

        continue_btn = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.green,
            disabled=True
        )
        continue_btn.callback = self._continue_callback
        view.add_item(continue_btn)

        await interaction.response.edit_message(
            content="Select loaders to monitor:\n**Selected:** None",
            view=view
        )

    async def _continue_callback(self, interaction: discord.Interaction):
        if not self.selected_loaders:
            await interaction.response.send_message("Please select at least one loader.", ephemeral=True)
            return

        self.result = {
            "type": "specific",
            "loaders": self.selected_loaders
        }
        await interaction.response.edit_message(
            content=f"✅ Selected loaders: {', '.join(self.selected_loaders)}",
            view=None
        )
        self.stop()

    async def on_timeout(self):
        self.result = None
        self.stop()


class LoaderSelect(discord.ui.Select):
    """Select menu for choosing specific loaders."""

    def __init__(self, loaders: List[str], parent_view: LoaderView):
        self.parent_view = parent_view

        options = []
        for loader in loaders:
            options.append(discord.SelectOption(
                label=loader.title(),
                value=loader,
                description=f"Monitor {loader} versions"
            ))

        super().__init__(
            placeholder="Choose loaders to monitor...",
            options=options,
            max_values=len(options),
            min_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_loaders = self.values

        # Update the message
        await interaction.response.edit_message(
            content=f"Select loaders to monitor:\n**Selected:** {', '.join(self.values)}",
            view=self.view
        )

        # Enable continue button
        for item in self.view.children:
            if isinstance(item, discord.ui.Button) and item.label == "Continue":
                item.disabled = False
                break


class ReleaseChannelView(discord.ui.View):
    """View for selecting release channel monitoring options."""

    def __init__(self, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.result: Optional[Dict[str, Any]] = None
        self.selected_channels: List[str] = []

        # Add all channels button
        all_btn = discord.ui.Button(
            label="1️⃣ All Channels",
            style=discord.ButtonStyle.primary,
            row=0
        )
        all_btn.callback = self._all_channels_callback
        self.add_item(all_btn)

        # Add individual channel buttons
        channels = [
            ("2️⃣ Release channel", "release"),
            ("3️⃣ Beta channel", "beta"),
            ("4️⃣ Alpha channel", "alpha")
        ]

        for i, (label, channel) in enumerate(channels):
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                row=i // 2 + 1
            )
            btn.callback = self._create_channel_callback(channel)
            self.add_item(btn)

        # Add custom selection button
        custom_btn = discord.ui.Button(
            label="🔧 Custom selection",
            style=discord.ButtonStyle.secondary,
            row=2
        )
        custom_btn.callback = self._custom_selection_callback
        self.add_item(custom_btn)

    async def _all_channels_callback(self, interaction: discord.Interaction):
        self.result = {
            "type": "all",
            "channels": ["release", "beta", "alpha"]
        }
        await interaction.response.edit_message(
            content="✅ Selected: Monitor all release channels",
            view=None
        )
        self.stop()

    def _create_channel_callback(self, channel: str) -> Callable:
        async def callback(interaction: discord.Interaction):
            self.result = {
                "type": "specific",
                "channels": [channel]
            }
            await interaction.response.edit_message(
                content=f"✅ Selected: Monitor {channel.title()} channel only",
                view=None
            )
            self.stop()

        return callback

    async def _custom_selection_callback(self, interaction: discord.Interaction):
        # Create multi-select dropdown
        select = ReleaseChannelSelect(self)
        view = discord.ui.View()
        view.add_item(select)

        continue_btn = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.green,
            disabled=True
        )
        continue_btn.callback = self._continue_callback
        view.add_item(continue_btn)

        await interaction.response.edit_message(
            content="Select release channels to monitor:\n**Selected:** None",
            view=view
        )

    async def _continue_callback(self, interaction: discord.Interaction):
        if not self.selected_channels:
            await interaction.response.send_message("Please select at least one channel.", ephemeral=True)
            return

        self.result = {
            "type": "specific",
            "channels": self.selected_channels
        }
        await interaction.response.edit_message(
            content=f"✅ Selected channels: {', '.join(self.selected_channels)}",
            view=None
        )
        self.stop()

    async def on_timeout(self):
        self.result = None
        self.stop()


class ReleaseChannelSelect(discord.ui.Select):
    """Select menu for choosing specific release channels."""

    def __init__(self, parent_view: ReleaseChannelView):
        self.parent_view = parent_view

        options = [
            discord.SelectOption(
                label="Release",
                value="release",
                description="Stable releases",
                emoji="📦"
            ),
            discord.SelectOption(
                label="Beta",
                value="beta",
                description="Beta releases",
                emoji="🔶"
            ),
            discord.SelectOption(
                label="Alpha",
                value="alpha",
                description="Alpha releases",
                emoji="🔴"
            )
        ]

        super().__init__(
            placeholder="Choose release channels to monitor...",
            options=options,
            max_values=3,
            min_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_channels = self.values

        # Update the message
        await interaction.response.edit_message(
            content=f"Select release channels to monitor:\n**Selected:** {', '.join(self.values)}",
            view=self.view
        )

        # Enable continue button
        for item in self.view.children:
            if isinstance(item, discord.ui.Button) and item.label == "Continue":
                item.disabled = False
                break


class ChannelSelect(discord.ui.ChannelSelect):
    """Channel select menu for choosing notification channel."""

    def __init__(self, callback_func: Callable):
        self.callback_func = callback_func
        super().__init__(
            placeholder="Select a channel for notifications...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news]
        )

    async def callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction, self.values[0])


class RoleSelect(discord.ui.RoleSelect):
    """Role select menu for choosing notification roles."""

    def __init__(self, callback_func: Callable):
        self.callback_func = callback_func
        super().__init__(
            placeholder="Select roles to ping (optional)...",
            max_values=10
        )

    async def callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction, self.values)