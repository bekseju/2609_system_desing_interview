# Rate limiter 실험 (System Design Interview ch.4)

- 명세: [docs/specification.md](docs/specification.md)
- 진행 상황: [docs/TODO.md](docs/TODO.md)

모든 명령은 이 폴더(`ch04_rate_limiter/`)를 루트로 두고 실행한다.

## 환경

- Python 3.12 이상 (명세 기준은 3.12, 현재 검증 환경은 **Python 3.13.5 / Windows 11**)
- 로컬 시뮬레이션은 Docker가 필요 없다. Docker Compose는 5·6단계(Redis, Lyft)에서만 사용한다.

## 설치

의존성은 `requirements.txt`에 버전을 고정했다(`pip freeze` 결과). 패키지는 의존성 없이 editable로 설치한다.

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

```bash
# macOS / Linux
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e . --no-deps
```

아래 예시는 venv를 활성화했다고 가정하고 `python`으로 적는다.

## 기본 실행 확인

```bash
python -m rate_limit_lab          # 환경·하위 패키지 import 확인
python -m rate_limit_lab rules    # configs/rules.yaml 검증 및 출력 (잘못되면 종료 코드 2)
```

## 테스트

```bash
python -m pytest -q
```

## 구성

```text
rate_limit_lab/
├── models.py      # Request / Decision / Rule, Status·Scope, CSV 직렬화
├── config.py      # configs/rules.yaml 로더와 검증
├── keys.py        # 정책 키 rl:<rule_id>:<scope>:<target> 생성·규칙 선택
├── clock.py       # 가상 시계(정수 ms), [start,end) 구간, 재생 순서, 단조 시계
├── algorithms/    # 다섯 알고리즘 (아래 표)
├── api/  load/  metrics/  distributed/  lyft/
configs/rules.yaml # 기본 정책: 사용자별 10건/초, 버킷·큐 10, 재충전·누출 10건/초
configs/scenarios.yaml  # 요청 시나리오 5종 (seed 기반)
scripts/
└── leaking_bucket_sustained.py  # 누출 버킷 지속 부하: 처리 속도·대기 시간 측정
tests/
results/           # 실행 시 생성 (git 제외)
```

### 알고리즘 (`rate_limit_lab/algorithms/`)

모두 `limiter.decide(key, now_ms)`로 쓰고 `Verdict(status, remaining, retry_after_ms, reason)`를 돌려준다.
각 파일 맨 위 주석에 동작 원리와 기본 정책 예시가 있다.

| 이름 | 한 줄 요약 | 기본 정책에서 "0ms에 15건" |
| --- | --- | --- |
| `token_bucket` | 토큰 10개로 시작, 초당 10개 재충전, 요청마다 1개 소비 | 10 허용 / 5 거절 |
| `leaking_bucket` | 줄 10칸, 100ms마다 1건 처리. 접수(`queued`)와 처리(`processed`)가 분리됨 | 10 접수 / 5 거절, 처리 100~1000ms |
| `fixed_window` | 1초 칸마다 10건. 칸 경계에서 최대 20건이 몰릴 수 있음 | 10 허용 / 5 거절 |
| `sliding_log` | 최근 1초의 허용 시각을 모두 저장 (strict, 가장 정확) | 10 허용 / 5 거절 |
| `sliding_log_pdf` | 책처럼 거절 시각도 저장 → 과부하가 이어지면 계속 거절 | 10 허용 / 5 거절 |
| `sliding_counter` | 지금 칸 + 앞 칸 × 겹치는 비율로 추정 (근사) | 10 허용 / 5 거절 |

유휴 키 제거(`evict_idle`)는 `idle_ttl_ms` 이상 쉬었고 **지워도 판정이 바뀌지 않는** 키만 지운다.

```bash
python scripts/leaking_bucket_sustained.py   # 5/10/20/50 req/s × 60초
```

### 요청 생성과 결정적 재생 (`rate_limit_lab/load/`)

시나리오는 [configs/scenarios.yaml](configs/scenarios.yaml)에 있다: `normal`, `overload`, `boundary`, `unique_keys`, `hot_key`.

```bash
# 1) 요청 CSV 생성 (같은 seed → 항상 같은 파일)
python -m rate_limit_lab.load.generate                    # 전부, seed 42
python -m rate_limit_lab.load.generate boundary --seed 7  # 일부, seed 지정
#    → results/scenarios/<시나리오>-seed<seed>/requests.csv, scenario.json(건수·목표/실제 RPS·해시)

# 2) 같은 CSV를 여섯 구현에 재생 + strict sliding log 대비 비교
python -m rate_limit_lab.load.run_replay results/scenarios/boundary-seed42/requests.csv
#    → .../replay/<알고리즘>/decisions.csv, events.csv, summary.json
#    → .../replay/accuracy.json, accuracy.csv (요청별 false_allow / false_reject)
#    --no-latency: 판정 지연을 0으로 기록해 출력 파일을 실행마다 완전히 같게 만든다
```

`accuracy.json`의 `interpretation`이 `approximation_error`인 것은 sliding_counter뿐이다.
나머지 알고리즘은 정책 의미가 달라서, 기준과 다르게 판정해도 오류가 아니라 **정책 차이**다(`policy_semantics` 참고).

### 공통 계약 요약

- 시간: 이름이 `_ms`인 값은 시뮬레이션 시작(0ms) 기준 정수 ms. 구간은 반열린 `[start, end)`.
- 재생 순서: `scheduled_at_ms` 오름차순, 같은 시각은 `request_id` 문자열 순(ID는 0으로 채워 생성).
- 판정 상태: `allowed`, `rejected`, `queued`, `processed`. `processed`만 `processed_at_ms`를 갖는다.
- CSV의 null은 빈 칸. 헤더·필수 값·정수 단위(`1.5`, `1s` 불가)·중복 `request_id`를 읽을 때 검사한다.
- 정책 키: `rule_id`와 대상 값을 퍼센트 인코딩하므로 `:` 등이 섞여도 서로 다른 규칙/대상의 키가 충돌하지 않는다. 전역 규칙의 대상은 `*`.
