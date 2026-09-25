# Rate limiter 개념 검증 및 비교 실험 명세

## 1. 목표와 기준

첨부 `/ch04_rate_limiter/ch04.pdf`(Chapter 4: Design a Rate Limiter)의 다섯 알고리즘을 구현하고, 같은 입력 요청을 사용해 허용·거절·대기 결과, 결정 지연 시간, 상태 메모리를 비교한다. 단일 프로세스의 정확성 실험, 실제 HTTP 부하 실험, 다중 인스턴스의 동기화 및 경쟁 상태 실험을 분리한다. Lyft가 공개한 rate limiting 서비스의 현행 저장소 `envoyproxy/ratelimit`를 별도 비교 대상으로 실행한다. 목표는 재현 가능한 개념 검증이며, 서비스별 알고리즘과 보장 범위를 보고서에 명시한다.

## 2. 산출물과 실행 환경

- Python 3.12 기반 패키지 `rate_limit_lab/`: `algorithms/`, `api/`, `load/`, `metrics/`, `distributed/`, `lyft/`로 구성. 의존성 버전 고정 및 재현 가능한 설치 절차를 제공한다.
- `docker-compose.yml`: Redis 단일 인스턴스, 자체 API 서버 2개, 선택적으로 Lyft 서비스와 비교용 Envoy/HTTP 프록시. Redis 실패 실험을 위해 각 서비스를 개별 중지할 수 있어야 한다. 이미지에는 `latest`나 `master` 대신 검증된 태그 또는 digest를 고정한다.
- `configs/rules.yaml`, `configs/scenarios.yaml`, `scripts/run_experiment.py`, `scripts/run_race.py`, `scripts/run_lyft.py`, `tests/`, `README.md`, `results/`(실행 시 생성). 단일 명령으로 샘플 결과를 만들고 그래프(PNG)·CSV·JSON·Markdown 요약을 생성한다.
- 필요한 도구: Python, Docker Compose. 로컬 파일 기반 시뮬레이션은 Docker 없이 실행되어야 한다. 현재 환경에서 이미지 접근이 안 될 경우 실행 불가 이유와 재현 명령을 기록하고 결과를 꾸며내지 않는다.

## 3. 공통 계약과 제한 정책

- 요청 레코드: `request_id`(중복 없는 문자열), `scheduled_at_ms`(시뮬레이션 시작 이후 정수), `client_id`, `ip`, `endpoint`, `method`, `rule_id`, `instance_id`; 출력: `arrival_at_ms`, `decision_at_ms`, `allowed|rejected|queued|processed`, `processed_at_ms|null`, `retry_after_ms|null`, `remaining|null`, `latency_us`, `reason`. 각 요청의 원본과 결과를 연결한다.
- 정책 키는 `rule_id + scope + client_id/IP/endpoint`를 명시적으로 인코딩한다. 전역·사용자·IP·엔드포인트별 정책을 설정 파일로 선택하며, 규칙이 여러 개면 **모든 적용 규칙이 허용해야 허용**한다. MVP는 단일 규칙별 실험을 먼저 제공하고 복수 규칙은 별도 검증한다.
- 기본 정책: 사용자별 10건/1초, 버킷 용량 10, 재충전·누출 속도 10건/초, 큐 길이 10. 동일 설정 수치는 알고리즘마다 의미가 다르므로 보고서에 그대로 기재한다. 시뮬레이션 창 시작은 0ms, 구간은 `[start,end)`, 시간은 정수 ms 또는 단조 시계를 사용한다. 동시간 요청은 `request_id`의 안정적인 순서로 정렬한다.
- HTTP `GET /work?client_id=...`는 허용 시 200, 거절 시 429를 반환한다. `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`(초)를 가능한 범위에서 반환하고 해당 알고리즘에서 정확한 값이 아닌 경우 문서화한다. 누출 버킷의 202(큐 접수)와 실제 처리 완료를 구별한다. 실제 작업은 고정된 가상 작업 또는 설정 가능한 지연으로 모델링한다.

## 4. 알고리즘별 명세

| 방식                   | 구현과 시간 경계                                                                                                                                                                              | 별도 관찰 항목                                                                                                |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Token bucket           | 용량 C, 연속 재충전 `min(C, tokens + elapsed_seconds * r)`; 처음 C개; 허용 때 1개 소비; 거절 때 소비하지 않음                                                                                 | 순간 버스트 허용, 회복 시간, 키당 상태                                                                        |
| Leaking bucket         | FIFO 용량 Q, 초당 r건 처리; 접수·처리·거절 상태를 따로 기록; 큐가 꽉 차면 거절. 시뮬레이터는 `scheduled_at_ms`까지 누출을 진행하고 일정한 간격으로 처리한다                                   | 큐 대기 시간 p50/p95/p99, 최대 길이, 실제 처리 속도. HTTP 구현에서는 비동기 작업 큐와 처리 이벤트 기록        |
| Fixed window counter   | `floor(t/W)`의 창에서 최초 L개만 허용, 허용 건만 카운트                                                                                                                                       | 창 경계에서 임의 이동 창 W 안의 초과 허용                                                                     |
| Sliding window log     | `[t-W,t]`에 들어오는 시각을 유지하고 신규 요청 전 `timestamp <= t-W`는 제거. **PDF 설명 재현 모드**에서는 거절 시각도 기록하고, **strict 모드**에서는 허용 시각만 기록; 두 모드를 결과에 분리 | strict 모드에서는 허용 건 기준 모든 이동 창의 상한 검증; 재현 모드의 거절 누적에 따른 메모리와 후속 거절 비교 |
| Sliding window counter | 이전 고정 창 건수 `prev * (1 - offset/W)` + 현재 창 건수 `curr`의 추정치가 L 미만이면 허용; 정수 반올림 전에 실수 비교, 허용 건만 카운트                                                      | 정확 로그 방식 대비 false allow/false reject 및 경계 오차                                                     |

모든 방식은 `decide(key, time)`이라는 동일한 인터페이스를 제공한다. 누출 버킷은 `admit`과 `drain` 이벤트를 추가 제공한다. 창 종료와 TTL에 따른 유휴 키 제거를 구현하고, 키 수 증가 실험에는 제거 전후 값을 모두 남긴다. 정책 선택과 수치의 의미가 달라 **허용 비율만으로 우열을 매기지 않는다**.

## 5. 부하 생성과 측정

- 시나리오 생성기는 seed를 입력으로 받고, 생성한 요청 CSV를 저장한다. 60초 기본 실행(설정 변경 가능): 일반 패턴은 사용자 100명 각각 평균 5 req/s의 독립 Poisson 도착; 과부하는 20~25초에 동일 사용자 집단에 기본의 10배 추가 도착; 경계 사례는 0.99초와 1.01초에 동일 키로 각각 10건; 고유 키 증가는 30~40초 사이 한 번씩 요청하는 10,000개 키; 핫 키는 1개 사용자에 100개 동시 요청. 시나리오별로 규칙·seed·총 발생 건수·구간별 목표/실제 RPS를 기록한다.
- 결정적 재생: 생성한 CSV를 모든 알고리즘에 같은 순서로 주입하고 가상 시계로 상태 전이를 검증. 벽시계 기반 부하는 별도 실행해 1/2/4 인스턴스, 동시성 1/32/128, 각 3회 반복, 예열 10초 후 60초 측정. 사용자 장비 성능에 따라 설정을 줄일 수 있게 한다.
- 결정 지연(`decide` 호출 전후의 단조 시계), HTTP 왕복 지연(부하 발생기 관점), 요청 스케줄 대비 발송 지연을 **별도** p50/p95/p99/max로 기록. 처리량(요청/초), 허용/거절/접수/처리 건수와 비율, 누출 버킷의 큐 지연, 1초 및 100ms 구간별 요청·허용·처리 건수, 이동 1초 최대 허용량을 기록한다. 실패·타임아웃은 거절과 분리한다.
- Python 메모리: `tracemalloc`의 추적 peak와 RSS peak를 각각 표시하고 시나리오 종료 후 활성 상태 키/로그 엔트리 수 기록. Redis `INFO memory`의 `used_memory`, `MEMORY USAGE` 표본, 키 수 및 TTL 확인. 컨테이너 전체 RSS는 가능하면 추가로 기록. 측정 범위(런타임/프로세스/Redis), 단위, 측정 시점을 함께 저장하며 서로 다른 범위를 직접 동등 비교하지 않는다.
- 결과 경로 `results/<UTC timestamp>/<scenario>/<algorithm>/<backend>/<replicate>/`: `requests.csv`, `timeseries.csv`, `summary.json`, `environment.json`; 상위 폴더에는 `comparison.csv`, 시간대별 처리 그래프, p95 지연·메모리 그래프와 주요 관찰을 담은 `report.md`. 머신·CPU·OS·Python/Redis/이미지 버전·정책·seed·실행 명령·실패 건수를 포함한다. 비교는 동일 규칙, 같은 입력, 동일 백엔드 조건별로 묶고 Lyft 비교는 별도 표에 표시한다.

## 6. 분산 환경 재현 및 Redis 비교

1. **카운터 동기화 실패:** API 인스턴스 A/B는 각자 메모리를 사용한다. 한 사용자에게 동일한 고정 창 내 10건을 A, 10건을 B로 보내 두 인스턴스에서 최대 20건 허용됨을 재현한다. 같은 요청을 공유 Redis 기반으로 재생해 전체 허용 건수가 10건임을 확인한다. 요청별 인스턴스와 상태를 로그에 기록한다.
2. **경쟁 상태 실패:** 별도 `unsafe_redis` 모드는 `GET` → 검사 → 동기화 장벽 → `SET`의 비원자적 경로를 제공한다. 예: limit=1, 같은 키·창, 두 독립 프로세스가 함께 읽은 뒤 쓰면 둘 다 허용된다. barrier에는 타임아웃을 두고 실험 외부에는 노출하지 않는다. 이후 Redis Lua 스크립트에서 검사·증가·최초 TTL 설정을 한 번에 수행해 같은 실험에서 정확히 1건만 허용됨을 확인한다. `INCR` 한 번만으로 조건부 허용과 TTL의 원자성이 자동 보장되는 것으로 취급하지 않는다.
3. Redis 백엔드의 기본 증명 대상은 고정 윈도와 token bucket이다. 각각 하나의 Lua 실행에서 판정·상태 변경·TTL을 처리한다. 슬라이딩 로그의 Redis 확장은 별도 구현 시 원자적 ZSET 정리·길이 검사·고유 멤버 삽입으로 검증한다. 시뮬레이션 시각을 Redis에 보내는 경우 모든 워커에 동일한 시간 기준을 강제하고 역행 시각을 거부한다. 실제 HTTP에서는 Redis `TIME` 또는 단일 서버 시계를 사용하며 시계 원천을 기록한다.
4. TTL·재시작·Redis 중단을 테스트한다. Redis 장애 시 `fail-open`/`fail-closed`를 설정으로 선택하고 별도의 `backend_error`와 결과를 기록한다. Redis 재시작 후 상태 유실의 영향을 보고서에 적는다. 이 실험은 단일 Redis의 정상 동작 시 원자성을 증명하며 복제·장애 조치에서의 전역 강한 일관성까지 주장하지 않는다.

## 7. Lyft 공개 서비스 연동

- 대상은 PDF의 Lyft 공개 구성 요소에서 이어지는 공식 [`envoyproxy/ratelimit`](https://github.com/envoyproxy/ratelimit). 공식 README와 [`docker-compose-example.yml`](https://github.com/envoyproxy/ratelimit/blob/main/docker-compose-example.yml)을 기준으로 검증된 commit과 이미지 digest를 기록한다. 사전 준비 시 실제 태그·gRPC 포트·설정 형식을 확인하여 README에 고정한다. 독립 Redis와 파일 기반 설정을 사용한다.
- 최소 `domain: auth`, `auth_type: login`, 5건/분의 PDF 규칙과 짧은 실험용 `domain: lab`, `client_id`별 10건/초 규칙을 구성한다. 도메인·descriptor의 동적 값이 사용자별로 독립 집계되는지 두 client로 확인한다. gRPC `ShouldRateLimit`을 직접 호출하는 어댑터를 우선 사용하고, 사용자에게 HTTP 429 체험을 제공하려면 Envoy 또는 명시적인 HTTP 래퍼를 별도로 구성한다. gRPC의 `OVER_LIMIT`와 HTTP 429를 혼동하지 않는다.
- 동일한 벽시계 요청 트레이스를 Lyft 어댑터와 자체 HTTP 구현에 각각 보내고 판정 결과·HTTP/gRPC 지연·Redis 메모리·서비스 RSS를 기록한다. 내부 알고리즘, 캐시 키, 설정 단위, 통신 경로가 다르므로 동일 알고리즘의 공정한 마이크로벤치마크로 해석하지 않는다. 1분 규칙은 실제 60초 이상 실행하거나 초 단위 랩 규칙을 별도로 사용하며 가상 시계를 주입할 수 있다고 가정하지 않는다.

## 8. 검증 및 완료 기준

- 각 알고리즘의 용량 경계, 시간 경계, 유휴 후 회복, 복수 사용자 격리, 키 TTL 검증. 요청 트레이스 seed가 같으면 결정적 재생 출력이 같다.
- 고정 창 경계 트레이스에서 이동 1초 허용 수가 10을 넘고, strict 슬라이딩 로그에서는 10을 넘지 않는다. 슬라이딩 카운터의 판정 차이는 strict 로그와 요청별로 대조해 오차율을 수치로 낸다.
- 누출 버킷은 접수와 처리 시각이 구별되고 지속 부하에서 처리율이 설정한 누출 속도를 넘지 않는다(이산 시간 해상도 오차는 설정값으로 명시).
- 2개 인스턴스 로컬 모드에서 10건 초과 허용, 비원자 Redis 모드에서 barrier로 2건 모두 허용, Lua 원자 모드에서 1건만 허용하는 결과를 자동화한 통합 테스트로 재현한다. 테스트용 장벽을 빼고 실행한 부하에서는 race 발생을 확률적 결과로 보고 실패 조건으로 삼지 않는다.
- Lyft 연결 시험은 정상/초과 gRPC 응답, 별도 사용자 독립성, Redis 연결 확인 및 서비스 기동 실패 진단을 포함한다. 모든 벤치마크의 요약 건수 합계가 입력 수 및 각 상태별 결과 합계와 일치해야 하며, 보고서에는 실행하지 못한 비교를 `not_run`으로 남긴다.

## 9. 구현 순서

1. 정책 스키마, 요청 CSV 생성기, 결정적 시계와 다섯 알고리즘 구현 및 단위 검증.
2. 동일 트레이스 재생, 결과 집계·그래프·메모리 측정, HTTP API와 실제 부하 실행.
3. 다중 API 인스턴스와 Redis 비원자·Lua 원자 비교, 장애 실험.
4. Lyft 서비스 연동 및 독립 비교 보고서 생성; README에 실행·해석·한계 작성.

## 출처

- 첨부 `/ch04_rate_limiter/ch04.pdf`, Chapter 4: Design a Rate Limiter, 특히 Algorithms for rate limiting, Rate limiter in a distributed environment, Monitoring.
- `envoyproxy/ratelimit` 공식 저장소 README와 Docker Compose 예제(구현 착수 시 commit/version 고정).
