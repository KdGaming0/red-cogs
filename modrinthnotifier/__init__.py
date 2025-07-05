from .modrinthnotifier import ModrinthNotifier

async def setup(bot):
    """Setup function for the cog."""
    await bot.add_cog(ModrinthNotifier(bot))