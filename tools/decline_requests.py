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


async def pick(client: TelegramClient):
    groups = [d async for d in client.iter_dialogs() if d.is_group]
    match = next((d for d in groups if d.id == config.GROUP_ID), None)
    if match:
        print(f"그룹: {match.name}")
        return match.input_entity
    print(f"GROUP_ID({config.GROUP_ID}) 그룹을 이 계정에서 찾지 못했습니다. 이 계정이 속한 그룹:")
    for i, d in enumerate(groups, 1):
        print(f"  {i}. {d.name}  ({d.id})")
    if not groups:
        raise SystemExit("이 계정이 속한 그룹이 없습니다. 그룹 관리자 계정으로 로그인하세요.")
    choice = input("번호 선택 (취소는 Enter): ").strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(groups):
        raise SystemExit("취소했습니다.")
    return groups[int(choice) - 1].input_entity


async def main():
    api_id = int(input("api_id: ").strip())
    api_hash = getpass("api_hash: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    try:
        peer = await pick(client)
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
