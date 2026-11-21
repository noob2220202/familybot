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
| `/포인트지급 @유저네임\|ID 수량` (답장이면 수량만) | 관리자 | 포인트 지급 |
| `/포인트회수 @유저네임\|ID 수량` | 관리자 | 포인트 회수 |
| `/이용권지급`, `/이용권회수` | 관리자 | 룰렛 이용권 지급 · 회수 |
| `/포인트초기화` | 관리자 | 전체 포인트 초기화 (확인 버튼, 5분 뒤 만료) |
| `/랭킹` | 전체 | 채팅 횟수 랭킹 (페이지 넘김) |
| `/경기` | 전체 | 오늘의 경기 일정 · 진행 중 스코어 |
| `/내정보` | 전체 | 포인트 · 이용권 · 채팅 · 출석 |
| `/출석` | 전체 | 일일 출석 보상 |
| `/id` | 전체 | 채팅방 ID 확인 |

## 설치

Python 3.10 이상, Node.js 18 이상(pm2용)이 필요합니다. Ubuntu 22.04 의 apt `nodejs` 는 12 버전이라 [NodeSource](https://github.com/nodesource/distributions) 로 설치합니다.

```bash
sudo apt install -y python3-pip
python3 -m pip install -r requirements.txt    # Ubuntu 23.04+ 는 --break-system-packages 추가
cp .env.example .env && chmod 600 .env         # 값 입력
sudo npm i -g pm2
pm2 start ecosystem.config.js
pm2 install pm2-logrotate                      # 로그 용량 제한
pm2 save && pm2 startup                        # 출력되는 명령을 한 번 실행하면 재부팅 후에도 자동 시작
```

`.env` 를 수정하면 `pm2 restart all` 로 다시 시작해야 반영됩니다.

| 키 | 설명 |
|---|---|
| `BOT_TOKEN` | @BotFather 에서 발급 |
| `GROUP_ID` | 그룹방에서 `/id` 로 확인한 값 (`-100…`). 봇을 먼저 실행해 확인한 뒤 넣고 재시작 |
| `MINIAPP_URL` | 미니앱 주소 |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | 관리자 페이지 로그인 |
| `SPORTS_API_KEY` | [API-Sports](https://dashboard.api-football.com) 키 (축구 · 농구 · 야구 · 배구 공용) |
| `HOST`, `MINIAPP_PORT`, `ADMIN_PORT` | 기본 `127.0.0.1`, `9200`, `9201` |

## 텔레그램 설정

1. 봇을 그룹방에 초대하고 **관리자**로 지정합니다. 관리자가 아니면 한글 명령어와 일반 채팅을 받지 못해 랭킹이 집계되지 않습니다. 공지를 상단 고정하려면 메시지 고정 권한도 줍니다.
2. @BotFather → Bot Settings → Configure Mini App 에서 `MINIAPP_URL` 을 Main Mini App 으로 등록합니다. `/내정보` 의 미니앱 버튼이 이 설정을 사용합니다.

두 설정이 빠져 있으면 봇 시작 시 `pm2 logs familybot-bot` 에 경고가 남습니다.

## HTTPS

텔레그램 미니앱은 HTTPS 주소만 열 수 있습니다.

1. 두 도메인(`jypteam.cloud`, `admin.jypteam.cloud`)의 DNS A 레코드를 서버 IP 로 지정합니다.
2. Caddy 를 설치합니다. Ubuntu 24.04 는 `sudo apt install caddy`, 22.04 는 [Caddy 공식 저장소](https://caddyserver.com/docs/install#debian-ubuntu-raspbian)를 추가한 뒤 설치합니다.
3. `Caddyfile.example` 을 `/etc/caddy/Caddyfile` 로 복사하고 `sudo systemctl reload caddy` 를 실행하면 인증서 발급과 9200 · 9201 프록시가 자동으로 설정됩니다.

방화벽은 SSH 외에 80 · 443 만 열면 됩니다.

## 업데이트

```bash
git pull && python3 -m pip install -r requirements.txt && pm2 restart ecosystem.config.js && pm2 save
```

## 데이터

SQLite 파일 `data/familybot.db` 에 저장됩니다. 실행 중에는 `-wal`, `-shm` 파일이 함께 생기므로 파일만 복사하면 최근 기록이 빠집니다. 백업은 실행 중에도 안전한 아래 명령을 사용합니다.

```bash
python3 -c "import sqlite3; s=sqlite3.connect('data/familybot.db'); d=sqlite3.connect('backup.db'); s.backup(d); d.close()"
```

복원: `pm2 stop all` → `data/familybot.db-wal`, `data/familybot.db-shm` 삭제 → 백업 파일을 `data/familybot.db` 로 복사 → `pm2 start all`
