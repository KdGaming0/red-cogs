from .cog import ModrinthChecker

async def setup(bot):
    await bot.add_cog(ModrinthChecker(bot))