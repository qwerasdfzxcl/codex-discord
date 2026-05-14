# codex-discord

Discord에서 Codex CLI를 쓰기 위한 최소 wrapper다.

기본 사용 흐름은 단순하다.

- Discord 채널 하나를 하나의 workspace에 연결한다.
- `/new-session`으로 작업 thread를 만든다.
- thread 안에서 일반 메시지를 보내면 Codex가 같은 세션을 이어서 작업한다.
- 필요하면 `/diff`, `/status`, `/break` 같은 명령으로 상태를 확인한다.

이 README는 `main` bot 하나만 운영하는 기준으로 설명한다. `staging` bot은 선택 사항이다.

## 주요 기능

- 채널별 workspace 매핑
- thread별 Codex session 유지
- Discord 메시지를 Codex turn으로 전달
- 긴 출력은 여러 메시지나 파일로 분할 전송
- Codex 승인 요청을 Discord 버튼으로 처리
- `/new-repo`로 새 repository와 채널 생성
- `/deploy`, `/restart` 같은 운영 명령 지원

## 기본 사용법

일반적인 흐름은 이렇다.

1. 관리용 채널 하나를 workspace에 연결한다.
2. `/new-repo`로 새 repository와 대응 채널을 만든다.
3. 해당 채널에서 `/new-session`으로 작업 thread를 만든다.
4. thread 안에서 자연어로 요청한다.
5. 필요하면 `/status`, `/diff`, `/break`를 사용한다.

예:

1. `/new-repo name:todo-list`
2. 생성된 `#todo-list` 채널로 이동
3. `/new-session title:initial-setup danger:off`
4. thread 안에서 `FastAPI로 TODO CRUD 골격 만들어줘` 전송

## 주요 명령

`main` bot 기준:

- `/ping`: 봇 상태 확인
- `/new-repo`: 새 Git repository와 채널 생성
- `/delete-repo`: 현재 채널의 repository와 채널 삭제
- `/new-session`: 새 작업 thread 생성
- `/status`: 현재 thread의 상태 확인
- `/diff`: 현재 workspace 변경 사항 확인
- `/break`: 실행 중인 작업 중단
- `/restart`: main bot 서비스 재시작
- `/restart-staging`: staging bot 서비스 재시작
- `/deploy`: 배포 스크립트 실행

보통 사용자는 `/new-repo`, `/new-session`, thread 메시지, `/status`, `/diff` 정도만 쓰면 된다.

## 요구 환경

- Ubuntu 22.04 이상 권장
- Python 3.11 이상 권장
- `codex` CLI 설치 완료
- Discord application과 application commands 사용 가능
- Discord developer portal에서 `MESSAGE CONTENT INTENT` 활성화
- thread 이름 변경 권한
- `new-repo`에서 GitHub clone을 쓰려면 `github.com` 접근 가능

## 설치

예시 경로:

- `/srv/codex-discord/main`

설치:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p config
cp config/config.example.json config/config.json
```

## .env 설정

예시:

```env
DISCORD_BOT_TOKEN=your_main_bot_token
DISCORD_ALLOWED_USER_ID=123456789012345678
BOT_ROLE=main
CODEX_DISCORD_CONFIG=config/config.json
DISCORD_GUILD_ID=123456789012345678
LOG_LEVEL=INFO
CODEX_DISCORD_DEVELOPER_INSTRUCTIONS_FILE=
```

의미:

- `DISCORD_BOT_TOKEN`: Discord bot token
- `DISCORD_ALLOWED_USER_ID`: 명령 실행을 허용할 단일 Discord user id
- `BOT_ROLE`: `main`
- `CODEX_DISCORD_CONFIG`: 설정 파일 경로. 기본값은 `config/config.json`
- `DISCORD_GUILD_ID`: 지정하면 해당 guild에만 slash command를 빠르게 sync
- `CODEX_DISCORD_DEVELOPER_INSTRUCTIONS_FILE`: 선택값. 지정하면 해당 파일 내용을 Codex app-server의 developer instructions로 전달

## config/config.json 설정

예시:

```json
{
  "channels": {
    "111111111111111111": "/srv/workspaces/example-repo"
  },
  "timeout_seconds": 900,
  "history_messages": 20,
  "max_prompt_chars": 12000,
  "codex_bin": "codex",
  "codex_global_args": [
    "-a",
    "on-request"
  ],
  "codex_exec_args": [
    "--model",
    "gpt-5.4",
    "--skip-git-repo-check",
    "--color",
    "never"
  ],
  "main_commands": {
    "restart": [
      "./scripts/systemd-restart-service.sh",
      "codex-discord-main"
    ],
    "restart_staging": [
      "./scripts/systemd-restart-service.sh",
      "codex-discord-staging"
    ],
    "deploy": [
      "./scripts/deploy-prod.sh"
    ]
  }
}
```

주요 항목:

- `channels`: 부모 채널 ID -> workspace 경로 매핑
- `timeout_seconds`: Codex 실행 timeout
- `codex_bin`: Codex 바이너리 경로
- `codex_global_args`: 전역 Codex 인자
- `codex_exec_args`: `codex exec` 추가 인자
- `main_commands`: 운영 명령에서 실행할 argv 배열

운영 참고:

- `channels`는 부모 채널 기준으로 연결된다.
- 절대경로를 권장한다.
- 상대경로는 `config.json` 기준으로 해석된다.
- `codex_global_args`에 `["-a", "on-request"]`를 두면 Discord 승인 버튼 흐름을 쓸 수 있다.
- `["-a", "never"]`로 바꾸면 승인 버튼 없이 바로 실행된다.

## 실행

```bash
cd /srv/codex-discord/main
. .venv/bin/activate
python app.py
```

## systemd

예시 서비스 파일:

- `systemd/codex-discord-main.service`

설치 예:

```bash
mkdir -p ~/.config/systemd/user
cp /srv/codex-discord/main/systemd/codex-discord-main.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-discord-main
```

로그아웃 후에도 계속 돌릴 계획이면:

```bash
sudo loginctl enable-linger "$USER"
```

## 배포

`/deploy`는 `scripts/deploy-prod.sh`를 호출한다.

현재 예시 스크립트는:

- `staging` checkout을 `main`에 merge
- 필요하면 `staging`, `main`을 각각 push
- main 서비스 재시작

즉 `deploy`를 쓰려면 필요에 따라 `staging` checkout도 같이 준비해야 한다.

## 선택 기능: staging bot

`staging` bot은 선택 사항이다.

있으면 좋은 점:

- 새 wrapper 변경을 실험하기 쉬움
- `main`과 `staging` 역할 분리 가능
- `/new-session-staging`, `/status-staging` 같은 별도 명령 사용 가능

없어도 `main` bot만으로 repository 생성, session 생성, Codex 작업은 가능하다.

## 구현 제한

- OpenAI API 직접 호출 없음
- `shell=True` 미사용
- subprocess argv는 전부 리스트로 전달
- DB 없음
- 웹 UI 없음
- 다중 사용자 권한 시스템 없음
- 자동 rollback 없음

## License

MIT
