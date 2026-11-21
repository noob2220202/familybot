# familybot

텔레그램 그룹방 봇 · 미니앱 · 관리자 페이지

| 프로세스 | 실행 | 포트 |
|---|---|---|
| 봇 | `python3 -m app.bot` | – |
| 미니앱 | `python3 -m app.miniapp` | 9200 |
| 관리자 페이지 | `python3 -m app.admin` | 9201 |

## 명령어

| 명령어 | 권한 | 설명 |
|---|---|---|
| `/포인트지급 @유저\|유저ID 숫자` (답장이면 숫자만) | 관리자 | 포인트 지급 |
| `/포인트회수 @유저\|유저ID 숫자` | 관리자 | 포인트 회수 |
| `/이용권지급`, `/이용권회수` | 관리자 | 룰렛 이용권 지급 · 회수 |
| `/포인트초기화` | 관리자 | 전체 포인트 초기화 (확인 버튼) |
| `/랭킹` | 전체 | 채팅 횟수 랭킹 (페이지 넘김) |
| `/경기` | 전체 | 오늘의 경기 일정 · 진행 중 스코어 |
| `/내정보` | 전체 | 포인트 · 이용권 · 채팅 · 출석 |
| `/출석` | 전체 | 일일 출석 보상 |
| `/id` | 전체 | 채팅방 ID 확인 |

## 설치

Python 3.10 이상, Node.js(pm2용)가 필요합니다.

```bash
pip install -r requirements.txt      # Ubuntu 23.04+ 는 --break-system-packages 추가
cp .env.example .env                 # 값 입력
npm i -g pm2
pm2 start ecosystem.config.js
pm2 save && pm2 startup              # 출력되는 명령을 한 번 실행하면 재부팅 후에도 자동 시작
```

`.env`

| 키 | 설명 |
|---|---|
| `BOT_TOKEN` | @BotFather 에서 발급 |
| `GROUP_ID` | 그룹방에서 `/id` 로 확인한 값 (`-100…`) |
| `MINIAPP_URL`, `ADMIN_URL` | 미니앱 · 관리자 페이지 주소 |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | 관리자 페이지 로그인 |
| `SPORTS_API_KEY` | [API-Sports](https://dashboard.api-football.com) 키 (축구 · 농구 · 야구 · 배구 공용) |
| `HOST`, `MINIAPP_PORT`, `ADMIN_PORT` | 기본 `127.0.0.1`, `9200`, `9201` |

## 텔레그램 설정

1. 봇을 그룹방에 초대하고 **관리자**로 지정합니다. 관리자가 아니면 일반 채팅을 받지 못해 랭킹이 집계되지 않습니다.
2. @BotFather → Bot Settings → Configure Mini App 에서 `MINIAPP_URL` 을 Main Mini App 으로 등록합니다. `/내정보` 의 미니앱 버튼이 이 설정을 사용합니다.

## HTTPS

텔레그램 미니앱은 HTTPS 주소만 열 수 있습니다. `Caddyfile.example` 을 `/etc/caddy/Caddyfile` 로 복사하면 인증서 발급과 9200 · 9201 프록시가 자동으로 설정됩니다. 서버 방화벽은 80 · 443 만 열면 됩니다.

## 데이터

SQLite 파일 `data/familybot.db` 하나에 저장됩니다. 백업은 이 파일을 복사하면 됩니다.
