# codex-discord

Discord slash command로 Codex CLI를 감싸는 개인용 최소 wrapper입니다. 현재 구조는 실제 git worktree 기반입니다. `main` bot은 `main/` checkout에서 실행되고, `staging` bot은 `staging/` checkout에서 실행됩니다. Codex 작업 대상은 항상 컨테이너 루트 아래의 `staging/`만 허용되므로, 운영 중인 main checkout을 Codex가 직접 수정하지 않게 막습니다.

## 핵심 동작

- Discord 채널 하나를 하나의 workspace에 매핑
- 해당 채널의 각 thread를 하나의 작업 세션으로 사용
- `/ask`를 thread 안에서 실행하면, 매핑된 workspace에서 `codex exec`를 subprocess로 실행
- 출력은 Discord에 다시 올리고, 길면 여러 메시지 또는 첨부 파일로 전송
- 같은 thread에서는 lock으로 동시 실행 방지

## 포함 명령

공통 명령:

- `/ping`: 봇 생존 확인
- `/ask prompt:...`: 현재 thread 문맥과 함께 Codex 실행
- `/status`: 현재 bot 역할, checkout, 최근 실행 상태, 작업 중 여부, git 정보 출력
- `/diff`: 현재 thread workspace 또는 현재 checkout의 변경 파일 목록과 diff stat 출력

`main` bot 전용 명령:

- `/restart_staging`: staging bot 서비스 재시작
- `/deploy`: 외부 배포 스크립트 실행

`staging` bot은 `restart_staging`, `deploy`를 slash command로 등록하지 않습니다.

## 요구 환경

- Ubuntu 22.04
- Python 3.11 이상 권장
- `codex` CLI 설치 완료
- Discord application에서 bot과 application commands 사용 가능
- 필요한 경우 `MESSAGE CONTENT INTENT` 활성화

## 파일 구성

- `app.py`: slash command 기반 봇 엔트리포인트
- `requirements.txt`: Python 의존성
- `.env.example`: 환경 변수 예시
- `config/config.example.json`: 채널 매핑 및 실행 옵션 예시
- `systemd/codex-discord-main.service`: main bot 서비스 예시
- `systemd/codex-discord-staging.service`: staging bot 서비스 예시
- `scripts/deploy-prod.sh`: main bot이 호출하는 단순 배포 예시 스크립트

## 설치

서버 또는 로컬에 다음 구조가 있다고 가정합니다.

- `/srv/codex-discord/main`
- `/srv/codex-discord/staging`

각 checkout에서 공통으로:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p config
cp config/config.example.json config/config.json
```

그 다음 `prod/.env`와 `dev/.env`를 각각 역할에 맞게 수정합니다.

## .env 설정

staging 예시:

```env
DISCORD_BOT_TOKEN=your_staging_bot_token
DISCORD_ALLOWED_USER_ID=123456789012345678
BOT_ROLE=staging
CODEX_DISCORD_CONFIG=config/config.json
DISCORD_GUILD_ID=123456789012345678
LOG_LEVEL=INFO
```

main 예시:

```env
DISCORD_BOT_TOKEN=your_main_bot_token
DISCORD_ALLOWED_USER_ID=123456789012345678
BOT_ROLE=main
CODEX_DISCORD_CONFIG=config/config.json
DISCORD_GUILD_ID=123456789012345678
LOG_LEVEL=INFO
```

의미:

- `DISCORD_ALLOWED_USER_ID`: 명령 실행을 허용할 단일 Discord user id
- `BOT_ROLE`: `main` 또는 `staging`
- `CODEX_DISCORD_CONFIG`: 현재 checkout 기준 설정 파일 경로. 기본값은 `config/config.json`
- `DISCORD_GUILD_ID`: 지정하면 해당 guild에만 slash command를 빠르게 sync

`CHECKOUT_PATH`와 `DEV_WORKSPACE_ROOT`는 env에서 받지 않습니다.

- 현재 실행 중인 `app.py` 위치가 자동으로 현재 checkout 경로가 됩니다.
- 그 checkout의 상위 디렉터리 아래 `staging/`를 개발 workspace root로 자동 사용합니다.

## config/config.json 설정

```json
{
  "channels": {
    "111111111111111111": ".",
    "222222222222222222": "subproject"
  },
  "timeout_seconds": 900,
  "history_messages": 20,
  "max_prompt_chars": 12000,
  "codex_bin": "codex",
  "codex_exec_args": [
    "--model",
    "gpt-5.4",
    "--skip-git-repo-check",
    "--color",
    "never"
  ],
  "main_commands": {
    "restart_staging": [
      "/usr/bin/systemctl",
      "restart",
      "codex-discord-staging"
    ],
    "deploy": [
      "./scripts/deploy-prod.sh"
    ]
  }
}
```

의미:

- `channels`: 부모 채널 ID -> workspace 경로 매핑
- `codex_bin`: Codex 바이너리 경로 또는 이름
- `codex_exec_args`: `codex exec` 뒤에 붙는 추가 인자 배열
- `main_commands.restart_staging`: `main` bot이 `/restart_staging`에서 실행할 argv 배열
- `main_commands.deploy`: `main` bot이 `/deploy`에서 실행할 argv 배열

중요:

- `channels`는 절대경로를 써도 되고, 상대경로를 쓰면 자동으로 `staging/` 기준으로 해석됩니다.
- `.`는 `staging/` 자체를 의미합니다.
- 모든 workspace는 최종적으로 `staging/` 아래여야 합니다.
- 그래서 `main` bot이 main checkout에서 실행되더라도 `/ask`는 staging workspace만 수정합니다.

## 실행

staging:

```bash
cd /srv/codex-discord/staging
. .venv/bin/activate
python app.py
```

main:

```bash
cd /srv/codex-discord/main
. .venv/bin/activate
python app.py
```

## systemd

예시 서비스 파일:

- `systemd/codex-discord-main.service`
- `systemd/codex-discord-staging.service`

설치 예:

```bash
sudo cp /srv/codex-discord/main/systemd/codex-discord-main.service /etc/systemd/system/
sudo cp /srv/codex-discord/staging/systemd/codex-discord-staging.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-discord-main
sudo systemctl enable --now codex-discord-staging
```

## 배포 스크립트

`scripts/deploy-prod.sh`는 git 기반 예시입니다.

- `prod` checkout에서 `staging` 브랜치를 `main`에 merge
- merge가 성공하면 prod 서비스 재시작

실제 운영 전에 다음은 반드시 검토해야 합니다.

- merge 정책
- 서비스 이름
- 권한과 sudo/systemd 정책

## 테스트 방법

1. staging checkout에서 `.env`와 `config/config.json`을 채웁니다.
2. `python app.py`로 staging bot을 올립니다.
3. Discord에서 매핑된 채널 안에 thread를 만든 뒤 `/ping` 실행합니다.
4. 같은 thread에서 `/ask prompt:"현재 디렉터리 구조를 설명해줘"`를 실행합니다.
5. `/status`로 현재 역할과 최근 실행 상태를 확인합니다.
6. workspace에 변경이 있으면 `/diff`로 변경 파일과 diff stat을 확인합니다.
7. staging에서 커밋을 하나 만든 뒤 main bot에서 `/deploy`를 실행합니다.
8. main bot에서는 `/restart_staging`, `/deploy`가 보이고, staging bot에서는 보이지 않는지 확인합니다.

## 구현 제한

- OpenAI API 직접 호출 없음
- `shell=True` 미사용
- subprocess argv는 전부 리스트로 전달
- DB 없음
- 웹 UI 없음
- 다중 사용자 권한 시스템 없음
- 자동 merge, 자동 rollback 없음
