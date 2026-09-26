# Rate limiter 실험 구현 TODO

기준 문서: 사용자가 수정해 다시 업로드한 명세를 프로젝트의 `/ch04_rate_limiter/docs/specification.md`에 배치한다. 이 파일은 `/ch04_rate_limiter/docs/TODO.md`에 둔다. 아래 모든 프로젝트 경로는 **VS Code에서 연 `/ch04_rate_limiter`를 루트로 한 상대 경로**다. 작업 명령은 프로젝트 루트에서 실행한다. 각 항목은 구현하고 해당 확인 방법을 실행한 뒤 `[x]`로 바꾼다. 한 번에 한 항목씩 진행하며, 결과 파일이나 실행 명령을 작업 기록에 남긴다. `results/`는 실행 결과물이므로 아래 항목을 완료했다고 해서 실제 측정이 끝난 것으로 표시하지 않는다.

```text
/ch04_rate_limiter/
├── docs/
│   ├── specification.md
│   └── TODO.md
├── rate_limit_lab/
│   ├── algorithms/
│   ├── api/
│   ├── load/
│   ├── metrics/
│   ├── distributed/
│   └── lyft/
├── configs/
├── scripts/
├── tests/
├── results/                 # 실행 시 생성
├── docker-compose.yml
└── README.md
```

## 0. 프로젝트 준비

- [x] 0.1 `/ch04_rate_limiter`를 작업 루트로 열고 `docs/specification.md`, `docs/TODO.md`를 확인한다. 루트에 Python 3.12 패키지 구조(`rate_limit_lab/algorithms`, `rate_limit_lab/api`, `rate_limit_lab/load`, `rate_limit_lab/metrics`, `rate_limit_lab/distributed`, `rate_limit_lab/lyft`)와 `tests/`, `scripts/`, `configs/`를 만든다. 루트에서 `python -m ...` 기본 실행을 확인한다.
- [x] 0.2 의존성을 고정하고 설치·테스트 명령을 `README.md`에 적는다. Docker 없이 로컬 시뮬레이션 실행이 가능한지 확인한다.
- [x] 0.3 `configs/rules.yaml`에 사용자별 10건/초, 버킷 용량·큐 용량 10, 초당 재충전·처리 10건의 기본 정책을 정의하고 읽기 오류를 검증한다.
- [x] 0.4 공통 `Request`, `Decision`, `Rule` 자료형과 CSV 직렬화/역직렬화를 구현한다. 명세의 필수 필드, 상태(`allowed`, `rejected`, `queued`, `processed`), 누락 값과 단위를 검사한다.
- [x] 0.5 `rule_id + scope + 대상` 키 생성과 사용자·IP·엔드포인트·전역 선택을 구현하고 키 충돌 및 사용자 간 격리를 확인한다. 복수 규칙 결합은 7단계 최종 검증에서 확인한다.
- [x] 0.6 가상 시계(정수 ms), `[start,end)` 구간, 동일 시각의 `request_id` 순서 및 실제 실행용 단조 시계를 구현한다. 재생 순서가 반복 실행에서 같은지 확인한다.

## 1. 다섯 알고리즘: 한 항목씩 구현·검증

- [x] 1.1 공통 `decide(key, time)` 인터페이스와 키별 상태/유휴 키 제거 계약을 정의한다. 결과에 `remaining`, `retry_after_ms`, `reason`을 담는다.
- [x] 1.2 Token bucket의 초기 토큰 C, 연속 재충전, 용량 상한, 허용 시 토큰 1개 소비를 구현한다. 즉시 버스트·거절 후 무소비·유휴 후 회복을 확인한다.
- [x] 1.3 Leaking bucket의 FIFO `admit`/`drain`과 큐 용량·고정 처리 간격을 구현한다. 접수·처리·거절 시각과 큐 길이가 맞는지 확인한다.
- [x] 1.4 Fixed window counter를 구현한다. `floor(t/W)`와 허용 건만 카운트하는 동작을 창 직전·직후 요청으로 확인한다.
- [x] 1.5 Sliding window log의 **strict 모드**를 구현한다. `timestamp <= t-W` 제거, 허용 시각만 저장, 임의 이동 창에서 허용 건이 L을 넘지 않는지 확인한다.
- [x] 1.6 Sliding window log의 **PDF 재현 모드**를 추가한다. 거절 시각도 저장하며 strict 모드와 후속 판정·로그 크기 차이를 확인한다.
- [x] 1.7 Sliding window counter를 구현한다. `curr + prev × (1 - offset/W)`를 반올림 전에 비교하고 창 경계·이전 창 영향·유휴 후 회복을 확인한다.
- [x] 1.8 모든 알고리즘의 복수 키 격리, 용량 경계, TTL·유휴 상태 제거 테스트를 수행한다. 비활성 키 수가 제거 후 감소하는지 확인한다.
- [x] 1.9 누출 버킷의 접수와 실제 처리를 분리해 검증한다. 지속 부하에서 처리 속도와 큐 대기 시간을 측정한다.

## 2. 재현 가능한 요청 생성·가상 시계 재생

- [x] 2.1 `configs/scenarios.yaml`과 seed 기반 CSV 생성기를 구현한다. 동일 seed에서 동일 파일을 생성하고 시나리오 메타데이터·건수·목표/실제 RPS를 저장한다.
- [x] 2.2 일반 패턴(60초, 사용자 100명, 사용자별 독립 Poisson 평균 5 req/s)을 생성하고 분포와 사용자별 요청 수를 확인한다.
- [x] 2.3 20~25초에 기본 트래픽의 10배를 **추가**하는 과부하 패턴을 생성하고 해당 구간 건수를 확인한다.
- [x] 2.4 같은 키로 0.99초·1.01초 각각 10건을 만드는 창 경계 패턴을 생성한다. fixed window의 이동 1초 초과 허용을 확인한다.
- [x] 2.5 30~40초 사이 한 번씩 요청하는 10,000개 고유 키 패턴을 생성하고 총 고유 키 수를 확인한다.
- [x] 2.6 한 사용자에게 동시 100건을 보내는 핫 키 패턴을 생성하고 요청 ID·동일 시각 정렬을 확인한다.
- [x] 2.7 저장된 **같은 CSV**를 다섯 알고리즘에 주입하는 결정적 재생기를 구현한다. 각 요청의 판정·도착·처리 이벤트를 남기고 같은 실행의 판정 출력이 일치하는지 확인한다.
- [x] 2.8 Strict sliding log를 기준으로 sliding counter의 false allow/false reject 수와 비율을 요청별로 계산한다. 각 알고리즘이 서로 다른 정책 의미를 갖는 점을 결과에 표시한다.

## 3. 로컬 측정과 보고서

- [x] 3.1 `decide` 전후 시간, 처리량, 허용·거절·접수·처리·오류·타임아웃 건수를 집계한다. 상태 합계와 입력 건수가 일치하는지 확인한다.
- [x] 3.2 100ms·1초 구간별 도착·허용·실제 처리 건수와 이동 1초 최대 허용량을 계산한다. 양 끝점 `[start,end)`를 검증한다.
- [x] 3.3 결정 지연 p50/p95/p99/max와 누출 버킷 큐 대기 지연 p50/p95/p99를 계산한다. 측정 대상이 없는 경우를 빈 값으로 처리한다.
- [x] 3.4 `tracemalloc` 추적 peak, 프로세스 RSS peak, 활성 키·로그 엔트리 수 및 제거 전후 상태를 별도 지표로 기록한다.
- [x] 3.5 `results/<UTC timestamp>/<scenario>/<algorithm>/<backend>/<replicate>/`에 `requests.csv`, `timeseries.csv`, `summary.json`, `environment.json`을 생성한다. 설정·seed·CPU·OS·Python 버전·실행 명령도 저장한다.
- [x] 3.6 `comparison.csv`, 시간대별 결과·p95 지연·메모리 PNG 및 `report.md`를 만든다. 같은 입력·정책·백엔드끼리 비교하고 측정 범위와 단위를 명시한다.
- [x] 3.7 `scripts/run_experiment.py` 한 명령으로 생성→재생→보고서를 실행하고 실험 폴더 재현 방법을 README에 적는다.

## 4. HTTP API와 실제 부하

- [x] 4.1 `GET /work?client_id=...`에 정책 선택과 200/429 응답을 구현한다. 적용 가능한 `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`를 검증한다.
- [x] 4.2 누출 버킷의 202(큐 접수), 비동기 처리 완료 이벤트와 큐 포화 시 429를 구현한다. 접수와 처리 완료를 별도 조회·기록할 수 있게 한다.
- [x] 4.3 동일 CSV를 벽시계 속도로 보내는 부하 발생기를 구현한다. 발송 지연, HTTP 왕복 지연, API 내부 결정 지연을 혼합하지 않고 수집한다.
- [x] 4.4 예열 10초·측정 60초, 인스턴스 1/2/4, 동시성 1/32/128, 3회 반복을 실행 가능한 옵션으로 만든다. 축소 설정도 제공하고 각 회차를 독립 저장한다.
- [x] 4.5 실제 HTTP 실험의 타임아웃·전송 오류·429를 분리하여 기록하고 결과 요약을 검증한다.

## 5. 두 인스턴스와 Redis: 동기화·race 재현

- [ ] 5.1 버전 고정 Redis와 독립 API 인스턴스 A/B의 Docker Compose 구성을 만든다. 각 서비스 시작·중지·상태 점검을 확인한다.
- [ ] 5.2 A/B의 독립 메모리 고정 윈도에 같은 사용자 요청 10건씩 분배한다. 한 창에서 최대 20건 허용되는 사례와 요청별 `instance_id`를 저장한다.
- [ ] 5.3 Redis 고정 윈도 Lua 스크립트를 구현해 검사·증가·최초 TTL 설정을 원자화한다. 동일 분배에서 전체 10건만 허용하는지 확인한다.
- [ ] 5.4 Redis token bucket Lua 스크립트를 구현해 시간·재충전·판정·상태/TTL 변경을 한 번에 처리한다. 동시 요청에서 용량 초과 허용이 없는지 확인한다.
- [ ] 5.5 테스트 전용 `unsafe_redis`의 GET→검사→장벽→SET을 별도 경로에 구현한다. `limit=1`, 독립 프로세스 2개에서 둘 다 허용되는 상황을 장벽 타임아웃과 함께 재현한다.
- [ ] 5.6 동일 race 입력을 원자적 Lua 경로에서 재생한다. 허용이 정확히 1건인 자동화 통합 테스트와 `scripts/run_race.py`를 제공한다.
- [ ] 5.7 시뮬레이션 시간 기준의 워커 간 일치·역행 시각 거부를 확인한다. 실제 HTTP용 Redis `TIME` 또는 단일 시간 기준을 채택해 기록한다.
- [ ] 5.8 Redis `INFO memory`, `MEMORY USAGE` 표본, 키 수·TTL과 가능하면 컨테이너 RSS를 수집한다. 프로세스 메모리와 Redis 메모리를 별도 열로 보고한다.
- [ ] 5.9 Redis 중지·재시작·TTL 만료를 시험한다. `fail-open`/`fail-closed`, `backend_error`, 재시작 후 상태 유실의 결과를 기록한다.
- [ ] 5.10 (선택) Sliding log의 원자적 Redis ZSET 변형을 구현하고 고유 요청 멤버·정리·판정·TTL을 동시 요청으로 검증한다. 미구현이면 보고서에 `not_run`으로 표시한다.

## 6. Lyft 공개 rate limiter 연동

- [ ] 6.1 `envoyproxy/ratelimit` 공식 README와 Compose 예제에서 동작하는 commit·이미지 digest·gRPC 포트·설정 문법을 확인해 고정한다. 설치가 막히면 원인과 재실행 명령을 기록한다.
- [ ] 6.2 독립 Redis와 Lyft 서비스를 선택 실행하는 Compose 구성을 만들고 상태 점검 및 설정 파일 로딩을 확인한다.
- [ ] 6.3 `domain: auth`의 `auth_type: login` 5건/분과 `domain: lab`의 `client_id`별 10건/초를 설정한다. 사용자 두 명의 한도를 독립적으로 확인한다.
- [ ] 6.4 gRPC `ShouldRateLimit` 어댑터와 `scripts/run_lyft.py`를 구현한다. 정상·`OVER_LIMIT`·연결 오류를 서로 다른 결과로 남긴다.
- [ ] 6.5 (선택) Envoy 또는 HTTP 래퍼를 연결해 HTTP 429를 체험할 수 있게 한다. gRPC 판정과 HTTP 응답 간 매핑을 검증한다.
- [ ] 6.6 동일한 벽시계 CSV를 자체 서비스와 Lyft 어댑터에 재생한다. 판정·왕복 지연·Redis 메모리·서비스 RSS를 별도 표로 기록하고 가상 시계 주입 가능 여부를 가정하지 않는다.

## 7. 최종 검증과 설명서

- [ ] 7.1 알고리즘별 용량·경계·회복·키 격리·TTL 검증을 한 번에 실행하는 명령을 README에 적고 결과를 확인한다.
- [ ] 7.2 고정 창의 경계 초과, strict 로그의 이동 창 준수, 카운터의 판정 오차, 누출 버킷의 처리율·대기 시간 결과를 보고서에 요약한다.
- [ ] 7.3 로컬 A/B 20건, `unsafe_redis` 2건, Lua 원자 모드 1건 재현 결과를 순서대로 기록한다. 장벽 없는 race 부하에서 현상이 매번 나타나야 한다는 조건은 두지 않는다.
- [ ] 7.4 모든 회차에서 입력 건수, 요청별 최종 상태, 요약 건수의 정합성을 검사하고 실행하지 못한 실험을 `not_run`으로 표시한다.
- [ ] 7.5 루트 `README.md`에 각 단계의 실행·중지·초기화·재실행 명령, 필요한 환경, `docs/specification.md`·`docs/TODO.md` 위치, 결과 해석, 서비스별 보장 범위 및 Redis 단일 인스턴스/장애 조치 한계를 정리한다.
- [ ] 7.6 새 환경에서 README대로 로컬 시뮬레이션을 재실행한다. Docker를 사용할 수 있으면 분산 및 Lyft 통합 검증도 수행하고 생성된 결과 경로를 남긴다.

## 작업 기록

완료할 때 아래 형식으로 한 줄씩 추가한다. 미실행 작업에 완료 표시를 하지 않는다.

| 날짜       | 항목 | 실행 명령 또는 결과 경로                                                                                                                                                                                | 관찰·남은 문제                                                                                                                                                                                                                                                                                                   |
| ---------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-09-24 | 0.1  | `python -m rate_limit_lab` → 6개 하위 패키지 import ok; `tests/test_structure.py`                                                                                                                       | Python 3.12 미설치로 사용자 결정에 따라 3.13.5 사용(`requires-python >=3.12`). 프로젝트 전용 `.venv` 생성                                                                                                                                                                                                        |
| 2026-09-24 | 0.2  | `pyproject.toml`, `requirements.txt`(PyYAML 6.0.3, pytest 9.1.1 등 고정); 새 venv에 README 절차로 설치 → `python -m pytest -q` 102 passed; Docker를 PATH에서 제거한 상태로도 102 passed                 | 이후 단계 의존성(HTTP·Redis·gRPC·matplotlib 등)은 해당 단계에서 추가 고정                                                                                                                                                                                                                                        |
| 2026-09-24 | 0.3  | `configs/rules.yaml`(`user_default`); `python -m rate_limit_lab rules`; `tests/test_config.py`                                                                                                          | 파일 없음·YAML 구문·version·누락/미지 필드·타입(bool·문자열·실수 ms)·범위·중복 rule_id 검사. `idle_ttl_ms: 2000` 추가                                                                                                                                                                                            |
| 2026-09-24 | 0.4  | `rate_limit_lab/models.py`; `tests/test_models.py`                                                                                                                                                      | CSV null=빈 칸, 헤더 일치·열 개수·필수 값·정수 단위·중복 ID·상태별 `processed_at_ms`·시각 순서 검사. `latency_us`는 실수                                                                                                                                                                                         |
| 2026-09-24 | 0.5  | `rate_limit_lab/keys.py`; `tests/test_keys.py`                                                                                                                                                          | 키 `rl:<rule_id>:<scope>:<target>`, 구성요소 퍼센트 인코딩으로 구분자 주입 충돌 방지. MVP 규칙 선택은 요청의 `rule_id` 1개                                                                                                                                                                                       |
| 2026-09-24 | 0.6  | `rate_limit_lab/clock.py`; `tests/test_clock.py`                                                                                                                                                        | 셔플 20회 및 서로 다른 `PYTHONHASHSEED` 3개 프로세스에서 재생 순서 동일. 동일 시각 순서는 `request_id` 문자열 순이므로 생성기는 0 채움 ID 사용 필요(2.1)                                                                                                                                                         |
| 2026-09-25 | 1.1  | `rate_limit_lab/algorithms/base.py`, `__init__.py`(`create(name, rule)`); `tests/test_algo_contract.py`                                                                                                 | `Verdict(status, remaining, retry_after_ms, reason)`. 시간 역행·비정수 시 `ClockError`. 유휴 키 제거는 `idle_ttl_ms` 경과 **및** 상태가 초기와 동등할 때만(제거해도 판정 불변). 모든 알고리즘에서 `retry_after_ms` 후 재시도는 수락, 1ms 전은 거절 확인                                                          |
| 2026-09-25 | 1.2  | `algorithms/token_bucket.py`; `tests/test_token_bucket.py`                                                                                                                                              | 0ms 15건 → 10 허용/5 거절(retry 100ms). 거절 무소비, 350ms 휴식 → 3건, 장시간 휴식도 상한 10. 부동소수 오차 여유 `EPS=1e-9`                                                                                                                                                                                      |
| 2026-09-25 | 1.3  | `algorithms/leaking_bucket.py`; `tests/test_leaking_bucket.py`                                                                                                                                          | 처리 완료 = max(도착, 앞 요청 처리 완료) + 100ms. 0ms 15건 → 10 접수/5 거절, 처리 100…1000ms FIFO, 250ms 큐 길이 8. 간격은 정수 ms 반올림(1000/r 비정수면 건당 ≤0.5ms 오차)                                                                                                                                      |
| 2026-09-25 | 1.4  | `algorithms/fixed_window.py`; `tests/test_fixed_window.py`                                                                                                                                              | 999ms 거절·1000ms 허용, 거절 건 미카운트. 990ms 10건 + 1010ms 10건 → 20건 허용(이동 1초 최대 20)                                                                                                                                                                                                                 |
| 2026-09-25 | 1.5  | `algorithms/sliding_log.py` (`sliding_log`); `tests/test_sliding_log.py`                                                                                                                                | 무작위 트레이스 10개(각 2,000건+경계 몰림)에서 임의 이동 1초 허용 최대 = 10. 경계 트레이스 990/1010ms → 10 허용/10 거절                                                                                                                                                                                          |
| 2026-09-25 | 1.6  | `sliding_log_pdf`; `tests/test_sliding_log.py`                                                                                                                                                          | 20 req/s × 5초: strict 50건 허용·로그 10, PDF 10건 허용(이후 전부 거절)·로그 20. 과부하 후 회복 시점 strict 3000ms vs PDF 3600ms                                                                                                                                                                                 |
| 2026-09-25 | 1.7  | `algorithms/sliding_counter.py`; `tests/test_sliding_counter.py`                                                                                                                                        | 정수 비교 `curr·W + prev·(W−offset) < L·W`로 오차 제거. 책 예 6.5<7 허용, 9.9 허용(반올림 전), 정확히 10.0 거절. 990ms 10건 후 1010ms에 1건 허용(strict 대비 false allow 1)                                                                                                                                      |
| 2026-09-25 | 1.8  | `tests/test_algo_common.py`                                                                                                                                                                             | 6개 구현 × (용량 경계 L=1/3/10, 102개 키 격리, TTL 1999ms 미제거·2000ms 99개 제거, 초과 상태 키 미제거, 무작위 3,000건 트레이스에서 매번 제거 vs 무제거 판정 동일). 전체 `python -m pytest -q` 235 passed                                                                                                        |
| 2026-09-25 | 1.9  | `python scripts/leaking_bucket_sustained.py` (60초)                                                                                                                                                     | 5/10 rps: 거절 0, 대기 p50/p95/p99 = 100/100/100ms. 20 rps: 접수 609·거절 591, 정상상태 처리 10.0건/초, 1초 최대 10, 대기 p50/p95/p99 = 1000/1000/1000ms(평균 986). 50 rps: 접수 609·거절 2391, 처리 10.0건/초, 대기 p50 1000ms                                                                                  |
| 2026-09-25 | 2.1  | `configs/scenarios.yaml`, `rate_limit_lab/load/scenarios.py`, `python -m rate_limit_lab.load.generate` → `results/scenarios/<시나리오>-seed42/{requests.csv, scenario.json}`; `tests/test_scenarios.py` | 난수는 `Random("<seed>\|<구성요소>\|…")`로 사용자별 분리. 같은 seed 파일 SHA-256 동일(서로 다른 `PYTHONHASHSEED` 프로세스 포함), seed 다르면 다름. `scenario.json`에 seed·규칙·구성요소별 건수·1초 구간별 목표/실제 RPS·CSV 해시 저장                                                                            |
| 2026-09-25 | 2.2  | `results/scenarios/normal-seed42/`                                                                                                                                                                      | 30,006건(기대 30,000), 사용자 100명 모두 300±87 이내, 도착 간격 평균 ≈200ms(±3%), 사용자·초별 건수 분산/평균 ≈1(Poisson)                                                                                                                                                                                         |
| 2026-09-25 | 2.3  | `results/scenarios/overload-seed42/`                                                                                                                                                                    | 54,907건(기대 55,000). 20~25초 추가분 ≈25,000(기본의 ≈10배), 기본 트래픽은 normal과 요청 단위로 동일(0번 구성요소 난수 공유)                                                                                                                                                                                     |
| 2026-09-25 | 2.4  | `results/scenarios/boundary-seed42/`                                                                                                                                                                    | 990ms 10건 + 1010ms 10건. fixed_window 20건 허용·이동 1초 최대 20, strict log 10                                                                                                                                                                                                                                 |
| 2026-09-25 | 2.5  | `results/scenarios/unique_keys-seed42/`                                                                                                                                                                 | 고유 키·IP 10,000, 모두 [30000,40000)ms. 제거 없음: 활성 키 최대 10,000 / 1초마다 제거: 최대 3,038, 실행 중 6,982 제거, 종료 시 3,018 → 2,026(TTL 2초 안 키 유지)                                                                                                                                                |
| 2026-09-25 | 2.6  | `results/scenarios/hot_key-seed42/`                                                                                                                                                                     | 0ms 100건, ID r00000001~r00000100 연속. 입력을 뒤집어도 6개 구현 모두 r…01~r…10만 수락                                                                                                                                                                                                                           |
| 2026-09-25 | 2.7  | `rate_limit_lab/load/replay.py`, `python -m rate_limit_lab.load.run_replay <requests.csv>` → `replay/<알고리즘>/{decisions,events}.csv, summary.json`; `tests/test_replay.py`                           | 요청마다 arrival·decision(·processed) 이벤트, 시간순. 입력 순서를 섞어도 판정·이벤트·통계 동일. overload `--no-latency` 2회 → 출력 20개 파일 해시 동일. 누출 버킷 최종 상태 `processed`, 처리 이동 1초 최대 ≤10                                                                                                  |
| 2026-09-25 | 2.8  | `rate_limit_lab/metrics/accuracy.py` → `replay/accuracy.{json,csv}`; `tests/test_accuracy.py`                                                                                                           | sliding_counter vs strict: boundary false allow 1(5%), normal FA 406(1.35%)/FR 77(0.26%), overload FA 3,685(6.71%)/FR 3,335(6.07%). 나머지는 `policy_difference`로 표시. 확인된 성질: PDF 로그는 false allow 0, token·leaking bucket은 수락 요청이 동일(차이는 처리 시각). 전체 `python -m pytest -q` 306 passed |
| 2026-09-26 | 3.1 | `rate_limit_lab/metrics/summary.py`; `tests/test_local_metrics.py` | 상태 6종(allowed/rejected/queued/processed/error/timeout) + accepted·admitted_to_queue·처리량(입력÷재생 벽시계). `check_consistency`로 합계=입력 검사, 90개 회차 모두 통과. 로컬 처리량 예: overload 누출 버킷 약 45,900 req/s |
| 2026-09-26 | 3.2 | `rate_limit_lab/metrics/timeseries.py` → `timeseries.csv` | 100ms·1초 [start,end) 구간별 arrivals/accepted/rejected/processed/served. 99→0번, 100→1번, 999→0번, 1000→1번 구간 테스트. 이동 1초 최대(전체·키당) 기록: normal 키당 최대 수락 sliding_log 10, sliding_counter 14, fixed_window 16, token/leaking 18, 누출 버킷 수행 10 |
| 2026-09-26 | 3.3 | `summary.json`의 `decision_latency_us`, `queue_wait_ms` | nearest-rank p50/p95/p99/max. 값이 없으면 count 0·나머지 null. normal 판정 p95 2.8~3.9µs(중앙값 3회). 누출 버킷 큐 대기 normal 100/304/432ms, overload 143/993/999ms |
| 2026-09-26 | 3.4 | `rate_limit_lab/metrics/memory.py`, `run_local.py` | 재생을 3번: ①지연(추적 없음) ②tracemalloc 전체 ③기록 없이 판정만(≈알고리즘 상태). ①②판정 일치(`replay_consistent`). 상태 peak: normal 로그 137.7KB vs 카운터 10~11KB, unique_keys 로그 2.57MB vs 카운터 0.35MB. RSS peak는 프로세스 단위라 회차마다 새 프로세스. 활성 키 제거 전후: unique_keys 3,038 → 2,026 |
| 2026-09-26 | 3.5 | `results/20260926T072928Z/<시나리오>/<알고리즘>/memory/r{1,2,3}/` | 네 파일(requests.csv=요청+판정, timeseries.csv, summary.json, environment.json). environment에 CPU(i7-10510U, 8 논리 코어)·OS·Python 3.13.5·패키지 버전·규칙·seed·실행 명령 |
| 2026-09-26 | 3.6 | `rate_limit_lab/metrics/report.py` → `results/20260926T072928Z/{comparison.csv, report.md, timeseries_*.png, latency_p95_memory.png, memory_memory.png}` | 같은 시나리오·규칙·백엔드끼리 표, 측정 범위·단위 표, 정책 의미 표, strict 대비 판정 차이, 정합성 검사, `not_run` 목록. 그래프 색은 dataviz 기본 팔레트 1~6(검증 통과, 대비 부족 3색은 값 직접 표기 + 표로 보완) |
| 2026-09-26 | 3.7 | `python scripts/run_experiment.py` → `results/20260926T072928Z/` (5 시나리오 × 6 알고리즘 × 3회 = 90회차) | 한 명령으로 생성→재생→보고서. 정합성 문제 0, `not_run` 0. `--quick` 축소 설정 제공. 재현 방법 README에 기록. 전체 `python -m pytest -q` 343 passed |
| 2026-09-26 | 4.1 | `rate_limit_lab/api/server.py`; `tests/test_api.py` | aiohttp. `GET /work` 200/429, `X-RateLimit-Limit`(창=limit, 토큰=용량, 누출=큐 용량), `X-RateLimit-Remaining`, `Retry-After`(ceil 초, 최소 1) + `X-RateLimit-Retry-After-Ms`, `X-Decision-Us`. 헤더 정확도 한계는 server.py 상단에 문서화. 400(client_id 누락·알 수 없는 rule_id) |
| 2026-09-26 | 4.2 | 같은 파일; `tests/test_api.py` | 누출 버킷 202 + `status_url`, 비동기 작업자가 처리 예정 시각에 이벤트 기록. `GET /requests/{id}`(queued→processed), `GET /events?after=N`(커서). 큐 포화 429(retry 100ms). 실시간 테스트에서 처리 간격 ≥100ms |
| 2026-09-26 | 4.3 | `rate_limit_lab/load/http_load.py`; `tests/test_http_load.py` | 같은 CSV를 벽시계 예정 시각에 발송. 요청마다 send_lag_ms·rtt_ms·server_decision_us 분리 기록. 동시성 상한 초과 시 발송 지연으로 반영(동시성 1·작업 100ms → 마지막 지연 >350ms). 예열은 `warmup-` 접두 키로 분리 |
| 2026-09-26 | 4.4 | `scripts/run_http_experiment.py`; (a) `--quick --exp-dir results/20260926T072928Z` 8조합, (b) 명세 길이(예열 10초·측정 60초) i1-c32 token/leaking → `results/20260926T074105Z`, (c) i4-c128 × 3회(측정 10초) | 조합마다 서버 새로 기동·종료, 회차별 독립 폴더(`http-i<N>-c<C>/r<k>`). 명세 전체(5 알고리즘 × 3 × 3 × 3회 × 70초 ≈ 3시간)는 옵션으로만 제공하고 **실행하지 않음**. (b) normal 30,006건 전부 200/202, rtt p95 5.7/8.8ms |
| 2026-09-26 | 4.5 | `tests/test_http_load.py`; overload i1-c128 30초 → `results/20260926T074105Z/overload/` | 타임아웃(작업 1초·제한 0.3초 → 10건)·전송 오류(없는 인스턴스 → 5건)·429 분리 검증, `check_consistency` 통과. 실제 overload: 토큰 200 19,377 / 429 20,486, 누출 202 23,005 / 429 16,858, 타임아웃·오류 0. 단, 발송 지연 p95 1.9s/5.9s — 부하 발생기가 초당 5,500건 몰림을 제때 못 보내 도착이 퍼짐(로컬 판정과 직접 비교 불가) |
