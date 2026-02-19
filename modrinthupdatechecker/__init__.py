from .modrinthupdatechecker import ModrinthUpdateChecker

async def setup(bot):
    await bot.add_cog(ModrinthUpdateChecker(bot))