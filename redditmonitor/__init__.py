from .redditmonitor import RedditMonitor


async def setup(bot):
    await bot.add_cog(RedditMonitor(bot))