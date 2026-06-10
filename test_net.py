import aiohttp
from aiohttp_socks import ProxyConnector
import asyncio

async def check():
    print("Пытаюсь подключиться к Telegram через прокси...")
    try:
        connector = ProxyConnector.from_url("http://127.0.0.1:10801")
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get('https://api.telegram.org') as resp:
                print(f"✅ Ура! Связь есть! Статус: {resp.status}")
    except Exception as e:
        print(f"❌ Связи нет. Ошибка: {e}")

asyncio.run(check())