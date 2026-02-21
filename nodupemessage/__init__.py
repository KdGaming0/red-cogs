from .nodupemessage import NoDupeMessage


async def setup(bot):
    await bot.add_cog(NoDupeMessage(bot))