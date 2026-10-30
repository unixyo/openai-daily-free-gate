# openai-daily-free-gate

OpenAI가 API 트래픽 데이터 공유(data sharing)에 참여한 조직에 제공하는 **일일 무료 토큰 한도**를 넘기 전에 텍스트 요청을 차단하는 로컬 프록시이다.

`127.0.0.1`에서만 수신하며, 날짜별 토큰 장부를 SQLite에 기록한다. 요청이 당일 상한을 넘을 가능성이 있으면 OpenAI로 보내기 전에 거절한다. Python 표준 라이브러리만 사용하며 별도 의존성은 없다.

> **면책 고지.** 이 도구는 비공식 도구이며 OpenAI와 관계가 없다. 무료 토큰 한도, 대상 모델 목록, 프로그램 조건은 OpenAI가 정하며 언제든 바뀔 수 있다(OpenAI는 프로그램 종료 30일 전에 알린다고 밝히고 있다). 이 도구는 조직의 실제 사용량을 볼 수 없다. 반드시 본인의 OpenAI 대시보드를 직접 확인해야 한다. 계정에 발생한 요금의 책임은 사용자에게 있으며, 어떠한 보증도 하지 않는다(`LICENSE` 참조).

English: [README.md](README.md)

## 기능

- `POST /v1/responses`, `POST /v1/chat/completions`의 텍스트 요청(SSE 스트리밍 포함)을 받아 HTTPS로 `https://api.openai.com`에 전달한다.
- 공식 무료 토큰 목록에서 가져온 내장 허용 목록의 모델 ID만 허용하며, 공식 그룹 두 개로 나눈다.
  - **big 그룹**: 공식 Build 한도 하루 250,000 토큰, 기본 차단 상한 **240,000**
  - **small 그룹**: 공식 Build 한도 하루 2,500,000 토큰, 기본 차단 상한 **2,400,000**
- 전달 전에 보수적 추정치를 예약한다. 추정치는 요청 JSON의 UTF-8 바이트 수 + 여유분 2,048 토큰 + 요청한 최대 출력 토큰(기본 2,048, 최대 8,192)이다. `사용량 + 진행 중 예약 + 이번 예약`이 상한을 넘으면 HTTP 429를 반환하고 OpenAI로는 아무것도 보내지 않는다.
- 응답을 받은 뒤 OpenAI가 보고한 실제 `usage` 합계를 기록한다. 스트림 중단이나 5xx 오류처럼 사용량을 알 수 없으면 예약분 전체를 사용한 것으로 기록한다. OpenAI가 4xx 오류를 반환한 요청은 0으로 기록한다.
- 매일 00:00 UTC(한국 시간 오전 9시)에 새 장부를 사용한다. 공식 초기화 시각과 같다.
- 무료 혜택 밖에서 과금될 수 있거나 비용을 미리 확인할 수 없는 요청을 거절한다. 대상은 도구·함수 호출, 웹 검색, 이미지, 오디오, 파일, `modalities`, `prediction`, 기본값이 아닌 `service_tier`, `background`, 저장된 대화·`previous_response_id`, 1이 아닌 `n`·`best_of`이다.
- `GET /v1/models`는 클라이언트가 보낸 키로 OpenAI의 실제 모델 목록을 조회한 뒤, 허용 목록에 있으면서 실제로 접근 가능한 모델만 반환한다. 이 호출은 토큰을 생성하지 않는다.
- Chat Completions에서 GPT-5·GPT-6·o3 모델 요청에 구형 `max_tokens`가 오면 `max_completion_tokens`로 바꿔 전달한다. 일부 OpenAI 호환 클라이언트가 아직 `max_tokens`를 보내기 때문이다. 예약량은 바뀌지 않는다.
- API 키를 저장·기록·출력하지 않는다. 요청 로그를 남기지 않으며, OpenAI 오류 본문도 로그에 남기지 않는다.

## 하지 않는 일

- **프록시를 거치지 않고 OpenAI API를 직접 호출하는 프로그램은 막을 수 없다.** 이 프록시를 통과한 요청만 세고 차단한다. Playground, 다른 앱, 다른 PC, `api.openai.com`에 직접 접속하는 스크립트는 보이지 않는다.
- **조직의 실제 사용량을 알지 못한다.** OpenAI는 무료 한도를 조직 전체에 적용한다. 다른 곳에서 당일 무료분을 먼저 썼다면, 로컬 장부에 여유가 남아 있어도 이 프록시를 거친 요청이 유료로 처리될 수 있다.
- 당일 이미 발생한 사용량을 가져오지 않는다.
- 입력 토큰 추정은 공식 토크나이저가 아니라 보수적 근삿값이다. 과대 추정하도록 설계했지만 비정상적인 요청까지 정확히 계측한다고 보장하지 않는다.
- 해당 프로젝트에 데이터 공유가 켜져 있는지, 조직이 대상인지 확인하지 않는다.
- 모델 허용 목록을 자동으로 갱신하지 않는다. 새 모델, 이름이 바뀐 모델, 별칭은 `gate.py`의 `BIG_MODELS`·`SMALL_MODELS`를 고치기 전까지 거절된다.
- 위 두 엔드포인트 외에 임베딩, 이미지, 오디오, 파일, 파인튜닝, evals, batch 등은 전달하지 않는다.
- 신뢰할 수 없는 사용자를 막는 보안 경계가 아니다. localhost에서만 수신하며, 클라이언트 키를 그대로 전달하는 것 외에 자체 인증은 없다.

## 상한 결정 근거

출처: OpenAI Help Center,
[Sharing feedback, evaluation and fine-tuning data, and API inputs and outputs with OpenAI](https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai)
의 "Complimentary tokens on prompts and completions shared with OpenAI" 항목(2026-10-29 확인, 2026-10-30 재확인). 관련 문서:
[OpenAI developer docs](https://developers.openai.com/api/docs/guides/chatgpt-api-credits).

작성 시점에 해당 페이지에는 다음 내용이 적혀 있다.

- 모델 그룹마다 일일 한도가 따로 있으며, 같은 그룹의 모델들이 한도를 공유한다.
- Build 조직: 첫 번째 그룹 250,000 토큰, 두 번째 그룹 2,500,000 토큰. Launch·Grow 조직: 1,000,000 토큰과 10,000,000 토큰.
- 카운터는 00:00 UTC에 초기화된다.
- 요청 하나가 당일 합계를 한도 너머로 밀면 **그 요청 전체**가 정상 요금으로 청구된다.
- 파인튜닝 모델, 파인튜닝 학습, evals, 도구 사용은 포함되지 않는다.

이에 따라 이 프록시는 다음과 같이 동작한다.

- **Build** 한도를 최대값으로 쓴다(`--big-cap`은 250,000, `--small-cap`은 2,500,000을 넘을 수 없다). 따라서 Launch·Grow 조직도 더 작은 Build 수치로 제한된다.
- 기본 상한은 한도의 96%(240,000 / 2,400,000)이다. 남은 4%는 프록시 밖 사용량과 추정 오차에 대비한 여유분이다.
- 한도를 넘는 요청 하나가 통째로 청구되므로, 요청마다 최악의 경우를 먼저 예약한다.
- 도구 사용은 혜택 대상이 아니므로 차단한다.

`gate.py`의 허용 목록은 Help Center 목록의 일부이다. 해당 페이지에는 이 프록시가 일부러 허용하지 않는 모델(예: 종료된 스냅샷)도 남아 있다. 사용 전에 두 목록을 직접 비교해야 한다.

## 요구 사항

- Python 3(표준 라이브러리만 사용). Linux의 Python 3.14에서 시험했다.
- 데이터 공유가 켜져 있고, 데이터 공유 설정 페이지에 "You're enrolled for complimentary daily tokens" 문구가 보이는 OpenAI API 조직.
- 선택: 백그라운드 실행용 systemd 사용자 서비스.

## 설치와 실행

```sh
git clone <this-repository-url> openai-daily-free-gate
cd openai-daily-free-gate
python3 gate.py
```

프록시는 `http://127.0.0.1:18765/v1`에서 수신한다. 옵션은 다음과 같다.

```sh
python3 gate.py --port 18765 --big-cap 230000 --small-cap 2300000 --db .state/usage.sqlite3
```

| 옵션 / 환경변수 | 기본값 | 의미 |
| --- | --- | --- |
| `--port` | `18765` | 로컬 포트(항상 127.0.0.1에 바인드) |
| `--db` | `gate.py` 옆 `.state/usage.sqlite3` | 장부 파일, 재시작 후에도 유지 |
| `--big-cap` | `240000` | big 그룹 일일 상한(최대 250000) |
| `--small-cap` | `2400000` | small 그룹 일일 상한(최대 2500000) |
| `GATE_UPSTREAM` 환경변수 | `https://api.openai.com` | 전달 대상 기본 URL |

자리표시자 값은 `.env.example`에 있다. 프록시는 `OPENAI_API_KEY`를 읽지 않으며, 키는 클라이언트가 보낸다.

### 클라이언트 연결

OpenAI 호환 클라이언트의 Base URL을 `http://127.0.0.1:18765/v1`로 바꾸고, API 키는 클라이언트의 원래 비밀키 설정(예: `OPENAI_API_KEY` 환경변수)에 둔다. 키 문자열을 셸 히스토리에 직접 입력하지 않는 편이 좋다.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:18765/v1")  # 키는 OPENAI_API_KEY에서 읽음
result = client.responses.create(
    model="gpt-5.6-terra", input="안녕", max_output_tokens=1024
)
print(result.output_text)
```

이 예시는 연결 후 실행하면 실제 API 호출이 발생하며, 한도를 넘으면 과금될 수 있다.

### 상태 확인

```sh
curl -s http://127.0.0.1:18765/status   # JSON
python3 status.py                       # 한국어 요약 표시
python3 status.py --watch 5             # 5초마다 갱신, Ctrl+C로 종료
python3 status.py --no-models           # 남은 토큰만 표시
```

`status.py`는 로컬 `/status`만 읽는다. 키가 필요 없고 토큰을 쓰지 않는다. `GET /health`는 간단한 동작 확인 응답을 돌려준다.

### systemd 사용자 서비스로 실행(선택)

```sh
mkdir -p ~/.config/systemd/user
cp systemd/openai-daily-free-gate.service.example ~/.config/systemd/user/openai-daily-free-gate.service
# 저장소가 ~/openai-daily-free-gate가 아니면 복사한 파일의 경로를 고친다
systemctl --user daemon-reload
systemctl --user enable --now openai-daily-free-gate
systemctl --user status openai-daily-free-gate
```

서비스가 멈추면 고정 Base URL을 쓰는 클라이언트에서 연결 오류가 난다. 이때 클라이언트가 다른 제공자나 URL로 조용히 전환하는지 따로 확인해야 한다. 서비스가 실행 중일 때 `gate.py`를 수동으로 같은 포트에 실행하면 주소 충돌이 난다.

## 테스트

```sh
python3 -m unittest -v test_gate.py
```

테스트는 localhost에 가짜 OpenAI 서버를 띄워 사용한다. OpenAI로 네트워크 요청을 보내지 않으며 크레딧을 쓰지 않는다.

## 권장 운용

1. 이 프록시 전용 OpenAI 프로젝트와 API 키를 만들어 프록시를 통해서만 쓴다.
2. 같은 조직의 다른 API 앱은 무료 대상 모델을 쓰지 않게 하거나, 그만큼 상한을 낮춘다.
3. 처음 사용할 때 [Usage 대시보드](https://platform.openai.com/usage/chat-completions)에서 "service tier"로 묶어 보고, 트래픽이 "data sharing incentive tier"로 표시되는지 확인한다.
4. [Credit grants / 결제 화면](https://platform.openai.com/settings/organization/billing/overview)과 비교한다.
5. Help Center 페이지를 주기적으로 다시 확인하고, 내용이 바뀌면 허용 목록과 상한을 갱신한다.

데이터 공유 프로젝트로 보낸 데이터는 OpenAI와 공유된다. 민감한 정보, 기밀, 개인정보는 보내지 않는다.

## 라이선스

MIT. `LICENSE` 참조.
