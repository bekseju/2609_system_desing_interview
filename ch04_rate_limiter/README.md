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
├── run_experiment.py            # 로컬 실험: 생성 → 재생·측정 → 보고서 (3단계)
├── run_http_experiment.py       # HTTP 부하 실험 (4단계)
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

### 로컬 실험 한 번에 실행 (3단계)

```bash
python scripts/run_experiment.py          # 시나리오 5 × 알고리즘 6 × 3회 (이 PC에서 약 6분)
python scripts/run_experiment.py --quick  # 축소: boundary·hot_key·normal × 6 × 1회 (약 1분)
python scripts/run_experiment.py --scenarios boundary --algorithms fixed_window sliding_log --replicates 2
python -m rate_limit_lab.metrics.report results/<UTC timestamp>   # 보고서만 다시 만들기
```

결과 폴더 `results/<UTC timestamp>/`:

```text
experiment.json                  실행 명령·계획(plan)·입력 CSV 해시
inputs/<시나리오>/                requests.csv, scenario.json (생성된 입력)
<시나리오>/<알고리즘>/memory/r<N>/
    requests.csv                 요청 원본 + 판정 결과 (요청마다 한 줄)
    timeseries.csv               100ms·1초 [start,end) 구간별 도착/수락/거절/처리/수행 건수
    summary.json                 건수·처리량·판정 지연·큐 대기·이동 1초 최대·키 수(제거 전후)·메모리
    environment.json             규칙·seed·CPU·OS·Python·패키지 버전·실행 명령
comparison.csv                   회차마다 한 줄 (summary.json을 펼친 값)
report.md, *.png                 보고서와 그래프
```

**재현 방법**: 같은 `--seed`(기본 42)와 같은 `configs/*.yaml`로 다시 실행하면 `experiment.json`의 입력 해시와
판정·건수가 모두 같다. 판정 지연·메모리는 장비와 순간 부하에 따라 달라진다.
각 회차는 새 프로세스에서 실행된다(RSS peak가 프로세스 단위이므로).

메모리 지표 세 가지는 측정 범위가 달라 서로 직접 비교하지 않는다.
- `algorithm_state_peak_bytes`: 기록 없이 판정만 반복할 때의 Python 할당 peak ≈ 알고리즘 상태 크기
- `tracemalloc_peak_bytes`: 재생기의 판정·이벤트 기록까지 포함한 Python 할당 peak
- `rss_*_bytes`: 프로세스 전체 상주 메모리(인터프리터·입력 데이터 포함)

### HTTP API와 실제 부하 (4단계)

```bash
# 서버 하나 직접 띄우기
python -m rate_limit_lab.api.server --algorithm token_bucket --port 8081 --instance-id A
curl "http://127.0.0.1:8081/work?client_id=alice"     # 200 / 429 (+ X-RateLimit-* , Retry-After)
curl "http://127.0.0.1:8081/stats"

# 부하 실험 (서버 기동 → 부하 → 저장 → 서버 종료를 조합마다 반복)
python scripts/run_http_experiment.py --quick         # 축소: 예열 1초·측정 5초, 인스턴스 1/2, 동시성 1/32, 1회
python scripts/run_http_experiment.py                 # 명세: 예열 10초·측정 60초, 인스턴스 1/2/4,
                                                      #       동시성 1/32/128, 3회 (알고리즘 5개 → 약 3시간)
python scripts/run_http_experiment.py --algorithms token_bucket --instances 4 --concurrency 128 \
    --replicates 3 --warmup-s 2 --measure-s 10        # 일부만 바꾸기
python scripts/run_http_experiment.py --quick --exp-dir results/<UTC timestamp>   # 기존 실험 폴더에 추가
```

- 결과는 `<시나리오>/<알고리즘>/http-i<인스턴스>-c<동시성>/r<N>/`에 로컬 결과와 같은 네 파일로 저장된다.
- 누출 버킷은 202(큐 접수)로 답하고, 처리 완료는 `GET /requests/{id}`, `GET /events`로 따로 조회한다.
- 요청마다 **발송 지연**(예정 대비), **HTTP 왕복 지연**, **서버 내부 판정 지연**(`X-Decision-Us`)을 따로 기록한다.
- 결과 분류는 `ok_200`, `queued_202`, `rejected_429`, `timeout`, `transport_error`, `http_error`로 서로 겹치지 않는다.
- 인스턴스가 여러 개면 각자 메모리 상태를 따로 가진다(공유 저장소 없음). 요청은 라운드 로빈으로 나눈다.
- Windows에서는 asyncio 타이머 해상도 때문에 발송 지연이 수~15ms 생길 수 있다. 그래서 따로 기록한다.

### 공통 계약 요약

- 시간: 이름이 `_ms`인 값은 시뮬레이션 시작(0ms) 기준 정수 ms. 구간은 반열린 `[start, end)`.
- 재생 순서: `scheduled_at_ms` 오름차순, 같은 시각은 `request_id` 문자열 순(ID는 0으로 채워 생성).
- 판정 상태: `allowed`, `rejected`, `queued`, `processed`. `processed`만 `processed_at_ms`를 갖는다.
- CSV의 null은 빈 칸. 헤더·필수 값·정수 단위(`1.5`, `1s` 불가)·중복 `request_id`를 읽을 때 검사한다.
- 정책 키: `rule_id`와 대상 값을 퍼센트 인코딩하므로 `:` 등이 섞여도 서로 다른 규칙/대상의 키가 충돌하지 않는다. 전역 규칙의 대상은 `*`.
