# DRB 벤치마크 실행 런북 (AutoSurvey vs Flat)

복사-붙여넣기용 단계별 가이드. AutoSurvey(iterative)와 flat baseline을 같은 조건에서
생성 → 검증 → 채점(RACE/FACT) → 분석한다. **FACT는 Jina 대신 crawl4ai로 스크랩**하므로
JINA_API_KEY 없이 RACE+FACT를 모두 돌릴 수 있다.

---

## 0. 규칙 / 전제

PowerShell에서 인터프리터는 **env python 전체 경로**를 권장한다(`conda activate` 불필요):

```powershell
$py = "C:\Users\pc21\miniconda3\envs\agent\python.exe"
$env:PYTHONUTF8 = "1"            # 한국어 Windows(cp949)에서 유니코드 출력 깨짐 방지
# 경로 확인: & $py -c "import sys; print(sys.executable)"
```

> ⚠️ `conda run -n agent python ...` 도 되지만, 자식 프로세스가 **비-ASCII(웹 본문/한글)**
> 를 출력하면 `conda run`이 cp949로 다시 출력하다 `UnicodeEncodeError`로 죽는다.
> 그래서 생성/스크랩 단계는 **`& $py`(직접 경로)** 를 쓴다. 아래 명령은 모두 `& $py` 기준.

- **하니스 명령**(`benchmarks.drb.*`)은 **repo 루트**에서 실행: `C:\Users\pc21\Desktop\veritas`
- **DRB 채점 스크립트**(`deepresearch_bench_race.py`, `utils.*`)는 **`deep_research_bench\` 안에서** 실행
- 전제 체크리스트:
  - [ ] **llama-server `:8080` 가동** ← *생성의 하드 블로커* (2단계)
  - [x] crawl4ai 설치됨 (생성 페치 + FACT 스크랩 둘 다 사용)
  - [ ] 네트워크(웹 검색/페치/스크랩)
  - [ ] 채점 시: `OPENAI_API_KEY`, `LLM_BACKEND=openai`, `pandas` 설치
  - **FACT에 JINA_API_KEY 불필요** (crawl4ai로 스크랩)

> 모델 이름은 1차 로컬 페어 기준: `veritas_autosurvey_local_m15`, `flat_local_web_m15`

---

## 1. 단위 테스트 (서버/네트워크 불필요, 지금 바로 가능)

```powershell
& $py -m unittest `
  tests.test_drb_vendor_layout tests.test_drb_benchmark_io tests.test_drb_citation_adapter `
  tests.test_drb_flat_baseline tests.test_drb_analysis tests.test_drb_crawl4ai_scrape
```

---

## 2. llama-server 기동 (:8080) — 생성의 전제

별도 터미널에서 띄워두고(켜둔 채로) 다음 단계로:

```powershell
& $py launcher.py
```

- 벤치 생성은 `:8080`(chat)만 있으면 됨. 임베딩 `:8081`은 `run_all`이 RAG 인덱싱을 안 하므로 **불필요**.
- 확인: `(Test-NetConnection 127.0.0.1 -Port 8080).TcpTestSucceeded` → `True`

---

## 3. 생성 (raw 기사 만들기) — 양쪽 시스템

> 로컬 generator 기준(기본값). 둘 다 `--max-docs 15 --scout-docs 3 --batch-size 5 --fetch-max-chars 100000` 기본.

### 3-A. 2-task smoke (먼저 동작 확인)

```powershell
& $py -m benchmarks.drb.veritas_runner --model-name veritas_autosurvey_local_m15 --limit 2 --resume
& $py -m benchmarks.drb.flat_runner    --model-name flat_local_web_m15 --limit 2 --resume
```

### 3-B. 10-task 층화 pilot (zh 5 + en 5; id는 query.jsonl 보고 조정)

```powershell
& $py -m benchmarks.drb.veritas_runner --model-name veritas_autosurvey_local_m15 --task-ids 1,8,16,25,40,51,60,70,84,96 --resume
& $py -m benchmarks.drb.flat_runner    --model-name flat_local_web_m15 --task-ids 1,8,16,25,40,51,60,70,84,96 --resume
```

- 출력: `deep_research_bench\data\test_data\raw_data\<model>.jsonl` (공식: `id`/`prompt`/`article`만)
- 메타: `<model>.jsonl.meta.jsonl` (timings/budgets/counts/warnings — 키·본문 없음)
- 워크스페이스: `runs\drb\<model>\task_<id>\` (gitignore됨), `--resume`는 완료 task 건너뜀

---

## 4. raw 데이터 검증 (채점 전 필수)

```powershell
& $py -m benchmarks.drb.validate_raw_data `
  deep_research_bench\data\test_data\raw_data\veritas_autosurvey_local_m15.jsonl `
  deep_research_bench\data\test_data\raw_data\flat_local_web_m15.jsonl
```

확인: 공식 키만 / 비어있지 않은 article / inline `[n]` 인용 / URL 있는 `## References`.

---

## 5. 채점 준비 (env + deps)

```powershell
& $py -m pip install pandas   # 누락돼 있음 (또는 -r deep_research_bench\requirements.txt)

$env:LLM_BACKEND   = "openai"        # ★ 기본이 openrouter라 반드시 설정
$env:OPENAI_API_KEY = "..."          # 이미 등록한 키
$env:RACE_MODEL    = "gpt-5.4-mini"  # 예산 judge (공식 기본은 gpt-5.5)
$env:FACT_MODEL    = "gpt-5.4-mini"
# JINA_API_KEY 불필요 — FACT 스크랩을 crawl4ai로 대체
```

> ⚠️ `RACE_MODEL`을 gpt-5.5가 아닌 값으로, 또는 FACT 스크랩을 crawl4ai로 바꾸면 **비공식**
> (leaderboard 비교 불가). 같은 judge·같은 scraper를 **양쪽 시스템에 동일하게** 적용하면
> A/B delta는 유효 → 결과는 `budget_judge` / `fact_crawl4ai_budget`로 라벨.

---

## 6. RACE 채점 (보고서 품질, Jina 불필요)

```powershell
cd deep_research_bench
& $py -u deepresearch_bench_race.py veritas_autosurvey_local_m15 `
  --raw_data_dir data/test_data/raw_data --query_file data/prompt_data/query.jsonl `
  --output_dir results/race/veritas_autosurvey_local_m15 --only_en --limit 4
& $py -u deepresearch_bench_race.py flat_local_web_m15 `
  --raw_data_dir data/test_data/raw_data --query_file data/prompt_data/query.jsonl `
  --output_dir results/race/flat_local_web_m15 --only_en --limit 4
cd ..
```

- 산출: `results/race/<model>/{raw_results.jsonl, race_result.txt}`
- RACE_MODEL이 cleaning + scoring 둘 다에 쓰임(비용 큼) → task 수로 비용 조절

---

## 7. FACT 채점 (인용 신뢰도, **crawl4ai 스크랩 — Jina 키 불필요**)

DRB FACT 5단계 중 **scrape만 crawl4ai로 교체**한다(나머지는 공식 그대로). 평가자 트리는 무수정.
extract/deduplicate/validate/stat은 `OPENAI_API_KEY`(FACT_MODEL)를 쓴다.

```powershell
cd deep_research_bench
$m = "veritas_autosurvey_local_m15"
New-Item -ItemType Directory -Force "results/fact/$m" | Out-Null

& $py -u -m utils.extract     --raw_data_path "data/test_data/raw_data/$m.jsonl" --output_path "results/fact/$m/extracted.jsonl"     --query_data_path data/prompt_data/query.jsonl --n_total_process 6
& $py -u -m utils.deduplicate --raw_data_path "results/fact/$m/extracted.jsonl"   --output_path "results/fact/$m/deduplicated.jsonl" --query_data_path data/prompt_data/query.jsonl --n_total_process 6
# ↓ Jina 대신 crawl4ai (repo 루트의 스크립트를 파일 경로로 호출; 자체적으로 repo 루트를 path에 추가함)
& $py ..\benchmarks\drb\crawl4ai_scrape.py --raw_data_path "results/fact/$m/deduplicated.jsonl" --output_path "results/fact/$m/scraped.jsonl" --n_total_process 4
& $py -u -m utils.validate    --raw_data_path "results/fact/$m/scraped.jsonl"      --output_path "results/fact/$m/validated.jsonl"   --query_data_path data/prompt_data/query.jsonl --n_total_process 6
& $py -u -m utils.stat        --input_path  "results/fact/$m/validated.jsonl"      --output_path "results/fact/$m/fact_result.txt"
cd ..
# flat_local_web_m15 에 대해서도 $m 만 바꿔 동일 반복
```

- 산출: `results/fact/<model>/fact_result.txt` (`total_citations`, `total_valid_citations`, `valid_rate`)
- crawl4ai는 HTTP-only라 JS/anti-bot 페이지엔 약함. 인용 URL은 어차피 crawl4ai로 수집된 것이라
  대체로 재스크랩 성공. **양쪽 시스템에 동일 적용**되므로 A/B는 공정.
- (선택) 공식 Jina 경로를 쓰려면 위 crawl4ai 줄 대신
  `& $py -u -m utils.scrape ...` + `$env:JINA_API_KEY="..."`.

---

## 8. 분석 (paired delta 리포트)

```powershell
& $py -m benchmarks.drb.analyze_results `
  --system-a veritas_autosurvey_local_m15 `
  --system-b flat_local_web_m15 `
  --label budget_judge
```

- 산출: `bench_results\drb\<a>__vs__<b>\{summary.csv, paired_deltas.csv, comparison_report.md}`
- RACE per-task(`raw_results.jsonl`) 있으면 paired delta·win rate·bootstrap 95% CI(고정 seed),
  없으면 `race_result.txt` aggregate-only로 degrade. FACT는 `fact_result.txt` aggregate 비교.

---

## (옵션) OpenAI generator로 생성

로컬이 아닌 OpenAI(gpt-5-mini 등)로 **생성**하려면 3단계 전에:

```powershell
$env:VERITAS_AUTOSURVEY_LLM_PROVIDER = "openai"
$env:VERITAS_AUTOSURVEY_OPENAI_MODEL = "gpt-5-mini"
$env:OPENAI_API_KEY = "..."
# 모델명도 구분: veritas_autosurvey_gpt5mini_m15 / flat_gpt5mini_web_m15
```

> 주의: OpenAI generator여도 runner는 시작 시 로컬 `LLMClient(:8080)`을 먼저 만들기 때문에
> **:8080 chat 서버는 떠 있어야 함**(main.py와 동일 패턴).

---

## 비용 / 라벨 정책 (≈ USD 90)

1. 단위테스트 — 무료
2. 2-task smoke 생성 — 로컬 generator면 API 비용 ≈ 0
3. 10-task pilot — `RACE_MODEL=FACT_MODEL=gpt-5.4-mini`, FACT는 crawl4ai → 결과 `budget_judge` / `fact_crawl4ai_budget`
4. (선택) 3~5 task만 `RACE_MODEL=gpt-5.5` + 공식 Jina로 `official_judge_confirmation`
5. 100-task 풀 채점은 예산 범위 밖 → "not run"
- 실행 전 **모델 가격 직접 확인**(코드에 하드코딩 안 함)

---

## 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `conda activate` 에러 (`Run 'conda init'`) | `conda activate` 쓰지 말고 `& $py`(전체 경로) 사용 |
| `conda run` 중 `UnicodeEncodeError: 'cp949'` | `conda run`의 stdout 재출력 버그(한국어 Windows). `& $py` + `$env:PYTHONUTF8="1"` 사용 |
| runner가 즉시 접속 실패 | `:8080` llama-server 꺼짐 → 2단계로 기동 |
| `ModuleNotFoundError: pandas` (채점) | `& $py -m pip install pandas` |
| RACE/extract가 OpenRouter 키를 찾음 | `$env:LLM_BACKEND="openai"` 설정 누락 |
| `crawl4ai is not installed` | `& $py -m pip install crawl4ai` (이미 설치돼 있음) |
| `benchmarks.drb.* 못 찾음` | repo 루트(`...\veritas`)에서 실행했는지 확인 |
| `utils.* import 오류` | `deep_research_bench\` 안에서 실행했는지 확인 |
| 생성물이 git에 잡힘 | 정상 ignore됨: `git check-ignore -v deep_research_bench\data\test_data\raw_data\veritas_autosurvey_local_m15.jsonl` |
