# Scenario vruntime convention

CFS 스케줄링에 쓰이는 `initial_vruntime` / `vruntime_increment`를 priority에서 자동 도출하기 위한 컨벤션. 새 시나리오 추가 시 손으로 숫자를 비교/조정할 필요를 줄이기 위함.

## priority → vruntime mapping

`ScenarioType._PRIORITY_VRUNTIME_DEFAULTS` (`scenarios.py`):

| priority | initial_vruntime | vruntime_increment | 의도 |
|---|---|---|---|
| `"high"` | -5.0 | 3.0 | 드물게 ready, 발화 시 가치 큼 → 우선 픽 |
| `"medium"` | 0.0 | 2.0 | 기본 |
| `"low"` | 5.0 | 2.0 | 자주 ready 가능, 다른 시나리오 양보 |

서브클래스가 클래스 attribute로 `initial_vruntime` / `vruntime_increment`를 명시하지 않으면 `ScenarioType.__init__`이 priority 기반 default를 자동 채움.

## 의미

- **`initial_vruntime`**: 첫 fire 기회 순서. 낮을수록 먼저 picked.
- **`vruntime_increment`**: 한 번 발화당 charged. 높을수록 다음 발화까지 더 양보. CFS의 fair-share를 작동시키는 본질.
- 두 값의 **명목 단위는 통일** — 시나리오 간 직접 비교 가능해야 의미 있음.

## 사용 패턴

### A. priority만 선언 (권장)
```python
class FooScenario(ScenarioType):
    name = "foo"
    priority = "medium"
    # initial_vruntime / vruntime_increment 생략
    # → base가 priority="medium" default (0.0, 2.0) 자동 적용

    def evaluate(self, context): ...
```

### B. 부분 override (필요한 것만 명시)
```python
class BarScenario(ScenarioType):
    name = "bar"
    priority = "high"
    initial_vruntime = -8.0   # 명시 override (사유 주석)
    # vruntime_increment 생략 → high default의 3.0 적용

    def evaluate(self, context): ...
```

### C. 전체 override (특수 사유, 명시적 #주석 권장)
```python
class BazScenario(ScenarioType):
    name = "baz"
    priority = "low"
    # 매 캡처마다 ready되지만 fire 후 한참 양보해야 하는 특수 케이스
    initial_vruntime = 0.0
    vruntime_increment = 10.0

    def evaluate(self, context): ...
```

### D. ctor 인자 (테스트/실험 시 instance-level override)
```python
s = FooScenario(initial_vruntime=-3.0, vruntime_increment=4.0)
```

## 현재 5개 시나리오 (`scenarios.py`)

기존 명시 override는 **그대로 유지** (현 동작 보존). 새 시나리오부터 컨벤션 적용.

| 시나리오 | priority | initial | increment | default 대비 | 비고 |
|---|---|---|---|---|---|
| `idle_after_writing` | medium | 0.0 | **1.0** | increment ↓ | 자주 떠도 적게 양보 (high frequency) |
| `whole_document_review` | high | **-10.0** | **5.0** | 더 강함 | 매우 우선, 한 번 떴으면 길게 양보 |
| `long_static_review` | low | **10.0** | **3.0** | initial ↑↑, increment ↑ | 매우 뒤로 |
| `paragraph_churn` | medium | **3.0** | 2.0 | initial ↑ | idle보다 약간 뒤 |
| `blank_document_start` | low | **8.0** | 2.0 | initial ↑ | low 중에서도 약간 뒤 |

→ 시간 두고 검토 후 컨벤션 default로 통일할 가치 있는 시나리오는 명시 override 제거 가능.

## 결정 시 가이드

- 시나리오를 **자주 트리거**시키고 싶음 → `priority = "high"` (initial이 낮음)
- 시나리오가 발화 후 **한참 잠잠해야** 함 → `vruntime_increment`를 명시 override로 키움 (또는 별도 시간 기반 cooldown 사용)
- 비슷한 priority의 다른 시나리오와 **fair-share** 원함 → priority 같게 두고 nothing override (CFS가 알아서)
- 새 priority 분류가 필요해지면 → `_PRIORITY_VRUNTIME_DEFAULTS`에 추가

## 보완 레이어

vruntime/cooldown 외에 다음 두 가지가 발화율을 추가로 조정:

1. **각 시나리오의 `cooldown_min_seconds`** — 같은 시나리오 발화 간 최소 시간. base의 `_time_cooldown_status(last_fired_at)` 헬퍼 사용.
2. **`ScenarioScheduler.min_global_fire_interval_sec`** (default 10.0) — 시나리오 무관 전역 throttle. 어떤 시나리오든 마지막 발화로부터 N초 내엔 또 안 뜸.

vruntime은 "**누가** 다음에 뜨나"를 결정하고, cooldown/throttle은 "**언제** 뜰 수 있나"를 결정. 두 레이어 독립.
