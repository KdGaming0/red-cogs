from .watcher import ModrinthWatcher

async def setup(bot):
    """Load the modrinthwatcher cog."""
    cog = ModrinthWatcher(bot)
    await bot.add_cog(cog)