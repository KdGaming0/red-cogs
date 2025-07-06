import discord
from redbot.core import commands
import logging

log = logging.getLogger("red.modrinth_checker")


class ConfirmView(discord.ui.View):
    def __init__(self, *, timeout=120):
        super().__init__(timeout=timeout)
        self.value = None

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()

    async def on_timeout(self):
        self.value = False
        self.stop()


class MinecraftVersionView(discord.ui.View):
    def __init__(self, available_versions, *, timeout=120):
        super().__init__(timeout=timeout)
        # Handle both list of strings and list of dicts
        if available_versions and isinstance(available_versions[0], str):
            # Convert string list to dict format
            self.available_versions = [{"version_number": v, "snapshot": self._is_snapshot(v)} for v in
                                       available_versions]
        else:
            self.available_versions = available_versions or []

        self.release_versions = [v for v in self.available_versions if not v.get('snapshot', False)]
        self.has_snapshots = any(v.get('snapshot', False) for v in self.available_versions)
        self.result = None
        self.selected_versions = []
        self.specific_mode = False
        self.showing_snapshots = False

        # Get current versions (latest few releases)
        self.current_versions = self.release_versions[:5] if self.release_versions else []

        self._build_main_view()

    def _is_snapshot(self, version: str) -> bool:
        """Check if a version is a snapshot."""
        version_lower = version.lower()
        return any(indicator in version_lower for indicator in ['snapshot', 'alpha', 'beta', 'rc', 'pre', 'w'])

    def _build_main_view(self):
        self.clear_items()

        # All versions button
        all_button = discord.ui.Button(
            label="All supported versions",
            style=discord.ButtonStyle.primary,
            emoji="1️⃣",
            row=0
        )
        all_button.callback = self._all_versions_callback
        self.add_item(all_button)

        # Specific versions button
        specific_button = discord.ui.Button(
            label="Specific versions",
            style=discord.ButtonStyle.primary,
            emoji="2️⃣",
            row=0
        )
        specific_button.callback = self._specific_versions_callback
        self.add_item(specific_button)

        # Latest current button
        latest_current_button = discord.ui.Button(
            label="Latest current version",
            style=discord.ButtonStyle.primary,
            emoji="3️⃣",
            row=1
        )
        latest_current_button.callback = self._latest_current_callback
        self.add_item(latest_current_button)

        # Latest always button
        latest_always_button = discord.ui.Button(
            label="Latest version always",
            style=discord.ButtonStyle.primary,
            emoji="4️⃣",
            row=1
        )
        latest_always_button.callback = self._latest_always_callback
        self.add_item(latest_always_button)

        # Toggle snapshots button if available
        if self.has_snapshots:
            toggle_button = discord.ui.Button(
                label="Show snapshots" if not self.showing_snapshots else "Hide snapshots",
                style=discord.ButtonStyle.secondary,
                row=2
            )
            toggle_button.callback = self._toggle_snapshots_main
            self.add_item(toggle_button)

    def _build_specific_view(self):
        self.clear_items()

        # Version dropdown
        versions_to_show = self.available_versions if self.showing_snapshots else self.release_versions
        if versions_to_show:
            version_select = VersionSelect(self, versions_to_show[:25])  # Discord limit
            self.add_item(version_select)

        # Toggle snapshots button if available
        if self.has_snapshots:
            toggle_button = discord.ui.Button(
                label="Show snapshots" if not self.showing_snapshots else "Hide snapshots",
                style=discord.ButtonStyle.secondary,
                row=1
            )
            toggle_button.callback = self._toggle_snapshots_specific
            self.add_item(toggle_button)

        # Continue button (only if versions selected)
        if self.selected_versions:
            continue_button = discord.ui.Button(
                label="Continue",
                style=discord.ButtonStyle.success,
                row=2
            )
            continue_button.callback = self._continue_specific
            self.add_item(continue_button)

        # Back button
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=2
        )
        back_button.callback = self._back_to_main
        self.add_item(back_button)

    def _create_main_embed(self):
        embed = discord.Embed(
            title="Select Minecraft Versions",
            description="Which Minecraft versions should be monitored?",
            color=0x00ff00
        )

        embed.add_field(
            name="Options",
            value=(
                "1️⃣ **All supported versions** - Monitor all current and future versions\n"
                "2️⃣ **Specific versions** - Choose specific versions to monitor\n"
                "3️⃣ **Latest current version** - Monitor only the current latest version\n"
                "4️⃣ **Latest version always** - Always monitor the newest version"
            ),
            inline=False
        )

        if self.has_snapshots:
            embed.add_field(
                name="Available Versions",
                value=f"**Releases:** {len(self.release_versions)}\n**Snapshots:** {len(self.available_versions) - len(self.release_versions)}",
                inline=False
            )
        else:
            embed.add_field(
                name="Available Versions",
                value=f"**Releases:** {len(self.release_versions)}",
                inline=False
            )

        return embed

    def _create_specific_embed(self):
        embed = discord.Embed(
            title="Select Specific Versions",
            description="Choose which versions to monitor:",
            color=0x00ff00
        )

        if self.selected_versions:
            embed.add_field(
                name="Selected Versions",
                value=", ".join(self.selected_versions),
                inline=False
            )
        else:
            embed.add_field(
                name="Selected Versions",
                value="None selected",
                inline=False
            )

        versions_to_show = self.available_versions if self.showing_snapshots else self.release_versions
        embed.add_field(
            name="Available Versions",
            value=f"Showing {len(versions_to_show)} versions",
            inline=False
        )

        return embed

    async def _all_versions_callback(self, interaction: discord.Interaction):
        self.result = {"type": "all"}
        await interaction.response.edit_message(content="✅ All versions selected!", embed=None, view=None)
        self.stop()

    async def _specific_versions_callback(self, interaction: discord.Interaction):
        self.specific_mode = True
        self._build_specific_view()
        embed = self._create_specific_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _toggle_snapshots_main(self, interaction: discord.Interaction):
        self.showing_snapshots = not self.showing_snapshots
        self._build_main_view()
        embed = self._create_main_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _toggle_snapshots_specific(self, interaction: discord.Interaction):
        self.showing_snapshots = not self.showing_snapshots
        self._build_specific_view()
        embed = self._create_specific_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _continue_specific(self, interaction: discord.Interaction):
        if self.selected_versions:
            self.result = {"type": "specific", "versions": self.selected_versions}
            await interaction.response.edit_message(
                content=f"✅ Selected versions: {', '.join(self.selected_versions)}",
                embed=None,
                view=None
            )
            self.stop()
        else:
            await interaction.response.send_message("Please select at least one version first.", ephemeral=True)

    async def _back_to_main(self, interaction: discord.Interaction):
        self.specific_mode = False
        self.selected_versions = []
        self._build_main_view()
        embed = self._create_main_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _latest_current_callback(self, interaction: discord.Interaction):
        if self.release_versions:
            latest_version = self.release_versions[0]['version_number']
            self.result = {"type": "latest_current", "version": latest_version}
            await interaction.response.edit_message(
                content=f"✅ Latest current version selected: {latest_version}",
                embed=None,
                view=None
            )
            self.stop()
        else:
            await interaction.response.send_message("No release versions available.", ephemeral=True)

    async def _latest_always_callback(self, interaction: discord.Interaction):
        self.result = {"type": "latest_always"}
        await interaction.response.edit_message(content="✅ Latest version always selected!", embed=None, view=None)
        self.stop()

    async def on_timeout(self):
        self.result = None
        self.stop()


class VersionSelect(discord.ui.Select):
    def __init__(self, parent_view, versions):
        self.parent_view = parent_view

        options = []
        for version in versions:
            if isinstance(version, dict):
                version_num = version.get('version_number', 'Unknown')
                is_snapshot = version.get('snapshot', False)
            else:
                version_num = str(version)
                is_snapshot = parent_view._is_snapshot(version_num)

            label = f"{version_num} {'(Snapshot)' if is_snapshot else '(Release)'}"

            options.append(discord.SelectOption(
                label=label,
                value=version_num,
                description=f"Minecraft {version_num}",
                default=version_num in parent_view.selected_versions
            ))

        super().__init__(
            placeholder="Choose versions...",
            min_values=1,
            max_values=min(len(options), 25),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_versions = self.values

        # Update the view to show continue button
        self.parent_view._build_specific_view()
        embed = self.parent_view._create_specific_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class LoaderView(discord.ui.View):
    def __init__(self, available_loaders, *, timeout=120):
        super().__init__(timeout=timeout)
        self.available_loaders = available_loaders or []
        self.result = None
        self.selected_loaders = []
        self.custom_mode = False

        self._build_main_view()

    def _build_main_view(self):
        self.clear_items()

        # All loaders button
        all_button = discord.ui.Button(
            label="All supported loaders",
            style=discord.ButtonStyle.primary,
            emoji="1️⃣",
            row=0
        )
        all_button.callback = self._all_loaders_callback
        self.add_item(all_button)

        # Individual loader buttons
        for i, loader in enumerate(self.available_loaders[:4]):  # Max 4 individual buttons
            button = discord.ui.Button(
                label=f"{loader.title()} only",
                style=discord.ButtonStyle.primary,
                emoji=f"{i + 2}️⃣",
                row=(i // 2) + 1
            )
            button.callback = self._create_loader_callback(loader)
            self.add_item(button)

        # Custom selection button if more than 1 loader
        if len(self.available_loaders) > 1:
            custom_button = discord.ui.Button(
                label="Custom selection",
                style=discord.ButtonStyle.secondary,
                emoji="🔧",
                row=2
            )
            custom_button.callback = self._custom_selection_callback
            self.add_item(custom_button)

    def _build_custom_view(self):
        self.clear_items()

        # Loader dropdown
        if self.available_loaders:
            loader_select = LoaderSelect(self, self.available_loaders)
            self.add_item(loader_select)

        # Continue button (only if loaders selected)
        if self.selected_loaders:
            continue_button = discord.ui.Button(
                label="Continue",
                style=discord.ButtonStyle.success,
                row=1
            )
            continue_button.callback = self._continue_callback
            self.add_item(continue_button)

        # Back button
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=1
        )
        back_button.callback = self._back_to_main
        self.add_item(back_button)

    def _create_main_embed(self):
        embed = discord.Embed(
            title="Select Mod Loaders",
            description="Which mod loaders should be monitored?",
            color=0x00ff00
        )

        options_text = "1️⃣ **All supported loaders** - Monitor all available loaders\n"
        for i, loader in enumerate(self.available_loaders[:4]):
            options_text += f"{i + 2}️⃣ **{loader.title()} only** - Monitor only {loader.title()}\n"

        if len(self.available_loaders) > 1:
            options_text += "🔧 **Custom selection** - Choose specific loaders"

        embed.add_field(
            name="Options",
            value=options_text,
            inline=False
        )

        embed.add_field(
            name="Available Loaders",
            value=", ".join(loader.title() for loader in self.available_loaders),
            inline=False
        )

        return embed

    def _create_custom_embed(self):
        embed = discord.Embed(
            title="Select Custom Loaders",
            description="Choose which loaders to monitor:",
            color=0x00ff00
        )

        if self.selected_loaders:
            embed.add_field(
                name="Selected Loaders",
                value=", ".join(loader.title() for loader in self.selected_loaders),
                inline=False
            )
        else:
            embed.add_field(
                name="Selected Loaders",
                value="None selected",
                inline=False
            )

        embed.add_field(
            name="Available Loaders",
            value=", ".join(loader.title() for loader in self.available_loaders),
            inline=False
        )

        return embed

    async def _all_loaders_callback(self, interaction: discord.Interaction):
        self.result = {"type": "all"}
        await interaction.response.edit_message(content="✅ All loaders selected!", embed=None, view=None)
        self.stop()

    def _create_loader_callback(self, loader):
        async def callback(interaction: discord.Interaction):
            self.result = {"type": "specific", "loaders": [loader]}
            await interaction.response.edit_message(
                content=f"✅ {loader.title()} selected!",
                embed=None,
                view=None
            )
            self.stop()

        return callback

    async def _custom_selection_callback(self, interaction: discord.Interaction):
        self.custom_mode = True
        self._build_custom_view()
        embed = self._create_custom_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _continue_callback(self, interaction: discord.Interaction):
        if self.selected_loaders:
            self.result = {"type": "specific", "loaders": self.selected_loaders}
            await interaction.response.edit_message(
                content=f"✅ Selected loaders: {', '.join(loader.title() for loader in self.selected_loaders)}",
                embed=None,
                view=None
            )
            self.stop()
        else:
            await interaction.response.send_message("Please select at least one loader first.", ephemeral=True)

    async def _back_to_main(self, interaction: discord.Interaction):
        self.custom_mode = False
        self.selected_loaders = []
        self._build_main_view()
        embed = self._create_main_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        self.result = None
        self.stop()


class LoaderSelect(discord.ui.Select):
    def __init__(self, parent_view, loaders):
        self.parent_view = parent_view

        options = []
        for loader in loaders:
            options.append(discord.SelectOption(
                label=loader.title(),
                value=loader,
                description=f"Monitor {loader.title()} versions",
                default=loader in parent_view.selected_loaders
            ))

        super().__init__(
            placeholder="Choose loaders...",
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_loaders = self.values

        # Update the view to show continue button
        self.parent_view._build_custom_view()
        embed = self.parent_view._create_custom_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class ReleaseChannelView(discord.ui.View):
    def __init__(self, *, timeout=120):
        super().__init__(timeout=timeout)
        self.result = None
        self.selected_channels = []
        self.custom_mode = False

        self._build_main_view()

    def _build_main_view(self):
        self.clear_items()

        # All channels button
        all_button = discord.ui.Button(
            label="All Channels",
            style=discord.ButtonStyle.primary,
            emoji="1️⃣",
            row=0
        )
        all_button.callback = self._all_channels_callback
        self.add_item(all_button)

        # Individual channel buttons
        channels = ["release", "beta", "alpha"]
        for i, channel in enumerate(channels):
            button = discord.ui.Button(
                label=f"{channel.title()} channel",
                style=discord.ButtonStyle.primary,
                emoji=f"{i + 2}️⃣",
                row=(i // 2) + 1
            )
            button.callback = self._create_channel_callback(channel)
            self.add_item(button)

        # Custom selection button
        custom_button = discord.ui.Button(
            label="Custom selection",
            style=discord.ButtonStyle.secondary,
            emoji="🔧",
            row=2
        )
        custom_button.callback = self._custom_selection_callback
        self.add_item(custom_button)

    def _build_custom_view(self):
        self.clear_items()

        # Channel dropdown
        channel_select = ReleaseChannelSelect(self)
        self.add_item(channel_select)

        # Continue button (only if channels selected)
        if self.selected_channels:
            continue_button = discord.ui.Button(
                label="Continue",
                style=discord.ButtonStyle.success,
                row=1
            )
            continue_button.callback = self._continue_callback
            self.add_item(continue_button)

        # Back button
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=1
        )
        back_button.callback = self._back_to_main
        self.add_item(back_button)

    def _create_main_embed(self):
        embed = discord.Embed(
            title="Select Release Channels",
            description="Which release channels should be monitored?",
            color=0x00ff00
        )

        embed.add_field(
            name="Options",
            value=(
                "1️⃣ **All Channels** - Monitor all release channels\n"
                "2️⃣ **Release channel** - Monitor only stable releases\n"
                "3️⃣ **Beta channel** - Monitor only beta releases\n"
                "4️⃣ **Alpha channel** - Monitor only alpha releases\n"
                "🔧 **Custom selection** - Choose specific channels"
            ),
            inline=False
        )

        return embed

    def _create_custom_embed(self):
        embed = discord.Embed(
            title="Select Custom Release Channels",
            description="Choose which release channels to monitor:",
            color=0x00ff00
        )

        if self.selected_channels:
            embed.add_field(
                name="Selected Channels",
                value=", ".join(channel.title() for channel in self.selected_channels),
                inline=False
            )
        else:
            embed.add_field(
                name="Selected Channels",
                value="None selected",
                inline=False
            )

        embed.add_field(
            name="Available Channels",
            value="Release, Beta, Alpha",
            inline=False
        )

        return embed

    async def _all_channels_callback(self, interaction: discord.Interaction):
        self.result = {"type": "all"}
        await interaction.response.edit_message(content="✅ All channels selected!", embed=None, view=None)
        self.stop()

    def _create_channel_callback(self, channel):
        async def callback(interaction: discord.Interaction):
            self.result = {"type": "specific", "channels": [channel]}
            await interaction.response.edit_message(
                content=f"✅ {channel.title()} channel selected!",
                embed=None,
                view=None
            )
            self.stop()

        return callback

    async def _custom_selection_callback(self, interaction: discord.Interaction):
        self.custom_mode = True
        self._build_custom_view()
        embed = self._create_custom_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _continue_callback(self, interaction: discord.Interaction):
        if self.selected_channels:
            self.result = {"type": "specific", "channels": self.selected_channels}
            await interaction.response.edit_message(
                content=f"✅ Selected channels: {', '.join(channel.title() for channel in self.selected_channels)}",
                embed=None,
                view=None
            )
            self.stop()
        else:
            await interaction.response.send_message("Please select at least one channel first.", ephemeral=True)

    async def _back_to_main(self, interaction: discord.Interaction):
        self.custom_mode = False
        self.selected_channels = []
        self._build_main_view()
        embed = self._create_main_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        self.result = None
        self.stop()


class ReleaseChannelSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view

        channels = ["release", "beta", "alpha"]
        options = []
        for channel in channels:
            options.append(discord.SelectOption(
                label=channel.title(),
                value=channel,
                description=f"Monitor {channel} versions",
                default=channel in parent_view.selected_channels
            ))

        super().__init__(
            placeholder="Choose channels...",
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_channels = self.values

        # Update the view to show continue button
        self.parent_view._build_custom_view()
        embed = self.parent_view._create_custom_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class ChannelSelect(discord.ui.Select):
    def __init__(self, channels, callback_func):
        self.callback_func = callback_func

        options = []
        for channel in channels:
            options.append(discord.SelectOption(
                label=f"#{channel.name}",
                value=str(channel.id),
                description=f"Channel: {channel.name}"
            ))

        super().__init__(
            placeholder="Choose a channel...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction, self.values[0])


class RoleSelect(discord.ui.Select):
    def __init__(self, roles, callback_func):
        self.callback_func = callback_func

        options = []
        for role in roles:
            options.append(discord.SelectOption(
                label=f"@{role.name}",
                value=str(role.id),
                description=f"Role: {role.name}"
            ))

        super().__init__(
            placeholder="Choose roles to ping...",
            min_values=0,
            max_values=min(len(options), 25),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction, self.values)