from .modrinth_checker import ModrinthChecker

async def setup(bot):
    await bot.add_cog(ModrinthChecker(bot))