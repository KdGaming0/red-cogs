from .projecttracker import ProjectTracker

async def setup(bot):
    """Load the projecttracker cog."""
    cog = ProjectTracker(bot)
    await bot.add_cog(cog)