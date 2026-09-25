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
├── algorithms/  api/  load/  metrics/  distributed/  lyft/
configs/rules.yaml # 기본 정책: 사용자별 10건/초, 버킷·큐 10, 재충전·누출 10건/초
scripts/           # 실험 실행 스크립트 (이후 단계)
tests/
results/           # 실행 시 생성 (git 제외)
```

### 공통 계약 요약

- 시간: 이름이 `_ms`인 값은 시뮬레이션 시작(0ms) 기준 정수 ms. 구간은 반열린 `[start, end)`.
- 재생 순서: `scheduled_at_ms` 오름차순, 같은 시각은 `request_id` 문자열 순(ID는 0으로 채워 생성).
- 판정 상태: `allowed`, `rejected`, `queued`, `processed`. `processed`만 `processed_at_ms`를 갖는다.
- CSV의 null은 빈 칸. 헤더·필수 값·정수 단위(`1.5`, `1s` 불가)·중복 `request_id`를 읽을 때 검사한다.
- 정책 키: `rule_id`와 대상 값을 퍼센트 인코딩하므로 `:` 등이 섞여도 서로 다른 규칙/대상의 키가 충돌하지 않는다. 전역 규칙의 대상은 `*`.
