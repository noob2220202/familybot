import asyncio
from getpass import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetChatInviteImportersRequest, HideAllChatJoinRequestsRequest
from telethon.tl.types import InputUserEmpty

from app import config


async def pending(client: TelegramClient, peer) -> int:
    r = await client(
        GetChatInviteImportersRequest(peer=peer, offset_date=None, offset_user=InputUserEmpty(), limit=1, requested=True)
    )
    return r.count


async def main():
    if not config.GROUP_ID:
        raise SystemExit(".env 의 GROUP_ID 를 먼저 설정하세요.")
    api_id = int(input("api_id: ").strip())
    api_hash = getpass("api_hash: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    try:
        await client.get_dialogs()
        peer = await client.get_input_entity(config.GROUP_ID)
        n = await pending(client, peer)
        print(f"대기 중인 입장 요청: {n:,}건")
        if not n or input("전부 거절할까요? (y/N): ").strip().lower() != "y":
            return
        for _ in range(20):
            await client(HideAllChatJoinRequestsRequest(peer=peer, approved=False))
            left = await pending(client, peer)
            if not left:
                break
        print(f"거절 완료 · 남은 요청 {left:,}건")
    finally:
        await client.log_out()


if __name__ == "__main__":
    asyncio.run(main())
